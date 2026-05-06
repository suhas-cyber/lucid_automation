#!/usr/bin/env python3
"""
EV Lab Proxy Server
Combines: Lucid EVSE (10.91.0.250) + Pilot Dingus (/dev/ttyACM15) + Waveshare Modbus relay (contactors)

Uses subprocess curl exclusively — bypasses OpenSSL 3.0 TLS incompatibility
with the charger's embedded server.

Run:
  pip install 'fastapi' 'uvicorn[standard]' pyserial pymodbus
  uvicorn proxy_update:app --host 0.0.0.0 --port 8000   # or copy as proxy.py

Login mimics the browser: cookie jar, GET /index.html, hidden form fields,
Referer/Origin, and X-Requested-With: XMLHttpRequest. Override jar path with
CHARGER_COOKIE_JAR if needed.

Lucid `getconfiguration.cgi` data (V2H checklist + SOC/SOH) needs a valid cookie
`sid=…`. If auto-login always returns reject on the wall PC, set one of:
  CHARGER_SID=…           — initial sid at process start
  CHARGER_SID_FILE=/path  — file containing one line sid; updated mtime reloads it
  POST /api/session/sid   — JSON {"sid":"…"} (optional Bearer CHARGER_SID_TOKEN)
Set CHARGER_MANUAL_SID=1 with HARDCODED_SID to skip repeated failed logins.

Automatic sid renewal: Lucid does not expose a public “mint sid” API when login.cgi
returns reject — the proxy cannot invent a sid. Optional hooks run when a CGI
returns empty (expired sid):
  CHARGER_SID_REFRESH_CMD='…'   — shell; stdout first line or JSON {"sid":"…"}
  CHARGER_SID_REFRESH_URL='…' — curl GET; same body rules
  CHARGER_SID_HOOK_COOLDOWN_S=30 — min seconds between hook attempts (default 30)

Waveshare Modbus contactors (optional):
  pip install pymodbus
  RELAY_IP=10.91.0.201  RELAY_PORT=502  RELAY_C1=1 RELAY_C2=2 RELAY_C3=3 RELAY_STEP_DELAY=2
  RELAY_DISABLE=1     — skip relay if pymodbus/IP unavailable at startup

Open dashboard:
  http://10.91.0.98:8000
"""

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
import urllib.parse
from datetime import datetime
from fastapi import FastAPI, Header, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.websockets import WebSocket, WebSocketDisconnect
from pilot_dingus import PilotDingus

import dashboard_logs as dashlog

try:
    from uvicorn.protocols.utils import ClientDisconnected
except ImportError:
    ClientDisconnected = type("ClientDisconnected", (Exception,), {})

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])

# ── Charger Config ─────────────────────────────────────────────────────────────
CHARGER_BASE  = "https://10.91.0.250"
LOGIN_URL     = f"{CHARGER_BASE}/cgi-bin/login.cgi"

# !! Get fresh sid from browser !!
# Chrome → DevTools → Network → any home.cgi → Request Headers → Cookie: sid=XXXXX
HARDCODED_SID = "177787809600"   # ← update this when session expires

# Login credentials — CHARGER_LOGIN_PASS must match the Lucid "Password" box on index.html.
# Lucid often uses password-only (no username); LOGIN_USER is kept for dual-field firmware.
# Prefer env vars so secrets are not committed to git:
#   export CHARGER_LOGIN_PASS='your_wall_password'
#   export CHARGER_LOGIN_USER='admin'   # optional; ignored for password-only POSTs
LOGIN_USER = (os.environ.get("CHARGER_LOGIN_USER") or "admin").strip()
LOGIN_PASS = os.environ.get("CHARGER_LOGIN_PASS") or "2509000103"

# curl cookie jar — same path across requests so bootstrap + login share cookies
COOKIE_JAR = os.environ.get(
    "CHARGER_COOKIE_JAR",
    os.path.join(tempfile.gettempdir(), "ev_lab_charger_cookies.txt"),
)

# Browser-like defaults many embedded CGIs enforce (Referer / Origin / XHR).
CHROME_UA = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/125.0.0.0 Safari/537.36"
)


def _parse_sid_from_refresh_output(raw: str) -> str | None:
    """First line of digits (Lucid sid) or JSON object with key sid."""
    raw = (raw or "").strip()
    if not raw:
        return None
    try:
        data = json.loads(raw)
        sid = data.get("sid")
        if sid:
            return str(sid).strip()
    except (json.JSONDecodeError, TypeError):
        pass
    for line in raw.splitlines():
        line = line.strip()
        if line.isdigit() and len(line) >= 8:
            return line
    return None


def _sync_sid_refresh_hook() -> str | None:
    """
    Run CHARGER_SID_REFRESH_CMD (shell) or GET CHARGER_SID_REFRESH_URL; return new sid.
    Called from thread pool — keep side effects minimal.
    """
    cmd = (os.environ.get("CHARGER_SID_REFRESH_CMD") or "").strip()
    if cmd:
        try:
            proc = subprocess.run(
                cmd,
                shell=True,
                capture_output=True,
                text=True,
                timeout=90,
            )
        except subprocess.TimeoutExpired:
            print("[Session] CHARGER_SID_REFRESH_CMD timed out (90s)")
            return None
        out = proc.stdout or ""
        if proc.returncode != 0:
            tail = (proc.stderr or proc.stdout or "")[-200:]
            print(f"[Session] refresh cmd exit={proc.returncode} tail={tail!r}")
        sid = _parse_sid_from_refresh_output(out)
        if sid:
            return sid

    url = (os.environ.get("CHARGER_SID_REFRESH_URL") or "").strip()
    if url:
        proc = subprocess.run(
            ["curl", "-sS", "--max-time", "25", url],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0:
            print(f"[Session] CHARGER_SID_REFRESH_URL curl failed: {proc.stderr!r}")
        return _parse_sid_from_refresh_output(proc.stdout or "")

    return None


# ── Pure curl HTTP helpers ─────────────────────────────────────────────────────
def _curl_get(url: str, sid: str) -> str:
    """Synchronous curl GET. Returns response body as string."""
    r = subprocess.run([
        "curl", "-k", "-s",
        "--max-time", "10",
        "-H", f"Cookie: sid={sid}",
        "-H", "Accept: application/json",
        "-H", f"Referer: {CHARGER_BASE}/index.html",
        url,
    ], capture_output=True, text=True)
    return r.stdout.strip()

def _curl_post(url: str, sid: str, data: dict) -> str:
    """Synchronous curl POST with form data. Returns response body."""
    form = "&".join(f"{k}={v}" for k, v in data.items())
    r = subprocess.run([
        "curl", "-k", "-s",
        "--max-time", "10",
        "-X", "POST",
        "-H", f"Cookie: sid={sid}",
        "-H", "Accept: application/json, text/plain, */*",
        "-H", f"Referer: {CHARGER_BASE}/index.html",
        "--data", form,
        url,
    ], capture_output=True, text=True)
    return r.stdout.strip()

def _parse_hidden_inputs(html: str) -> dict[str, str]:
    """
    Extract <input type="hidden" ...> name/value pairs (CSRF / session tokens).
    Order is preserved so we can append Username/Password after vendor fields.
    """
    out: dict[str, str] = dict()
    for m in re.finditer(r"<input[^>]*>", html, flags=re.I):
        tag = m.group(0)
        if not re.search(r"type\s*=\s*['\"]?\s*hidden", tag, re.I):
            continue
        nm = re.search(r"name\s*=\s*['\"]([^'\"]+)['\"]", tag, re.I)
        if not nm:
            continue
        vm = re.search(r"value\s*=\s*['\"]([^'\"]*)['\"]", tag, re.I)
        out[nm.group(1)] = vm.group(1) if vm else ""
    return out


def _read_sid_from_cookie_jar(jar_path: str) -> str | None:
    """Read Netscape-format cookie jar written by curl -c (name sid)."""
    try:
        with open(jar_path, encoding="utf-8", errors="ignore") as f:
            for line in f:
                if line.startswith("#") or not line.strip():
                    continue
                parts = line.rstrip("\n").split("\t")
                if len(parts) >= 7 and parts[-2] == "sid":
                    return parts[-1]
    except OSError:
        return None
    return None


def _curl_bootstrap_index(jar_path: str) -> str:
    """GET login shell page like a browser; fills cookie jar (pre-session sid, etc.)."""
    url = f"{CHARGER_BASE}/index.html"
    r = subprocess.run(
        [
            "curl", "-k", "-s", "-S",
            "--max-time", "15",
            "-c", jar_path,
            "-b", jar_path,
            "-H", f"User-Agent: {CHROME_UA}",
            "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", f"Referer: {CHARGER_BASE}/",
            url,
        ],
        capture_output=True,
        text=True,
    )
    html = (r.stdout or "").strip()
    if len(html) < 200:
        # Some firmware serves SPA from "/" only
        r2 = subprocess.run(
            [
                "curl", "-k", "-s", "-S",
                "--max-time", "15",
                "-c", jar_path,
                "-b", jar_path,
                "-H", f"User-Agent: {CHROME_UA}",
                "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "-H", "Accept-Language: en-US,en;q=0.9",
                f"{CHARGER_BASE}/",
            ],
            capture_output=True,
            text=True,
        )
        html = (r2.stdout or html).strip()
    return html


def _curl_post_login_browser(
    login_url: str,
    jar_path: str,
    post_body: str,
    *,
    referer: str,
    content_type: str,
) -> tuple[str, str]:
    """
    POST login.cgi with browser-like headers; -v captures Set-Cookie on stderr.
    """
    r = subprocess.run(
        [
            "curl", "-k", "-s", "-v",
            "--max-time", "15",
            "-b", jar_path,
            "-c", jar_path,
            "-X", "POST",
            "-H", f"User-Agent: {CHROME_UA}",
            "-H", "Accept: application/json, text/javascript, */*; q=0.01",
            "-H", "Accept-Language: en-US,en;q=0.9",
            "-H", f"Referer: {referer}",
            "-H", f"Origin: {CHARGER_BASE}",
            "-H", "X-Requested-With: XMLHttpRequest",
            "-H", f"Content-Type: {content_type}",
            "--data-binary",
            post_body,
            login_url,
        ],
        capture_output=True,
        text=True,
    )
    return (r.stdout or "").strip(), r.stderr or ""


def _login_json_body(user: str, password: str, hidden: dict[str, str]) -> str:
    """Merge hidden token fields into JSON login body (common on SPA chargers)."""
    payload = dict(hidden)
    payload["Username"] = user
    payload["Password"] = password
    return json.dumps(payload, separators=(",", ":"))


def _login_json_password_only(password: str, hidden: dict[str, str]) -> str:
    """Lucid local UI: single Password field — JSON body often omits Username."""
    payload = dict(hidden)
    payload["Password"] = password
    return json.dumps(payload, separators=(",", ":"))


def _login_result_ok(body: str) -> bool:
    """True if JSON indicates success even when sid only appears in Set-Cookie."""
    if not body:
        return False
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        return False
    if data.get("sid"):
        return True
    result = str(data.get("result", "")).lower()
    return result in ("pass", "ok", "success", "accept", "true", "1")


def _curl_login_browser_flow(user: str, password: str) -> tuple[str, str, str]:
    """
    Full browser-style sequence: cookie jar + index bootstrap + XHR-style POST.
    Returns (response_body, curl_stderr, jar_path).
    """
    jar = COOKIE_JAR
    try:
        os.remove(jar)
    except OSError:
        pass

    index_html = _curl_bootstrap_index(jar)
    hidden = _parse_hidden_inputs(index_html)

    referers = (
        f"{CHARGER_BASE}/index.html",
        f"{CHARGER_BASE}/",
        f"{CHARGER_BASE}/home.html",
    )

    # ── Lucid-style: password-only field (no username on login screen) ─────
    pw_only_with_hidden = urllib.parse.urlencode({**dict(hidden), "Password": password})
    for referer in referers:
        body, stderr = _curl_post_login_browser(
            LOGIN_URL,
            jar,
            pw_only_with_hidden,
            referer=referer,
            content_type="application/x-www-form-urlencoded; charset=UTF-8",
        )
        if _login_result_ok(body):
            return body, stderr, jar

    pw_only_minimal = urllib.parse.urlencode({"Password": password})
    for referer in referers:
        body, stderr = _curl_post_login_browser(
            LOGIN_URL,
            jar,
            pw_only_minimal,
            referer=referer,
            content_type="application/x-www-form-urlencoded; charset=UTF-8",
        )
        if _login_result_ok(body):
            return body, stderr, jar

    json_pw = _login_json_password_only(password, hidden)
    for referer in referers:
        body, stderr = _curl_post_login_browser(
            LOGIN_URL,
            jar,
            json_pw,
            referer=referer,
            content_type="application/json; charset=UTF-8",
        )
        if _login_result_ok(body):
            return body, stderr, jar

    json_pw_plain = json.dumps({"Password": password}, separators=(",", ":"))
    for referer in referers:
        body, stderr = _curl_post_login_browser(
            LOGIN_URL,
            jar,
            json_pw_plain,
            referer=referer,
            content_type="application/json; charset=UTF-8",
        )
        if _login_result_ok(body):
            return body, stderr, jar

    # Alternate key spelling seen on some embedded stacks
    for key in ("password", "passwd"):
        alt = urllib.parse.urlencode({**dict(hidden), key: password})
        for referer in referers:
            body, stderr = _curl_post_login_browser(
                LOGIN_URL,
                jar,
                alt,
                referer=referer,
                content_type="application/x-www-form-urlencoded; charset=UTF-8",
            )
            if _login_result_ok(body):
                return body, stderr, jar

    # Form POST: hidden tokens first, then Username + Password (older dual-field UIs).
    form_ordered: dict[str, str] = dict(hidden)
    form_ordered["Username"] = user
    form_ordered["Password"] = password
    form_body = urllib.parse.urlencode(form_ordered)

    for referer in referers:
        body, stderr = _curl_post_login_browser(
            LOGIN_URL,
            jar,
            form_body,
            referer=referer,
            content_type="application/x-www-form-urlencoded; charset=UTF-8",
        )
        if _login_result_ok(body):
            return body, stderr, jar

    # JSON body (some stacks expect application/json from fetch()).
    json_body = _login_json_body(user, password, hidden)
    for referer in referers:
        body, stderr = _curl_post_login_browser(
            LOGIN_URL,
            jar,
            json_body,
            referer=referer,
            content_type="application/json; charset=UTF-8",
        )
        if _login_result_ok(body):
            return body, stderr, jar

    # Lowercase field names (alternate firmware spelling).
    low = dict(hidden)
    low["username"] = user
    low["password"] = password
    low_body = urllib.parse.urlencode(low)
    for referer in referers:
        body, stderr = _curl_post_login_browser(
            LOGIN_URL,
            jar,
            low_body,
            referer=referer,
            content_type="application/x-www-form-urlencoded; charset=UTF-8",
        )
        if _login_result_ok(body):
            return body, stderr, jar

    low_json = json.dumps({**hidden, "username": user, "password": password}, separators=(",", ":"))
    for referer in referers:
        body, stderr = _curl_post_login_browser(
            LOGIN_URL,
            jar,
            low_json,
            referer=referer,
            content_type="application/json; charset=UTF-8",
        )
        if _login_result_ok(body):
            return body, stderr, jar

    # Minimal form without hidden (last resort within browser flow).
    minimal = urllib.parse.urlencode({"Username": user, "Password": password})
    body, stderr = _curl_post_login_browser(
        LOGIN_URL,
        jar,
        minimal,
        referer=f"{CHARGER_BASE}/index.html",
        content_type="application/x-www-form-urlencoded; charset=UTF-8",
    )
    return body, stderr, jar


def _curl_login_legacy_post(url: str, user: str, password: str) -> tuple[str, str]:
    """Original bare POST (no cookie jar / headers) — fallback."""
    r = subprocess.run(
        [
            "curl", "-k", "-s", "-v",
            "--max-time", "10",
            "-X", "POST",
            "--data",
            urllib.parse.urlencode({"Username": user, "Password": password}),
            url,
        ],
        capture_output=True,
        text=True,
    )
    return (r.stdout or "").strip(), r.stderr or ""


def _curl_login_legacy_password_only(url: str, password: str) -> tuple[str, str]:
    """Bare POST with Password field only (Lucid single-field login)."""
    r = subprocess.run(
        [
            "curl", "-k", "-s", "-v",
            "--max-time", "10",
            "-X", "POST",
            "--data",
            urllib.parse.urlencode({"Password": password}),
            url,
        ],
        capture_output=True,
        text=True,
    )
    return (r.stdout or "").strip(), r.stderr or ""


def _curl_login_get(url: str, user: str, password: str) -> tuple[str, str]:
    """Login via GET with credentials as query params (legacy firmware)."""
    qs = urllib.parse.urlencode({"Username": user, "Password": password})
    r = subprocess.run(
        [
            "curl", "-k", "-s", "-v",
            "--max-time", "10",
            "-H", f"User-Agent: {CHROME_UA}",
            "-H", f"Referer: {CHARGER_BASE}/index.html",
            f"{url}?{qs}",
        ],
        capture_output=True,
        text=True,
    )
    return (r.stdout or "").strip(), r.stderr or ""

async def curl_get(url: str, sid: str) -> str:
    """Async wrapper — runs curl in thread pool so FastAPI doesn't block."""
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _curl_get, url, sid)

async def curl_post(url: str, sid: str, data: dict) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _curl_post, url, sid, data)


# ── Session Manager ────────────────────────────────────────────────────────────
class ChargerSession:
    """
    Manages charger HTTP session using curl subprocess.
    - Starts with HARDCODED_SID for immediate operation
    - Tries auto-login every SESSION_TTL seconds
    - Falls back to hardcoded sid if auto-login fails
    """

    SESSION_TTL = 50 * 60   # 50 minutes (before 1hr expiry)

    def __init__(self):
        env_sid = (os.environ.get("CHARGER_SID") or "").strip()
        self.sid = env_sid or HARDCODED_SID
        self.last_login = time.time()
        self._lock = asyncio.Lock()
        self._manual_sid = bool(env_sid) or os.environ.get(
            "CHARGER_MANUAL_SID", ""
        ).strip().lower() in ("1", "true", "yes")
        self._sid_file_mtime = 0.0
        self._sid_file_path = (os.environ.get("CHARGER_SID_FILE") or "").strip()
        self._sid_hook_last_ts = 0.0
        self._sid_hook_cooldown_s = float(
            os.environ.get("CHARGER_SID_HOOK_COOLDOWN_S", "30")
        )
        if env_sid:
            print(f"[Session] Initialized — sid from CHARGER_SID (manual mode)")
        elif self._manual_sid:
            print(f"[Session] Initialized — CHARGER_MANUAL_SID=1 (no auto re-login)")
        else:
            print(f"[Session] Initialized — sid={self.sid}")

    def set_sid_from_browser(self, sid: str) -> None:
        """Apply sid copied from DevTools (Lucid index.html session). Enables manual mode."""
        self.sid = sid.strip()
        self.last_login = time.time()
        self._manual_sid = True
        print(f"[Session] sid updated from browser/API (manual mode, tail …{self.sid[-6:]})")

    def _reload_sid_file_if_changed(self) -> None:
        """If CHARGER_SID_FILE is set, reload sid when the file mtime changes."""
        path = self._sid_file_path
        if not path:
            return
        try:
            st = os.stat(path)
        except OSError:
            return
        if st.st_mtime <= self._sid_file_mtime:
            return
        self._sid_file_mtime = st.st_mtime
        try:
            with open(path, encoding="utf-8", errors="ignore") as handle:
                sid = handle.read().strip()
        except OSError:
            return
        if sid and sid != self.sid:
            self.set_sid_from_browser(sid)

    async def login(self) -> bool:
        """
        Establish a session like the stock web UI: cookie jar, index bootstrap,
        XHR-style POST (and JSON / field-name fallbacks), then legacy curl shapes.
        """
        print("[Session] Attempting login...")
        loop = asyncio.get_event_loop()

        # ── Try 1: Browser-like flow (jar + Referer/Origin/X-Requested-With) ─
        print(f"[Session] Try 1: browser flow — jar={COOKIE_JAR}")
        body, stderr, jar = await loop.run_in_executor(
            None, _curl_login_browser_flow, LOGIN_USER, LOGIN_PASS
        )
        print(f"[Session] Browser body (trunc): {(body[:240])!r}")

        sid = self._extract_sid(body, stderr, jar)
        if sid:
            self.sid = sid
            self.last_login = time.time()
            print(f"[Session] ✅ Login OK (browser) — sid={self.sid}")
            return True
        if _login_result_ok(body):
            sid = self._extract_sid(body, stderr, jar)
            if sid:
                self.sid = sid
            self.last_login = time.time()
            print(f"[Session] ✅ Login OK (browser, result) — sid={self.sid}")
            return True

        # ── Try 2: Legacy POST password-only (Lucid single-field UI) ─────────
        print(f"[Session] Try 2: legacy POST Password-only {LOGIN_URL}")
        body, stderr = await loop.run_in_executor(
            None, _curl_login_legacy_password_only, LOGIN_URL, LOGIN_PASS
        )
        print(f"[Session] Legacy password-only body (trunc): {(body[:240])!r}")
        sid = self._extract_sid(body, stderr, None)
        if sid:
            self.sid = sid
            self.last_login = time.time()
            print(f"[Session] ✅ Login OK (legacy POST password-only) — sid={self.sid}")
            return True

        # ── Try 3: Legacy bare POST Username + Password ─────────────────────
        print(f"[Session] Try 3: legacy POST Username+Password {LOGIN_URL}")
        body, stderr = await loop.run_in_executor(
            None, _curl_login_legacy_post, LOGIN_URL, LOGIN_USER, LOGIN_PASS
        )
        print(f"[Session] Legacy POST body (trunc): {(body[:240])!r}")
        sid = self._extract_sid(body, stderr, None)
        if sid:
            self.sid = sid
            self.last_login = time.time()
            print(f"[Session] ✅ Login OK (legacy POST) — sid={self.sid}")
            return True

        # ── Try 4: GET with query params ─────────────────────────────────────
        print(f"[Session] Try 4: GET {LOGIN_URL}?Username=…&Password=…")
        body, stderr = await loop.run_in_executor(
            None, _curl_login_get, LOGIN_URL, LOGIN_USER, LOGIN_PASS
        )
        print(f"[Session] GET body (trunc): {(body[:240])!r}")
        sid = self._extract_sid(body, stderr, None)
        if sid:
            self.sid = sid
            self.last_login = time.time()
            print(f"[Session] ✅ Login OK (GET) — sid={self.sid}")
            return True

        if _login_result_ok(body):
            sid = self._extract_sid(body, stderr, None)
            if sid:
                self.sid = sid
            self.last_login = time.time()
            print("[Session] ✅ Login OK (GET result) — refreshing session timestamp")
            return True

        print("[Session] ❌ All login attempts failed")
        print(
            "[Session]    → Confirm CHARGER_LOGIN_USER / CHARGER_LOGIN_PASS "
            "(or LOGIN_* in proxy_update.py) match the Lucid web UI"
        )
        print("[Session]    → Or copy sid from DevTools → Cookie and set HARDCODED_SID")
        print(f"[Session]    → Stderr tail: {stderr[-400:]}")
        return False

    def _extract_sid(self, body: str, stderr: str, jar_path: str | None) -> str | None:
        """sid from Set-Cookie (verbose stderr), JSON body, or curl cookie jar."""
        m = re.search(r"[Ss]et-[Cc]ookie:.*?sid=([^;>\s\r\n]+)", stderr)
        if m:
            return m.group(1)
        try:
            data = json.loads(body)
            if data.get("sid"):
                return str(data["sid"])
        except (json.JSONDecodeError, TypeError):
            pass
        if jar_path and _login_result_ok(body):
            jar_sid = _read_sid_from_cookie_jar(jar_path)
            if jar_sid:
                return jar_sid
        return None

    async def ensure_valid(self):
        """Re-login if session age exceeds SESSION_TTL (skipped in manual sid mode)."""
        async with self._lock:
            if self._manual_sid:
                return
            age = time.time() - self.last_login
            if age > self.SESSION_TTL:
                print(f"[Session] Age={int(age)}s — renewing session")
                await self.login()

    async def _maybe_run_sid_refresh_hook(self) -> str | None:
        """
        Optional external sid source when Lucid login.cgi cannot succeed.
        Cooldown avoids hammering a broken hook on every poll tick.
        """
        has_cmd = bool((os.environ.get("CHARGER_SID_REFRESH_CMD") or "").strip())
        has_url = bool((os.environ.get("CHARGER_SID_REFRESH_URL") or "").strip())
        if not has_cmd and not has_url:
            return None
        now = time.time()
        if now - self._sid_hook_last_ts < self._sid_hook_cooldown_s:
            return None
        self._sid_hook_last_ts = now
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, _sync_sid_refresh_hook)

    async def get(self, endpoint: str) -> dict:
        """GET a charger CGI endpoint. Auto-renews session if needed."""
        self._reload_sid_file_if_changed()
        await self.ensure_valid()
        url = f"{CHARGER_BASE}/cgi-bin/{endpoint}"
        text = await curl_get(url, self.sid)

        if not text and not self._manual_sid:
            print("[Session] Empty response — session expired, re-logging in")
            ok = await self.login()
            if ok:
                text = await curl_get(url, self.sid)

        if not text:
            new_sid = await self._maybe_run_sid_refresh_hook()
            if new_sid:
                self.set_sid_from_browser(new_sid)
                text = await curl_get(url, self.sid)

        if not text:
            if self._manual_sid:
                return {
                    "error": f"no_response from {endpoint}",
                    "hint": (
                        "sid expired — POST /api/session/sid, update CHARGER_SID_FILE, "
                        "or configure CHARGER_SID_REFRESH_CMD / CHARGER_SID_REFRESH_URL "
                        "(Lucid will not issue a new sid via curl while login returns reject)."
                    ),
                }
            return {
                "error": f"no_response from {endpoint}",
                "hint": (
                    "Set CHARGER_SID, CHARGER_SID_FILE, POST /api/session/sid, or "
                    "CHARGER_SID_REFRESH_CMD / CHARGER_SID_REFRESH_URL. "
                    "The charger does not expose a working sid refresh without valid login."
                ),
            }
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return {"error": "bad_json", "raw": text[:100]}

    async def post(self, endpoint: str, data: dict | None = None) -> dict:
        """POST to a charger CGI endpoint."""
        if data is None:
            data = dict()
        await self.ensure_valid()
        url = f"{CHARGER_BASE}/cgi-bin/{endpoint}"
        text = await curl_post(url, self.sid, data)
        try:
            return json.loads(text)
        except Exception:
            return {"raw": text}


# ── Globals ────────────────────────────────────────────────────────────────────
session = ChargerSession()

PILOT_PORT = (os.environ.get("PILOT_PORT") or "/dev/ttyACM15").strip()
try:
    pilot = PilotDingus(port=PILOT_PORT)
    PILOT_OK = True
    print(f"[Pilot] Pilot Dingus connected ✅ ({PILOT_PORT})")
except Exception as e:
    print(f"[Pilot] Not available: {e}")
    PILOT_OK = False
    pilot = None

RELAY_OK = False
RELAY_ERR: str | None = None
relay = None
try:
    if (os.environ.get("RELAY_DISABLE") or "").strip().lower() in ("1", "true", "yes"):
        raise RuntimeError("RELAY_DISABLE is set")
    from contactor_relay import ContactorRelay

    relay = ContactorRelay.from_env()
    RELAY_OK = True
    print(f"[Relay] Modbus relay configured → {relay.ip}:{relay.port}")
except Exception as e:
    RELAY_ERR = str(e)
    print(f"[Relay] Not available: {e}")


async def _relay_snapshot() -> dict:
    if not RELAY_OK or relay is None:
        return {"available": False, "error": RELAY_ERR or "relay disabled"}
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, relay.get_status_dict)


# ── Normalizers ────────────────────────────────────────────────────────────────
def normalize_home(d: dict) -> dict:
    if "error" in d:
        return d
    t = int(d.get("Charging_time", 0))
    h, m, s = t // 3600, (t % 3600) // 60, t % 60
    return {
        "name":              d.get("EVSE_name",       "N/A"),
        "status":            d.get("Status",          "N/A"),
        "voltage_v":         float(d.get("Realtime_vol",    0)),
        "current_a":         float(d.get("Realtime_cur",    0)),
        "power_kw":          float(d.get("Realtime_power",  0)),
        "energy_kwh":        float(d.get("Realtime_energy", 0)),
        "max_current_a":     float(d.get("AllowChgCurrent", 0)),
        "charging_time_s":   t,
        "charging_time_fmt": f"{h:02d}h {m:02d}m {s:02d}s",
        "connector_type":    d.get("Connector_Type",  "N/A"),
        "auth_mode":         d.get("CusAuth_mode",    "N/A"),
    }

# Lucid V2H Setup tab — three green checks use bitmask V2H_check_condition (e.g. 7 = 0b111).
LUCID_V2H_CHECK_LABELS = (
    "Connected to the MID",
    "Batteries must be installed in the EVSE",
    "The EVSE must be connected to 240V nominal voltage",
)


def normalize_lucid_v2h_minimal(raw: dict) -> dict:
    """
    Subset of Lucid index.html V2H data: prerequisite checklist + battery SOC/SOH.
    Source: same JSON as curl …/getconfiguration.cgi -b 'sid=…'.
    """
    if "error" in raw:
        return {"error": raw["error"], "hint": raw.get("hint", "")}
    try:
        cond = int(str(raw.get("V2H_check_condition", "0")).strip(), 10)
    except ValueError:
        cond = 0
    checklist = []
    for i, label in enumerate(LUCID_V2H_CHECK_LABELS):
        checklist.append({"label": label, "ok": bool((cond >> i) & 1)})

    def pct(key: str) -> str:
        return str(raw.get(key, "") or "").strip()

    # Lucid V2H Setup page “V2H system status: …” (e.g. Grid Following) — same as V2H_der_op_mode.
    system = str(raw.get("V2H_der_op_mode", "") or "").strip()

    return {
        "check_condition": str(raw.get("V2H_check_condition", "")),
        "checklist": checklist,
        "mid_status": str(raw.get("V2H_mid_status", "") or "").strip(),
        "v2h_system_status": system,
        "soc_pct": pct("V2H_soc"),
        "soh_pct": pct("V2H_soh"),
        "v2h_enabled": raw.get("V2H_enabled", "0") == "1",
    }


def normalize_v2h(d: dict) -> dict:
    if "error" in d:
        return d
    def clean(v): return str(v).strip() if v else "N/A"
    return {
        "ocpp_connected":   clean(d.get("OCPP_Conn_State")),
        "eth_connected":    clean(d.get("Eth_Conn_State")),
        "wifi_connected":   clean(d.get("WIFI_Conn_State")),
        "v2h_enabled":      d.get("V2H_enabled", "0") == "1",
        "v2h_ongoing":      clean(d.get("V2H_ongoing")),
        "v2h_mode":         clean(d.get("V2H_der_op_mode")),
        "v2h_mid_status":   clean(d.get("V2H_mid_status")),
        "v2h_error":        clean(d.get("V2h_error_state")),
        "soc_pct":          clean(d.get("V2H_soc")),
        "soh_pct":          clean(d.get("V2H_soh")),
        "vol_peak_v":       clean(d.get("V2H_vol_peak")),
        "vol_lowest_v":     clean(d.get("V2H_vol_lowest")),
        "vol_nominal_v":    clean(d.get("V2h_vol_nominal")),
        "phase_deg":        clean(d.get("V2H_phase_deg")),
        "power_peak_kw":    clean(d.get("V2H_power_peak")),
        "total_energy_kwh": clean(d.get("V2H_total_energy")),
        "start_time":       clean(d.get("V2H_start_time")),
        "end_time":         clean(d.get("V2H_end_time")),
        "max_install_a":    clean(d.get("MaxCurrent_install")),
        "user_max_a":       clean(d.get("UserMaxCurrent")),
    }

def normalize_pilot(raw: dict, *, hardware_connected: bool) -> dict:
    """
    Shape pilot JSON for the dashboard. When the serial device is open but no
    telemetry line has arrived yet, raw is {} — still report available=True so
    the UI does not show 'undefined' for every field.
    """
    base = {
        "available":     False,
        "mode":          "N/A",
        "state":         "U",
        "high_v":        0.0,
        "low_v":         0.0,
        "duty_pct":      0.0,
        "frequency_hz":  0.0,
        "adv_current":   0.0,
        "state_changes": 0,
    }
    if not hardware_connected:
        base["error"] = "Pilot Dingus not connected"
        return base
    if not raw:
        base["available"] = True
        base["mode"] = "Waiting"
        return base
    return {
        "available":     True,
        "mode":          str(raw.get("mode", "N/A")),
        "state":         str(raw.get("state", "U")),
        "high_v":        round(float(raw.get("high_v", 0)), 2),
        "low_v":         round(float(raw.get("low_v", 0)), 2),
        "duty_pct":      round(float(raw.get("duty", 0)) * 100, 1),
        "frequency_hz":  round(float(raw.get("frequency", 0)), 1),
        "adv_current":   round(float(raw.get("adv_current", 0)), 1),
        "state_changes": raw.get("state_changes", 0),
    }


def normalize_contactor(raw: dict) -> dict:
    """Flatten Modbus snapshot for the dashboard JSON."""
    if not raw.get("available"):
        return {
            "available": False,
            "error":       raw.get("error", "unavailable"),
        }
    c1 = bool(raw.get("c1_grid_closed"))
    c2 = bool(raw.get("c2_ds_closed"))
    c3 = bool(raw.get("c3_dr_closed"))
    violation = bool(c2 and c3)
    return {
        "available":        True,
        "ip":               raw.get("ip"),
        "c1_grid_closed":   c1,
        "c2_ds_closed":     c2,
        "c3_dr_closed":     c3,
        "interlock_violation": violation,
    }


# ── REST API ───────────────────────────────────────────────────────────────────
@app.get("/api/charger/status")
async def api_charger_status():
    return normalize_home(await session.get("home.cgi"))

@app.get("/api/charger/v2h")
async def api_charger_v2h():
    return normalize_v2h(await session.get("getconfiguration.cgi"))


@app.get("/api/lucid/v2h")
async def api_lucid_v2h_minimal():
    """Lucid V2H prerequisite checklist + SOC/SOH only (getconfiguration.cgi)."""
    raw = await session.get("getconfiguration.cgi")
    return normalize_lucid_v2h_minimal(raw)

@app.get("/api/pilot/status")
async def api_pilot_status():
    if not PILOT_OK:
        return {"available": False, "error": "Pilot Dingus not connected"}
    return normalize_pilot(pilot.read_state(), hardware_connected=True)


@app.get("/api/pilot/diag")
async def api_pilot_diag():
    """Raw serial snapshot for debugging (keys the Pico last sent)."""
    if not PILOT_OK or pilot is None:
        return {
            "pilot_ok": False,
            "port": PILOT_PORT,
            "hint": "Check USB, PILOT_PORT, dialout group, and pilot_dingus.py on device.",
        }
    raw = pilot.read_state()
    log_path = pilot.get_log_file()
    log_tail = dict(path=log_path, bytes=0, last_line=None)
    try:
        st = os.stat(log_path)
        log_tail["bytes"] = st.st_size
        with open(log_path, "rb") as fh:
            fh.seek(max(0, st.st_size - 1200))
            chunk = fh.read().decode("utf-8", errors="replace")
        lines = [ln for ln in chunk.splitlines() if ln.strip()]
        if lines:
            log_tail["last_line"] = lines[-1][:600]
    except OSError as exc:
        log_tail["error"] = str(exc)

    return {
        "pilot_ok": True,
        "port": PILOT_PORT,
        "raw_keys": sorted(raw.keys()),
        "raw": raw,
        "log_tail": log_tail,
        "hint": (
            "If log_tail.bytes is 0, nothing is arriving on serial (wrong /dev/ttyACM*, "
            "unplugged Pico, or baud mismatch). If last_line is non-JSON, set PILOT_SERIAL_DEBUG=1 "
            "on the server and watch uvicorn stderr."
        ),
    }

@app.post("/api/pilot/mode/{mode}")
async def api_pilot_mode(mode: str):
    if not PILOT_OK:
        return {"error": "Pilot Dingus not connected"}
    out = pilot.set_mode(mode)
    dashlog.audit(f"Pilot Dingus mode → {mode}")
    return out

@app.get("/api/combined")
async def api_combined():
    home_raw = await session.get("home.cgi")
    v2h_raw = await session.get("getconfiguration.cgi")
    p_data = pilot.read_state() if PILOT_OK else {}
    relay_raw = await _relay_snapshot()
    return {
        "charger":    normalize_home(home_raw),
        "v2h":        normalize_v2h(v2h_raw),
        "lucid_v2h":  normalize_lucid_v2h_minimal(v2h_raw),
        "pilot":      normalize_pilot(p_data, hardware_connected=PILOT_OK),
        "contactor":  normalize_contactor(relay_raw),
        "timestamp":  datetime.now().isoformat(),
    }


@app.get("/api/contactor/status")
async def api_contactor_status():
    return normalize_contactor(await _relay_snapshot())


@app.post("/api/contactor/channel/{which}")
async def api_contactor_set_channel(which: int, payload: dict):
    """which: 1=C1 grid, 2=C2 DER_V2H, 3=C3 DER — body {\"on\": true} means Connect."""
    if not RELAY_OK or relay is None:
        return JSONResponse({"error": "relay unavailable"}, status_code=503)
    if which not in (1, 2, 3):
        return JSONResponse({"error": "channel must be 1, 2, or 3"}, status_code=400)
    ch = {1: relay.c1, 2: relay.c2, 3: relay.c3}[which]
    on = bool(payload.get("on"))
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, relay.set_channel, ch, on)
    except ValueError as exc:
        return JSONResponse(
            {"error": str(exc), "interlock": True},
            status_code=409,
        )
    dashlog.audit(f"Contactor C{which} → {'Connect' if on else 'Disconnect'}")
    return normalize_contactor(await _relay_snapshot())


@app.post("/api/contactor/initial")
async def api_contactor_initial():
    """Preset: C1 Connect, C2 Disconnect, C3 Connect."""
    if not RELAY_OK or relay is None:
        return JSONResponse({"error": "relay unavailable"}, status_code=503)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, relay.set_initial_state)
    except ValueError as exc:
        return JSONResponse(
            {"error": str(exc), "interlock": True},
            status_code=409,
        )
    dashlog.audit("Contactor initial: C1 Connect · C2 Disconnect · C3 Connect")
    return normalize_contactor(await _relay_snapshot())


@app.post("/api/contactor/v2h-sequence")
async def api_contactor_v2h_sequence():
    """C1 disconnect → wait → C3 disconnect → wait → C2 connect (blocking several seconds)."""
    if not RELAY_OK or relay is None:
        return JSONResponse({"error": "relay unavailable"}, status_code=503)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, relay.run_v2h_sequence)
    except ValueError as exc:
        return JSONResponse(
            {"error": str(exc), "interlock": True},
            status_code=409,
        )
    dashlog.audit(
        "Contactor V2H sequence complete (C1 disconnect → C3 disconnect → C2 connect)"
    )
    return normalize_contactor(await _relay_snapshot())


@app.post("/api/contactor/grid-return-sequence")
async def api_contactor_grid_return_sequence():
    """C1 connect → C2 disconnect → C3 connect (allows transient C1+C2 during automation)."""
    if not RELAY_OK or relay is None:
        return JSONResponse({"error": "relay unavailable"}, status_code=503)
    loop = asyncio.get_event_loop()
    try:
        await loop.run_in_executor(None, relay.run_grid_return_sequence)
    except ValueError as exc:
        return JSONResponse(
            {"error": str(exc), "interlock": True},
            status_code=409,
        )
    dashlog.audit(
        "Contactor Grid return sequence complete (C1 connect → C2 disconnect → C3 connect)"
    )
    return normalize_contactor(await _relay_snapshot())


@app.get("/api/logs/unified/status")
async def api_logs_unified_status():
    return dashlog.unified_status()


@app.post("/api/logs/unified/start")
async def api_logs_unified_start(payload: dict):
    user = (payload.get("username") or payload.get("user") or "").strip()
    host = (payload.get("host") or "").strip()
    password = payload.get("password")
    if password is not None and not isinstance(password, str):
        password = str(password)
    mf = payload.get("modbus_filters")
    if isinstance(mf, str):
        mf = [s.strip() for s in mf.split(",") if s.strip()]
    elif mf is not None and not isinstance(mf, list):
        mf = None
    if not user or not host or not password:
        return JSONResponse(
            {"error": "username, host, and password are required"},
            status_code=400,
        )
    out = dashlog.start_unified(user, host, password, mf)
    if not out.get("ok"):
        return JSONResponse(out, status_code=400)
    return out


@app.post("/api/logs/unified/stop")
async def api_logs_unified_stop():
    return dashlog.stop_unified()


@app.get("/api/logs/unified/download")
async def api_logs_unified_download(session_id: str, rel: str):
    try:
        path = dashlog.safe_session_download(session_id, rel)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return FileResponse(path, filename=path.name)


@app.get("/api/logs/uart/status")
async def api_logs_uart_status():
    return dashlog.uart_status()


@app.post("/api/logs/uart/start")
async def api_logs_uart_start(payload: dict):
    user = (payload.get("username") or payload.get("user") or "").strip()
    host = (payload.get("host") or "").strip()
    password = payload.get("password")
    if password is not None and not isinstance(password, str):
        password = str(password)
    strings = payload.get("strings")
    if isinstance(strings, str):
        strings = [ln.strip() for ln in strings.splitlines() if ln.strip()]
    elif isinstance(strings, list):
        strings = [str(s).strip() for s in strings if str(s).strip()]
    else:
        strings = []
    if not user or not host or not password:
        return JSONResponse(
            {"error": "username, host, and password are required"},
            status_code=400,
        )
    out = dashlog.start_uart_capture(user, host, password, strings)
    if not out.get("ok"):
        return JSONResponse(out, status_code=400)
    return out


@app.post("/api/logs/uart/stop")
async def api_logs_uart_stop():
    return dashlog.stop_uart_capture()


@app.get("/api/logs/uart/download")
async def api_logs_uart_download(
    which: str | None = Query(default=None),
    path: str | None = Query(default=None),
):
    """
    ``which`` = log | capture (first module while running).
    Or pass absolute ``path`` from /api/logs/uart/status file list (must be under capture root).
    """
    try:
        dashlog.flush_uart_open_files()
        if path:
            pth = dashlog.uart_resolve_existing_file(path)
        elif which in ("log", "capture"):
            pth = dashlog.uart_pick_download(which)
        else:
            return JSONResponse(
                {"error": "Provide query which=log|capture or path=<absolute path from status>"},
                status_code=400,
            )
    except PermissionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=403)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return FileResponse(pth, filename=pth.name)


@app.get("/api/dashboard/audit")
async def api_dashboard_audit():
    return dashlog.audit_entries()


@app.get("/api/dashboard/audit/download")
async def api_dashboard_audit_download():
    return PlainTextResponse(
        dashlog.audit_text(),
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": 'attachment; filename="dashboard_actions.log"',
        },
    )


@app.post("/api/dashboard/audit/log")
async def api_dashboard_audit_client(payload: dict):
    msg = (payload.get("message") or payload.get("msg") or "").strip()
    if msg:
        dashlog.audit(f"[browser] {msg}")
    return {"ok": True}


@app.get("/api/session")
async def api_session_info():
    age = int(time.time() - session.last_login)
    return {
        "sid":          session.sid,
        "age_s":        age,
        "expires_in":   max(0, session.SESSION_TTL - age),
        "ttl":          session.SESSION_TTL,
        "manual_mode":  session._manual_sid,
        "sid_file":     session._sid_file_path or None,
    }


@app.post("/api/session/sid")
async def api_session_set_sid(
    payload: dict,
    authorization: str | None = Header(default=None),
):
    """
    Push a fresh Lucid `sid` (from browser DevTools Cookie) without restarting.
    Optional: set CHARGER_SID_TOKEN and send header Authorization: Bearer <token>.
    """
    token = (os.environ.get("CHARGER_SID_TOKEN") or "").strip()
    if token and (authorization or "").strip() != f"Bearer {token}":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    sid = str(payload.get("sid", "")).strip()
    if not sid:
        return JSONResponse(
            {"error": "missing_sid", "hint": 'Body: {"sid": "177788538300"}'},
            status_code=400,
        )
    session.set_sid_from_browser(sid)
    return {"ok": True, "manual_mode": True}


@app.post("/api/session/refresh")
async def api_session_refresh():
    """Manually trigger re-login — useful for testing."""
    ok = await session.login()
    return {"success": ok, "sid": session.sid}

@app.post("/api/charger/start")
async def api_charger_start():
    # TODO: update endpoint once Start button CGI is captured from browser
    result = await session.post("setcommand.cgi", {"cmd": "start"})
    return {"action": "start", "result": result}

@app.post("/api/charger/stop")
async def api_charger_stop():
    result = await session.post("setcommand.cgi", {"cmd": "stop"})
    return {"action": "stop", "result": result}


# ── WebSocket ──────────────────────────────────────────────────────────────────
ws_clients: list = []

@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    ws_clients.append(ws)
    print(f"[WS] Client connected ({len(ws_clients)} total)")
    try:
        while True:
            await asyncio.sleep(1)
            try:
                data = await api_combined()
                await ws.send_json({"type": "update", **data})
            except (WebSocketDisconnect, ClientDisconnected):
                raise
            except RuntimeError as exc:
                # Client closed tab mid-send; do not call send again.
                if "send" in str(exc).lower() and "close" in str(exc).lower():
                    break
                raise
            except Exception as exc:
                try:
                    await ws.send_json({"type": "error", "msg": str(exc)})
                except Exception:
                    break
    except WebSocketDisconnect:
        pass
    finally:
        if ws in ws_clients:
            ws_clients.remove(ws)
        print(f"[WS] Client disconnected ({len(ws_clients)} total)")


# ── Startup ────────────────────────────────────────────────────────────────────
@app.on_event("startup")
async def on_startup():
    print(f"[Startup] Charger  : {CHARGER_BASE}")
    print(f"[Startup] Pilot    : {PILOT_PORT}")
    if RELAY_OK and relay is not None:
        print(f"[Startup] Relay    : {relay.ip}:{relay.port} (C1={relay.c1} C2={relay.c2} C3={relay.c3})")
    else:
        print(f"[Startup] Relay    : off ({RELAY_ERR or 'RELAY_DISABLE'})")
    print(f"[Startup] sid      : {session.sid}")
    if (os.environ.get("CHARGER_SID_REFRESH_CMD") or "").strip() or (
        os.environ.get("CHARGER_SID_REFRESH_URL") or ""
    ).strip():
        print("[Startup] sid auto-refresh: CHARGER_SID_REFRESH_CMD or _URL is set")
    print("[Startup] Testing charger connection...")
    data = await session.get("home.cgi")
    if "error" not in data:
        print(f"[Startup] ✅ Charger OK — status={data.get('status')} voltage={data.get('voltage_v')}V")
    else:
        print(f"[Startup] ❌ Charger error: {data}")
        print("[Startup]    → Lucid: copy Cookie sid from DevTools, then either:")
        print("[Startup]       export CHARGER_SID='…' CHARGER_MANUAL_SID=1  # or")
        print("[Startup]       curl -sS -X POST http://127.0.0.1:8000/api/session/sid \\")
        print("[Startup]         -H 'Content-Type: application/json' -d '{\"sid\":\"…\"}'")
    if not session._manual_sid:
        print("[Startup] Attempting auto-login for fresh sid...")
        await session.login()
    else:
        print("[Startup] Manual sid mode — skipping auto-login")
    print("[Startup] ✅ Ready — open http://10.91.0.98:8000 in browser")


# ── Dashboard ──────────────────────────────────────────────────────────────────
@app.get("/", response_class=HTMLResponse)
async def dashboard():
    try:
        with open("index.html") as f:
            return HTMLResponse(f.read())
    except FileNotFoundError:
        return HTMLResponse("""
        <html><body style="font-family:monospace;padding:40px;background:#0d1117;color:#e6edf3">
        <h2 style="color:#f85149">index.html not found</h2>
        <p>Place <code>index.html</code> in the same folder as <code>proxy.py</code></p>
        <p>API endpoints working:</p>
        <ul>
          <li><a href="/api/charger/status" style="color:#58a6ff">/api/charger/status</a></li>
          <li><a href="/api/charger/v2h"    style="color:#58a6ff">/api/charger/v2h</a></li>
          <li><a href="/api/pilot/status"   style="color:#58a6ff">/api/pilot/status</a></li>
          <li><a href="/api/contactor/status" style="color:#58a6ff">/api/contactor/status</a></li>
          <li><a href="/api/combined"       style="color:#58a6ff">/api/combined</a></li>
          <li><a href="/api/session"        style="color:#58a6ff">/api/session</a></li>
        </ul>
        </body></html>
        """)
