#!/usr/bin/env python3
"""
Waveshare 16-ch Modbus TCP relay — contactor helpers for the EV Lab proxy.

Uses the same coil mapping as contactor_control.py:
  C1 ch1 Grid, C2 ch2 DER_V2H, C3 ch3 DER

C1 and C3 default to **normally-closed** wiring (RELAY_C1_NC / RELAY_C3_NC).
C2 defaults to direct coil↔contact mapping (set RELAY_C2_NC later if needed).

Install: pip install pymodbus
"""

import inspect
import os
import threading
import time
from typing import Any

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    ModbusTcpClient = None  # type: ignore[misc, assignment]

# Modbus unit id for Waveshare relay (override with RELAY_MODBUS_UNIT env).
_DEFAULT_UNIT = int(os.environ.get("RELAY_MODBUS_UNIT", "1"))


def _env_bool(name: str, default_when_unset: str) -> bool:
    return (os.environ.get(name) or default_when_unset).strip().lower() in (
        "1",
        "true",
        "yes",
    )


# Lab wiring: C1 and C3 use NC contacts → effective CLOSED ⇔ coil de-energized (raw False).
# C2 assumed NO-style (effective follows raw coil). Override with RELAY_C1_NC=0 / RELAY_C3_NC=0.
_RELAY_C1_NC = _env_bool("RELAY_C1_NC", "1")
_RELAY_C3_NC = _env_bool("RELAY_C3_NC", "1")


def _unit_kwargs(client: Any, method_name: str) -> dict[str, Any]:
    """
    pymodbus API differs by version: slave=, unit=, device_id=, or none (implicit 1).
    """
    meth = getattr(client, method_name)
    names = set(inspect.signature(meth).parameters)
    for key in ("slave", "device_id", "unit"):
        if key in names:
            return {key: _DEFAULT_UNIT}
    return dict()


def _write_coil_compat(client: Any, addr: int, value: bool) -> Any:
    kw = _unit_kwargs(client, "write_coil")
    for attempt in (
        lambda: client.write_coil(address=addr, value=value, **kw),
        lambda: client.write_coil(addr, value, **kw),
        lambda: client.write_coil(addr, value),
    ):
        try:
            return attempt()
        except TypeError:
            continue
    raise TypeError("write_coil: incompatible pymodbus signature")


def _read_coils_compat(client: Any, addr: int, count: int = 1) -> Any:
    kw = _unit_kwargs(client, "read_coils")
    for attempt in (
        lambda: client.read_coils(address=addr, count=count, **kw),
        lambda: client.read_coils(addr, count, **kw),
        lambda: client.read_coils(addr, count),
    ):
        try:
            return attempt()
        except TypeError:
            continue
    raise TypeError("read_coils: incompatible pymodbus signature")


def validate_c2_c3_interlock(e2: bool, e3: bool) -> None:
    """C2 and C3 must never both be effectively connected."""
    if e2 and e3:
        raise ValueError(
            "Interlock: C2 (DER_V2H) and C3 (DER) cannot both be connected."
        )


def validate_c1_c2_manual(prev_e1: bool, prev_e2: bool, new_e1: bool, new_e2: bool) -> None:
    """
    Manual single-channel rules (not used inside automated Grid return sequence).
    Blocks stepping into both C1+C2 connected via one new closure while the other leg
    was already alone-on.
    """
    if not (new_e1 and new_e2):
        return
    if prev_e1 and not prev_e2 and new_e2 and not prev_e2:
        raise ValueError(
            "Interlock: cannot connect C2 while C1 is already connected (disconnect C1 first, "
            "or use Grid return sequence)."
        )
    if not prev_e1 and prev_e2 and new_e1 and not prev_e1:
        raise ValueError(
            "Interlock: cannot connect C1 while C2 is already connected (disconnect C2 first)."
        )


def validate_effective_state(
    e1: bool,
    e2: bool,
    e3: bool,
    prev_e1: bool | None,
    prev_e2: bool | None,
    *,
    check_c1_c2_manual_rules: bool,
) -> None:
    validate_c2_c3_interlock(e2, e3)
    if (
        check_c1_c2_manual_rules
        and prev_e1 is not None
        and prev_e2 is not None
    ):
        validate_c1_c2_manual(prev_e1, prev_e2, e1, e2)


class ContactorRelay:
    """Thread-safe Modbus TCP access for three contactor coils."""

    def __init__(
        self,
        ip: str,
        port: int,
        c1: int,
        c2: int,
        c3: int,
        step_delay: float,
    ) -> None:
        if ModbusTcpClient is None:
            raise RuntimeError("pymodbus is not installed (pip install pymodbus)")
        self.ip = ip
        self.port = int(port)
        self.c1 = int(c1)
        self.c2 = int(c2)
        self.c3 = int(c3)
        self.step_delay = float(step_delay)
        self.pre_step_delay = float(
            os.environ.get("RELAY_SEQUENCE_PRE_DELAY_S", "1")
        )
        self._lock = threading.Lock()
        self._client: ModbusTcpClient | None = None
        self._c1_nc = _RELAY_C1_NC
        self._c3_nc = _RELAY_C3_NC

    def _effective_from_raw(self, channel: int, raw: bool) -> bool:
        """Map coil read to effective path connected (dashboard: Connect); NC may invert."""
        ch = int(channel)
        raw_b = bool(raw)
        if ch == int(self.c1) and self._c1_nc:
            return not raw_b
        if ch == int(self.c3) and self._c3_nc:
            return not raw_b
        return raw_b

    def _raw_for_effective(self, channel: int, effective_closed: bool) -> bool:
        """Raw coil write so ``effective_closed`` matches operator Connect."""
        ch = int(channel)
        eff = bool(effective_closed)
        if ch == int(self.c1) and self._c1_nc:
            return not eff
        if ch == int(self.c3) and self._c3_nc:
            return not eff
        return eff

    def _effective_triple(self, r1: bool, r2: bool, r3: bool) -> tuple[bool, bool, bool]:
        return (
            self._effective_from_raw(self.c1, r1),
            self._effective_from_raw(self.c2, r2),
            self._effective_from_raw(self.c3, r3),
        )

    @classmethod
    def from_env(cls) -> "ContactorRelay":
        return cls(
            ip=(os.environ.get("RELAY_IP") or "10.91.0.201").strip(),
            port=int(os.environ.get("RELAY_PORT", "502")),
            c1=int(os.environ.get("RELAY_C1", "1")),
            c2=int(os.environ.get("RELAY_C2", "2")),
            c3=int(os.environ.get("RELAY_C3", "3")),
            step_delay=float(os.environ.get("RELAY_STEP_DELAY", "2")),
        )

    def _connect_unlocked(self) -> bool:
        if self._client is not None:
            try:
                if self._client.connected:
                    return True
            except Exception:
                pass
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        self._client = ModbusTcpClient(self.ip, port=self.port)
        return bool(self._client.connect())

    def ensure_connected(self) -> None:
        with self._lock:
            if not self._connect_unlocked():
                raise ConnectionError(f"Modbus TCP {self.ip}:{self.port}")

    def close(self) -> None:
        with self._lock:
            if self._client is not None:
                try:
                    self._client.close()
                except Exception:
                    pass
                self._client = None

    def _write_coil(self, channel: int, state: bool) -> None:
        addr = int(channel) - 1
        assert self._client is not None
        r = _write_coil_compat(self._client, addr, bool(state))
        if hasattr(r, "isError") and r.isError():
            raise RuntimeError(f"write_coil failed: {r}")

    def _read_coil(self, channel: int) -> bool:
        addr = int(channel) - 1
        assert self._client is not None
        r = _read_coils_compat(self._client, addr, 1)
        if hasattr(r, "isError") and r.isError():
            raise RuntimeError(f"read_coils failed: {r}")
        return bool(r.bits[0])

    def set_channel(self, channel: int, state: bool) -> None:
        """``state`` is effective path connected (API JSON ``on``: true = Connect)."""
        with self._lock:
            self._connect_unlocked()
            raw1 = self._read_coil(self.c1)
            raw2 = self._read_coil(self.c2)
            raw3 = self._read_coil(self.c3)
            ch = int(channel)
            prev_e1, prev_e2, prev_e3 = self._effective_triple(raw1, raw2, raw3)
            if ch == self.c1:
                new_e1, new_e2, new_e3 = bool(state), prev_e2, prev_e3
            elif ch == self.c2:
                new_e1, new_e2, new_e3 = prev_e1, bool(state), prev_e3
            elif ch == self.c3:
                new_e1, new_e2, new_e3 = prev_e1, prev_e2, bool(state)
            else:
                raise ValueError(f"Unknown relay channel coil index {channel}")
            validate_effective_state(
                new_e1,
                new_e2,
                new_e3,
                prev_e1,
                prev_e2,
                check_c1_c2_manual_rules=True,
            )
            raw_out = self._raw_for_effective(ch, bool(state))
            self._write_coil(ch, raw_out)
            time.sleep(0.2)

    def read_channel(self, channel: int) -> bool:
        with self._lock:
            self._connect_unlocked()
            return self._read_coil(channel)

    def get_status_dict(self) -> dict[str, Any]:
        """Snapshot for JSON API."""
        try:
            with self._lock:
                if not self._connect_unlocked():
                    return {
                        "available": False,
                        "error": f"no_tcp:{self.ip}:{self.port}",
                    }
                r1 = self._read_coil(self.c1)
                r2 = self._read_coil(self.c2)
                r3 = self._read_coil(self.c3)
                e1, e2, e3 = self._effective_triple(r1, r2, r3)
            return {
                "available": True,
                "ip": self.ip,
                "c1_grid_closed": e1,
                "c2_ds_closed": e2,
                "c3_dr_closed": e3,
            }
        except Exception as exc:
            return {"available": False, "error": str(exc)}

    def set_initial_state(self) -> dict[str, Any]:
        """
        Sequence: C1 connect → C2 disconnect → C3 connect (lab initial operating point).
        C1/C2 manual interlock is not enforced between these internal steps (transient C1+C2 is
        allowed); C2+C3 is always enforced. Raw coils use RELAY_C1_NC / RELAY_C3_NC for NC channels.
        """
        with self._lock:
            if not self._connect_unlocked():
                return {"ok": False, "error": "connect failed", "available": False}
            pre = self.pre_step_delay

            def _snap_effective() -> tuple[bool, bool, bool]:
                return self._effective_triple(
                    self._read_coil(self.c1),
                    self._read_coil(self.c2),
                    self._read_coil(self.c3),
                )

            # Step 1 — C1 connect
            pe1, pe2, pe3 = _snap_effective()
            time.sleep(pre)
            self._write_coil(self.c1, self._raw_for_effective(self.c1, True))
            time.sleep(0.2)
            e1, e2, e3 = _snap_effective()
            validate_effective_state(
                e1, e2, e3, pe1, pe2, check_c1_c2_manual_rules=False,
            )
            # Step 2 — C2 disconnect
            pe1, pe2, pe3 = e1, e2, e3
            time.sleep(pre)
            self._write_coil(self.c2, self._raw_for_effective(self.c2, False))
            time.sleep(0.2)
            e1, e2, e3 = _snap_effective()
            validate_effective_state(
                e1, e2, e3, pe1, pe2, check_c1_c2_manual_rules=False,
            )
            # Step 3 — C3 connect
            pe1, pe2, pe3 = e1, e2, e3
            time.sleep(pre)
            self._write_coil(self.c3, self._raw_for_effective(self.c3, True))
            time.sleep(0.2)
            e1, e2, e3 = _snap_effective()
            validate_effective_state(
                e1, e2, e3, pe1, pe2, check_c1_c2_manual_rules=False,
            )
        return {
            "ok": True,
            "available": True,
            "ip": self.ip,
            "c1_grid_closed": e1,
            "c2_ds_closed": e2,
            "c3_dr_closed": e3,
        }

    def run_v2h_sequence(self) -> dict[str, Any]:
        """Effective steps: C1 open → C3 open → C2 closed (V2H handoff sequence)."""
        with self._lock:
            if not self._connect_unlocked():
                return {"ok": False, "error": "connect failed", "available": False}
            pre = self.pre_step_delay
            d = self.step_delay

            def _snap_effective() -> tuple[bool, bool, bool]:
                return self._effective_triple(
                    self._read_coil(self.c1),
                    self._read_coil(self.c2),
                    self._read_coil(self.c3),
                )

            pe1, pe2, pe3 = _snap_effective()
            validate_effective_state(
                pe1,
                pe2,
                pe3,
                None,
                None,
                check_c1_c2_manual_rules=True,
            )
            time.sleep(pre)
            self._write_coil(self.c1, self._raw_for_effective(self.c1, False))
            time.sleep(d)
            e1, e2, e3 = _snap_effective()
            validate_effective_state(
                e1, e2, e3, pe1, pe2, check_c1_c2_manual_rules=True,
            )
            pe1, pe2, pe3 = e1, e2, e3
            time.sleep(pre)
            self._write_coil(self.c3, self._raw_for_effective(self.c3, False))
            time.sleep(d)
            e1, e2, e3 = _snap_effective()
            validate_effective_state(
                e1, e2, e3, pe1, pe2, check_c1_c2_manual_rules=True,
            )
            pe1, pe2, pe3 = e1, e2, e3
            time.sleep(pre)
            self._write_coil(self.c2, self._raw_for_effective(self.c2, True))
            time.sleep(d)
            e1, e2, e3 = _snap_effective()
            validate_effective_state(
                e1, e2, e3, pe1, pe2, check_c1_c2_manual_rules=True,
            )
        return {
            "ok": True,
            "available": True,
            "ip": self.ip,
            "c1_grid_closed": e1,
            "c2_ds_closed": e2,
            "c3_dr_closed": e3,
        }

    def run_grid_return_sequence(self) -> dict[str, Any]:
        """
        Grid return: C1 connect → C2 disconnect → C3 connect.
        Manual C1/C2 overlap rules are not applied between internal steps (transient C1+C2 allowed).
        """
        with self._lock:
            if not self._connect_unlocked():
                return {"ok": False, "error": "connect failed", "available": False}
            pre = self.pre_step_delay

            def _snap_effective() -> tuple[bool, bool, bool]:
                return self._effective_triple(
                    self._read_coil(self.c1),
                    self._read_coil(self.c2),
                    self._read_coil(self.c3),
                )

            # Step 1 — C1 connect
            pe1, pe2, pe3 = _snap_effective()
            time.sleep(pre)
            self._write_coil(self.c1, self._raw_for_effective(self.c1, True))
            time.sleep(0.2)
            e1, e2, e3 = _snap_effective()
            validate_effective_state(
                e1, e2, e3, pe1, pe2, check_c1_c2_manual_rules=False,
            )
            # Step 2 — C2 disconnect
            pe1, pe2, pe3 = e1, e2, e3
            time.sleep(pre)
            self._write_coil(self.c2, self._raw_for_effective(self.c2, False))
            time.sleep(0.2)
            e1, e2, e3 = _snap_effective()
            validate_effective_state(
                e1, e2, e3, pe1, pe2, check_c1_c2_manual_rules=False,
            )
            # Step 3 — C3 connect
            pe1, pe2, pe3 = e1, e2, e3
            time.sleep(pre)
            self._write_coil(self.c3, self._raw_for_effective(self.c3, True))
            time.sleep(0.2)
            e1, e2, e3 = _snap_effective()
            validate_effective_state(
                e1, e2, e3, pe1, pe2, check_c1_c2_manual_rules=False,
            )
        return {
            "ok": True,
            "available": True,
            "ip": self.ip,
            "c1_grid_closed": e1,
            "c2_ds_closed": e2,
            "c3_dr_closed": e3,
        }
