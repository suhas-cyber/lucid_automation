"""
Dashboard helpers: unified CAN/Modbus capture, UART logger, audit log, snapshot parsers.
"""

from __future__ import annotations

import collections
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

_EV_BASE = Path(
    os.environ.get("EV_LAB_DATA", str(Path.home() / "ev_lab_data"))
).resolve()
_SESSION_ROOT = _EV_BASE / "capture_sessions"
_SESSION_ROOT.mkdir(parents=True, exist_ok=True)

_audit: collections.deque[tuple[str, str]] = collections.deque(maxlen=8000)

_unified_lock = threading.Lock()
_unified_state: dict[str, Any] = {
    "running": False,
    "thread": None,
    "stop": None,
    "session_id": None,
    "session_dir": None,
    "error": None,
    "host": None,
    "username": None,
}

_uart_lock = threading.Lock()
_uart_logger: Any = None
# Allowed directories for UART log downloads (retained after stop for file retrieval).
_uart_roots: set[Path] = set()
_uart_last_snapshot: dict[str, Any] = dict()

# DER/NFT use "switch_state"; MID uses "mid_relay_state" in MidRelayState JSON.
_RELAY_SWITCH_RE = re.compile(
    r'"(?:switch_state|mid_relay_state)"\s*:\s*"(SWITCH_STATE_[^"]+)"'
)
_MID_LED_RE = re.compile(r"Request to DER:\s*(.+?)(?:\s*\[|$)")


def audit(message: str) -> None:
    """Append one dashboard audit line (server-side actions)."""
    ts = datetime.now().isoformat(timespec="seconds")
    _audit.append((ts, message))


def audit_entries() -> list[dict[str, str]]:
    return [{"t": t, "msg": m} for t, m in _audit]


def audit_text() -> str:
    return "\n".join(f"{t}\t{m}" for t, m in _audit)


def _switch_display(raw: str) -> str:
    r = raw.upper()
    if "CLOSED" in r:
        return "CLOSED"
    if "OPEN" in r:
        return "OPEN"
    return raw.replace("SWITCH_STATE_", "").replace("_", " ")


def parse_can_relays_from_text(text: str) -> dict[str, str]:
    """Extract latest NFT / DER / MID relay states from CAN filtered capture text."""
    out: dict[str, str | None] = {
        "nft_relay": None,
        "der_relay": None,
        "mid_relay": None,
    }
    for line in text.splitlines():
        if "PAYLOAD_MID_RELAY_STATE" in line:
            m = _RELAY_SWITCH_RE.search(line)
            if m:
                out["mid_relay"] = _switch_display(m.group(1))
        elif "PAYLOAD_DER_LANDED_CIRCUIT_CONTROL_STATE" in line:
            m = _RELAY_SWITCH_RE.search(line)
            if m:
                out["der_relay"] = _switch_display(m.group(1))
        elif "PAYLOAD_NFT_LANDED_CIRCUIT_CONTROL_STATE" in line:
            m = _RELAY_SWITCH_RE.search(line)
            if m:
                out["nft_relay"] = _switch_display(m.group(1))
    return {
        "nft_relay": out["nft_relay"] or "—",
        "der_relay": out["der_relay"] or "—",
        "mid_relay": out["mid_relay"] or "—",
    }


def parse_mid_led_mode_from_text(text: str) -> str:
    """Last MID-Led-Mode-Request decode line from Modbus full log."""
    last: Optional[str] = None
    for line in text.splitlines():
        if "MID LED Mode Request" not in line and "0x008C" not in line:
            continue
        m = _MID_LED_RE.search(line)
        if m:
            last = m.group(1).strip()
    if last:
        return last
    # Fallback: microgrid wording from DER Status register lines
    for line in reversed(text.splitlines()):
        if "Microgrid=" in line and "OpMode=" in line:
            return line.strip()[:220]
    return "—"


def _latest_file(root: Path, pattern: str) -> Optional[Path]:
    paths = list(root.rglob(pattern))
    if not paths:
        return None
    return max(paths, key=lambda p: p.stat().st_mtime)


def snapshot_from_session_dir(session_dir: Path) -> dict[str, Any]:
    """Read tail of CAN capture + Modbus logs for dashboard widgets."""
    can_path = _latest_file(session_dir, "canbus_capture_*.txt")
    if can_path is None:
        can_path = _latest_file(session_dir, "CAN bus capture_*.txt")
    mod_path = _latest_file(session_dir, "modbus_logs_*.txt")
    if mod_path is None:
        mod_path = _latest_file(session_dir, "Modbus logs_*.txt")

    can_txt = ""
    if can_path and can_path.is_file():
        try:
            can_txt = can_path.read_text(encoding="utf-8", errors="replace")[-120000:]
        except OSError:
            can_txt = ""

    mod_txt = ""
    if mod_path and mod_path.is_file():
        try:
            mod_txt = mod_path.read_text(encoding="utf-8", errors="replace")[-120000:]
        except OSError:
            mod_txt = ""

    relays = parse_can_relays_from_text(can_txt)
    mid_led = parse_mid_led_mode_from_text(mod_txt)
    return {
        "can_relays": relays,
        "mid_led_mode_request": mid_led,
        "can_capture_file": str(can_path) if can_path else None,
        "modbus_log_file": str(mod_path) if mod_path else None,
    }


def list_session_files(session_dir: Path) -> list[str]:
    if not session_dir.is_dir():
        return []
    out: list[str] = []
    for p in session_dir.rglob("*"):
        if p.is_file():
            out.append(str(p.relative_to(session_dir)))
    return sorted(out)


def safe_file_under(base: Path, rel: str) -> Path:
    """Resolve rel inside base; reject path traversal."""
    base_r = base.resolve()
    target = (base_r / rel).resolve()
    if not str(target).startswith(str(base_r)):
        raise ValueError("Invalid path")
    if not target.is_file():
        raise FileNotFoundError(rel)
    return target


_SESSION_ID_RE = re.compile(r"^[a-f0-9]{32}$")


def safe_session_download(session_id: str, rel: str) -> Path:
    if not _SESSION_ID_RE.match(session_id):
        raise ValueError("Invalid session id")
    base = _SESSION_ROOT / session_id
    return safe_file_under(base, rel)


def flush_uart_open_files() -> None:
    """Push logger buffers to disk so downloads see data while capture is active."""
    with _uart_lock:
        inst = _uart_logger
    if inst is None:
        return
    handles = getattr(inst, "file_handles", {}) or {}
    for hmap in handles.values():
        for key in ("log", "capture"):
            fh = hmap.get(key)
            if fh is None:
                continue
            try:
                if not fh.closed:
                    fh.flush()
            except OSError:
                pass


def uart_resolve_existing_file(abs_path: str) -> Path:
    """Allow download only if path lies under a registered UART capture root."""
    target = Path(abs_path).resolve()
    if not target.is_file():
        raise FileNotFoundError(abs_path)
    for root in _uart_roots:
        root_r = root.resolve()
        try:
            target.relative_to(root_r)
            return target
        except ValueError:
            continue
    raise PermissionError("Path not allowed")


def unified_running() -> bool:
    with _unified_lock:
        return bool(_unified_state.get("running"))


def unified_status() -> dict[str, Any]:
    with _unified_lock:
        st = dict(_unified_state)
    sess = st.get("session_dir")
    snap: dict[str, Any] = {}
    if sess:
        p = Path(sess)
        if p.is_dir():
            snap = snapshot_from_session_dir(p)
            snap["files"] = list_session_files(p)
    out = {
        "running": st.get("running", False),
        "session_id": st.get("session_id"),
        "session_dir": st.get("session_dir"),
        "host": st.get("host"),
        "username": st.get("username"),
        "error": st.get("error"),
        **snap,
    }
    return out


def start_unified(
    username: str,
    host: str,
    password: str,
    modbus_filters: Optional[list[str]] = None,
) -> dict[str, Any]:
    """Start unified CAN+Modbus capture in a background thread."""
    _ = modbus_filters  # API compatibility; ``updated_log.run_capture`` has no filter hook.
    with _unified_lock:
        if _unified_state["running"]:
            return {"ok": False, "error": "Unified capture already running"}
        sid = uuid.uuid4().hex
        session_path = _SESSION_ROOT / sid
        session_path.mkdir(parents=True, exist_ok=True)
        stop_evt = threading.Event()
        err_box: list[str] = []

        def worker() -> None:
            try:
                from updated_log import run_capture

                run_capture(
                    username=username,
                    host=host,
                    password=password,
                    output_dir=str(session_path),
                    duration=None,
                    stop_requested=stop_evt,
                )
            except Exception as exc:
                err_box.append(str(exc))
            finally:
                with _unified_lock:
                    _unified_state["running"] = False
                    _unified_state["stop"] = None
                    if err_box:
                        _unified_state["error"] = err_box[0]

        th = threading.Thread(target=worker, name="unified-capture", daemon=True)
        _unified_state.update(
            {
                "running": True,
                "thread": th,
                "stop": stop_evt,
                "session_id": sid,
                "session_dir": str(session_path),
                "error": None,
                "host": host,
                "username": username,
            },
        )
        th.start()

    audit(f"Unified CAN/Modbus capture START → session {sid} ({username}@{host})")
    return {"ok": True, "session_id": sid, "session_dir": str(session_path)}


def stop_unified() -> dict[str, Any]:
    with _unified_lock:
        if not _unified_state["running"] or _unified_state["stop"] is None:
            return {"ok": False, "error": "Unified capture not running"}
        stop = _unified_state["stop"]
        sid = _unified_state.get("session_id")
        stop.set()
    audit(f"Unified CAN/Modbus capture STOP (session {sid})")
    return {"ok": True, "session_id": sid}


def uart_running() -> bool:
    with _uart_lock:
        return _uart_logger is not None


def uart_status() -> dict[str, Any]:
    with _uart_lock:
        on = _uart_logger is not None
        inst = _uart_logger
    out: dict[str, Any] = {"running": on}
    if inst is not None:
        out["output_dir"] = getattr(inst, "output_dir", None)
        out["log_files"] = getattr(inst, "log_files", {}) or {}
        out["capture_files"] = getattr(inst, "capture_files", {}) or {}
        out["host"] = getattr(inst, "host", None)
        out["username"] = getattr(inst, "username", None)
    else:
        snap = _uart_last_snapshot
        out["output_dir"] = snap.get("output_dir")
        out["log_files"] = snap.get("log_files", {})
        out["capture_files"] = snap.get("capture_files", {})
    return out


def start_uart_capture(
    username: str,
    host: str,
    password: str,
    strings: Optional[list[str]] = None,
    output_dir: Optional[str] = None,
) -> dict[str, Any]:
    """Start UART logger non-interactively (filters + dashboard mode)."""
    global _uart_logger

    with _uart_lock:
        if _uart_logger is not None:
            return {"ok": False, "error": "UART capture already running"}

    base = output_dir or str(_SESSION_ROOT / f"uart_{uuid.uuid4().hex}")
    Path(base).mkdir(parents=True, exist_ok=True)
    _uart_roots.add(Path(base).resolve())
    filt = list(strings) if strings else []

    def runner() -> None:
        global _uart_logger
        try:
            from uart_logger_4 import UARTLogger

            lg = UARTLogger(
                host=host,
                username=username,
                password=password,
                output_dir=base,
            )
            ok = lg.start(serials=None, filter_strings=filt, dashboard=True)
            if ok:
                with _uart_lock:
                    _uart_logger = lg
            else:
                audit("UART capture failed during setup (config / devices)")
        except Exception as exc:
            audit(f"UART capture error: {exc}")

    threading.Thread(target=runner, name="uart-capture", daemon=True).start()

    audit(f"UART capture START ({username}@{host}) filters={len(filt)}")
    return {"ok": True, "output_dir": base}


def stop_uart_capture() -> dict[str, Any]:
    global _uart_logger
    global _uart_last_snapshot

    with _uart_lock:
        if _uart_logger is None:
            return {"ok": False, "error": "UART capture not running"}
        lg = _uart_logger
    audit("UART capture STOP requested")
    lg.stop()
    _uart_last_snapshot = {
        "output_dir": getattr(lg, "output_dir", None),
        "log_files": getattr(lg, "log_files", {}) or {},
        "capture_files": getattr(lg, "capture_files", {}) or {},
    }
    with _uart_lock:
        _uart_logger = None
    return {"ok": True}


def uart_pick_download(which: str) -> Path:
    """which: 'log' | 'capture' — first module's file while UART capture is active."""
    with _uart_lock:
        if _uart_logger is None:
            raise RuntimeError("UART not running")
        lg = _uart_logger
    if which == "log":
        files = getattr(lg, "log_files", {}) or {}
    elif which == "capture":
        files = getattr(lg, "capture_files", {}) or {}
    else:
        raise ValueError("which must be log or capture")
    if not files:
        raise FileNotFoundError("No UART files yet")
    first = next(iter(files.values()))
    p = Path(first)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    return p
