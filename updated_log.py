#!/usr/bin/env python3
"""
unified_logger.py — CAN Bus + Modbus Unified Log Capture Tool
═══════════════════════════════════════════════════════════════
SSH into a lab cell and simultaneously capture:
  • CAN bus capture  via spanbus logger (PEAK PCAN-USB FD)
  • Modbus logs      via picocom on /dev/ttyUSB* (8-byte RTU frames)

Output folder layout
────────────────────
  <output_dir>/<user>@<host>/
    canbus_capture_<timestamp>.txt  <- filtered: MID_RELAY / DER / NFT states
    modbus_logs_<timestamp>.txt      <- all timestamped RTU frames

Usage
-----
  python3 unified_logger.py capture -h 10.91.0.96 -u systems
  python3 unified_logger.py capture -h 10.91.0.96 -u systems -o ./lab_logs
  python3 unified_logger.py capture -h 10.91.0.96 -u systems --duration 120

Press Ctrl+C to stop both capture streams at any time.
"""

from __future__ import annotations

import argparse
import datetime
import getpass
import re
import signal
import sys
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

import paramiko

# =========================================================================
# CONFIGURATION  -- adjust if your remote environment differs
# =========================================================================
PCAN_DEVICE_STRING = "PEAK System PCAN-USB FD"
SPANBUS_COMMAND    = "spanbus logger -i socketcan -d can0"
MODBUS_BAUD        = 115200
SSH_PORT           = 22
SSH_TIMEOUT        = 10   # seconds for initial connection

# CAN payload types that are written to the filtered capture file
CAN_FILTER_TYPES = [
    "PAYLOAD_MID_RELAY_STATE",
    "PAYLOAD_DER_LANDED_CIRCUIT_CONTROL_STATE",
    "PAYLOAD_NFT_LANDED_CIRCUIT_CONTROL_STATE",
]


# =========================================================================
# TIMESTAMP HELPERS
# =========================================================================

def ts() -> str:
    """YYYY-MM-DD HH:MM:SS  -- used in log lines"""
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def ts_file() -> str:
    """YYYYMMDD_HHMMSS  -- used in file names"""
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


# =========================================================================
# SSH HELPERS
# =========================================================================

def _sh(s: str) -> str:
    """Wrap a string in single quotes safe for:  bash -lc '<s>'"""
    return "'" + s.replace("'", "'\\''") + "'"


def connect_ssh(username: str, host: str, password: str) -> paramiko.SSHClient:
    """Open and return an authenticated paramiko SSH connection."""
    print(f"\n[*] Connecting to {username}@{host}:{SSH_PORT} ...")
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            hostname=host, port=SSH_PORT,
            username=username, password=password,
            timeout=SSH_TIMEOUT,
        )
        print(f"[OK] Connected to {host}")
        return client
    except paramiko.AuthenticationException as exc:
        raise ConnectionError(
            "Authentication failed. Check your username / password."
        ) from exc
    except Exception as exc:
        raise ConnectionError(f"SSH connection error: {exc}") from exc


def run_remote(client: paramiko.SSHClient, command: str) -> str:
    """
    Run a command on the remote host inside a LOGIN shell and return
    combined stdout + stderr as a decoded string.

    KEY FIX: wrapping in  bash -lc '...'  sources /etc/profile and
    ~/.bashrc so tools installed outside /usr/bin (spanbus, pio,
    picocom ...) are on PATH even though paramiko does NOT start a
    login shell by default.
    """
    login_cmd = f"bash -lc {_sh(command)}"
    _, stdout, stderr = client.exec_command(login_cmd)
    return stdout.read().decode(errors="replace") + stderr.read().decode(errors="replace")


def resolve_tool(client: paramiko.SSHClient, name: str) -> Optional[str]:
    """
    Locate `name` on the remote host with `which`.
    Returns the full path, or None with a printed warning if not found.
    """
    path = run_remote(client, f"which {name} 2>/dev/null").strip()
    if path and not path.startswith("bash:") and not path.startswith("which:"):
        return path
    print(f"[!] WARNING: '{name}' not found on remote PATH.")
    return None


def open_channel(client: paramiko.SSHClient, command: str) -> paramiko.Channel:
    """
    Open a new paramiko PTY channel and run `command` inside a login
    shell so the remote user's full PATH is loaded.
    """
    transport = client.get_transport()
    channel   = transport.open_session()
    channel.get_pty(term="dumb", width=220, height=24)
    channel.exec_command(f"bash -lc {_sh(command)}")
    return channel


# =========================================================================
# CAN CAPTURE
# =========================================================================

def detect_pcan(client: paramiko.SSHClient) -> bool:
    """Return True if a PCAN-USB FD device appears in lsusb output."""
    print("\n[*] Checking for PCAN device via lsusb ...")
    output = run_remote(client, "lsusb")
    found  = False
    for line in output.splitlines():
        print(f"    {line}")
        if PCAN_DEVICE_STRING.lower() in line.lower():
            print(f"[OK] PCAN detected: {line.strip()}")
            found = True
    return found


def capture_can(
    client:       paramiko.SSHClient,
    capture_path: str,
    username:     str,
    host:         str,
    stop_event:   threading.Event,
) -> None:
    """
    Stream spanbus logger output from the remote host to one file:
      • capture_path -- only lines matching CAN_FILTER_TYPES (filtered capture)

    Each output line is prefixed with a local timestamp before writing.
    """
    print(f"[CAN ] Filtered capture -> {capture_path}")
    print(f"[CAN ] Active filters   : {CAN_FILTER_TYPES}")

    # Verify spanbus is available (with helpful error if not)
    if not resolve_tool(client, "spanbus"):
        print(f"[CAN ] 'spanbus' not found on remote host -- CAN capture aborted.")
        print(f"[CAN ] Confirm spanbus is installed and on PATH for the login shell.")
        with open(capture_path, "w") as fh:
            fh.write("=== ERROR: spanbus not found on remote host ===\n")
        return

    channel = open_channel(client, SPANBUS_COMMAND)

    with open(capture_path, "w", buffering=1, encoding="utf-8") as cap_fh:
        # ── Header ───────────────────────────────────────────────────────
        cap_fh.write("CAN Bus Filtered Capture\n")
        cap_fh.write(f"Host    : {username}@{host}\n")
        cap_fh.write(f"Command : {SPANBUS_COMMAND}\n")
        cap_fh.write(f"Started : {datetime.datetime.now().isoformat()}\n")
        cap_fh.write(f"Filters : {', '.join(CAN_FILTER_TYPES)}\n")
        cap_fh.write("=" * 60 + "\n")

        # ── Streaming read loop ───────────────────────────────────────────
        while not stop_event.is_set():
            if channel.recv_ready():
                chunk = channel.recv(4096).decode(errors="replace")
                for line in chunk.splitlines(keepends=True):
                    stamped = f"{ts()} | {line}"
                    print(f"[CAN ] {stamped}", end="", flush=True)
                    # Write to capture file only if line matches a filter type
                    if any(f in line for f in CAN_FILTER_TYPES):
                        cap_fh.write(stamped)
            elif channel.exit_status_ready():
                # Drain any final output
                while channel.recv_ready():
                    chunk = channel.recv(4096).decode(errors="replace")
                    for line in chunk.splitlines(keepends=True):
                        stamped = f"{ts()} | {line}"
                        print(f"[CAN ] {stamped}", end="", flush=True)
                        if any(f in line for f in CAN_FILTER_TYPES):
                            cap_fh.write(stamped)
                print(f"\n[CAN ] Remote spanbus process exited.")
                break
            else:
                time.sleep(0.05)

        # ── Footer ────────────────────────────────────────────────────────
        cap_fh.write("\n" + "=" * 60 + "\n")
        cap_fh.write(f"Stopped : {datetime.datetime.now().isoformat()}\n")

    channel.close()
    print(f"\n[CAN ] Filtered capture ended -> {capture_path}")


# =========================================================================
# MODBUS DEVICE DETECTION
# =========================================================================

def get_usb_device(client: paramiko.SSHClient) -> Optional[tuple]:
    """
    Find a Modbus-capable USB serial device on the remote host.

    Strategy:
      1. Try  pio device list  (PlatformIO, if installed)
         - CAN BUS  logs -> device whose description contains "Pico" (Raspberry Pi Pico serial)
         - MODBUS   logs -> device whose description contains "USB Single Serial"
      2. Fall back to listing /dev/ttyUSB* and /dev/ttyACM* directly

    Returns (identifier, device_path, description) or None.
    """
    print("\n[*] Probing USB serial devices ...")

    # -- Strategy 1: pio device list --------------------------------------
    pio_found = resolve_tool(client, "pio")
    if pio_found:
        output   = run_remote(client, "pio device list")
        devices: Dict[str, Dict[str, str]] = {}
        cur_dev = cur_ser = cur_desc = None

        for line in output.splitlines():
            line = line.strip()
            if line.startswith("/dev/tty"):
                if cur_dev and (cur_ser or cur_desc):
                    devices[cur_dev] = {
                        "serial":      cur_ser or "",
                        "description": cur_desc or "",
                    }
                cur_dev, cur_ser, cur_desc = line, None, None
            elif "SER=" in line and cur_dev:
                m = re.search(r"SER=([A-Za-z0-9]+)", line)
                if m:
                    cur_ser = m.group(1)
            elif line.startswith("Description:") and cur_dev:
                cur_desc = line.replace("Description:", "").strip()

        if cur_dev and (cur_ser or cur_desc):
            devices[cur_dev] = {
                "serial":      cur_ser or "",
                "description": cur_desc or "",
            }

        if devices:
            print("\n[*] pio device list detected:")
            for path, info in devices.items():
                print(f"    {path} -> '{info['description']}'")

            result = _pick_modbus_device(devices)
            if result:
                return result

    # -- Strategy 2: direct /dev listing ----------------------------------
    print("    [*] Falling back to /dev listing ...")
    raw = run_remote(client, "ls /dev/ttyUSB* /dev/ttyACM* 2>/dev/null").strip()
    if not raw:
        print("    No /dev/ttyUSB* or /dev/ttyACM* devices found on remote host.")
        return None

    fallback: Dict[str, Dict[str, str]] = {}
    for dev in raw.splitlines():
        dev = dev.strip()
        if dev:
            fallback[dev] = {"serial": "", "description": ""}
            print(f"    {dev}")

    return _pick_modbus_device(fallback)


def get_pico_can_device(client: paramiko.SSHClient) -> Optional[tuple]:
    """
    Scan  pio device list  for the Raspberry Pi Pico serial port used for
    CAN bus communication.

    Matching rule (case-insensitive):
      Description contains "pico"

    Returns (identifier, device_path, description) or None.
    Falls back to None if pio is not available or no Pico device is found.
    """
    print("\n[*] Probing for Pico serial (CAN BUS) via pio device list ...")

    pio_found = resolve_tool(client, "pio")
    if not pio_found:
        print("    [!] pio not found -- cannot auto-detect Pico serial for CAN.")
        return None

    output  = run_remote(client, "pio device list")
    devices: Dict[str, Dict[str, str]] = {}
    cur_dev = cur_ser = cur_desc = None

    for line in output.splitlines():
        line = line.strip()
        if line.startswith("/dev/tty"):
            if cur_dev and (cur_ser or cur_desc):
                devices[cur_dev] = {
                    "serial":      cur_ser or "",
                    "description": cur_desc or "",
                }
            cur_dev, cur_ser, cur_desc = line, None, None
        elif "SER=" in line and cur_dev:
            m = re.search(r"SER=([A-Za-z0-9]+)", line)
            if m:
                cur_ser = m.group(1)
        elif line.startswith("Description:") and cur_dev:
            cur_desc = line.replace("Description:", "").strip()

    if cur_dev and (cur_ser or cur_desc):
        devices[cur_dev] = {
            "serial":      cur_ser or "",
            "description": cur_desc or "",
        }

    for path, info in devices.items():
        desc = info.get("description", "")
        if "pico" in desc.lower():
            ident = desc.replace(" ", "_") if desc else path.split("/")[-1]
            print(f"[OK] Pico serial (CAN BUS): {path}  ('{desc}')")
            return ident, path, desc

    print("    [!] No Pico serial device found in pio device list.")
    return None


def _pick_modbus_device(devices: Dict[str, Dict[str, str]]) -> Optional[tuple]:
    """
    From a {path: {serial, description}} dict, return the best Modbus
    candidate as (identifier, path, description).

    Preference order (pio device list descriptions checked first):
      1. Description contains "USB Single Serial"  (explicit Modbus adapter)
      2. /dev/ttyUSB*  path with no Pico description
      3. /dev/ttyACM*  that is not STLINK and not a Pico

    Devices whose description contains "pico" are always skipped here
    (those belong to CAN bus capture).
    """
    # -- Pass 1: prefer explicit "USB Single Serial" description -----------
    for path, info in devices.items():
        desc = info.get("description", "")
        if "usb single serial" in desc.lower():
            ident = desc.replace(" ", "_") if desc else path.split("/")[-1]
            print(f"[OK] Modbus device (USB Single Serial): {path}  ('{desc}')")
            return ident, path, desc

    # -- Pass 2: any ttyUSB* that is not a Pico ---------------------------
    for path, info in devices.items():
        if "/dev/ttyUSB" in path:
            desc = info.get("description", "")
            if "pico" in desc.lower():
                continue   # belongs to CAN bus
            ident = desc.replace(" ", "_") if desc else path.split("/")[-1]
            print(f"[OK] Modbus device (ttyUSB fallback): {path}  ('{desc}')")
            return ident, path, desc

    # -- Pass 3: ttyACM* excluding STLINK and Pico ------------------------
    for path, info in devices.items():
        if "/dev/ttyACM" in path:
            desc = info.get("description", "")
            if "STLINK" in desc or "ST-Link" in desc or "pico" in desc.lower():
                continue
            ident = desc.replace(" ", "_") if desc else path.split("/")[-1]
            print(f"[OK] Modbus device (ttyACM fallback): {path}  ('{desc}')")
            return ident, path, desc

    print("    [!] No suitable Modbus USB serial device found in device list.")
    return None


# =========================================================================
# MODBUS REGISTER MAP  — Full DER / MID specification
# =========================================================================
# Key  : register address (int)
# Value: (dataset_name, register_name, decoder_fn)
#   decoder_fn(value: int) -> human-readable string
#   None  -> value logged as raw hex / decimal
# =========================================================================

# ── Shared decoder helpers ────────────────────────────────────────────────

def _led_mode(v: int) -> str:
    """0x008C  MID-Led-Mode-Request: requested operating mode sent to DER."""
    return {0: "Idle (No power output)", 1: "Grid Following", 2: "Grid Forming"}.get(
        v, f"Reserved (value={v})"
    )

def _der_op_mode(v: int) -> str:
    return {
        0: "Idle (No power output)", 1: "Grid-Following", 2: "Grid-Forming",
        3: "Reserved", 4: "Fault", 5: "Generator-On",
    }.get(v, f"Unknown (value={v})")

def _microgrid_req(v: int) -> str:
    return {0: "No state request", 1: "Do not form microgrid", 2: "Form microgrid"}.get(
        v, f"Unknown (value={v})"
    )

def _der_type(v: int) -> str:
    """0x1004  DER-Config: DER equipment type (bits 0-4)."""
    return {
        0: "Reserved", 1: "Reserved", 2: "Reserved", 3: "Reserved",
        4: "Battery", 5: "Bidirectional EVSE", 6: "Combiner",
    }.get(v & 0x1F, f"Unknown (value={v & 0x1F})")

def _sub_meter_cfg(v: int) -> str:
    """0x1009  DER sub-meter config (bit 0 = sub-metered, bits 1-3 = sub-meter number)."""
    sub_metered  = bool(v & 0x1)
    sub_meter_no = (v >> 1) & 0x7
    if sub_metered:
        return f"Sub-metered by MID (sub-meter number {sub_meter_no})"
    return "Not sub-metered by MID"

def _pwr_ctrl_mode(v: int) -> str:
    """0x00B4  MID-Led-Power-Control: power control operating mode."""
    return f"Power control mode = {v} (see DER spec for bit-field definition)"

def _signed16(v: int) -> int:
    """Convert unsigned 16-bit to signed."""
    return v if v < 0x8000 else v - 0x10000

def _watts(v: int) -> str:
    return f"{_signed16(v)} W"

def _ride_cat(v: int) -> str:
    return f"IEEE 1547 Category {v}" if 1 <= v <= 3 else f"Unknown category ({v})"

def _per_unit_v(v: int) -> str:
    return f"{v} (per unit voltage)"

def _ms(v: int) -> str:
    return f"{v} ms"

def _soe_pct(v: int) -> str:
    hi = (v >> 8) & 0xFF   # bits 8-15 = SOE percent
    lo = v & 0xFF           # bits 0-7  = SOE percent (low byte per spec)
    # Spec: 0-7 = SOE percent, 8-15 = SOE in 100Wh precision
    return f"SOE {v & 0xFF}%  |  SOE capacity {(v >> 8) * 100} Wh"


# ── Full register table ───────────────────────────────────────────────────

REGISTER_MAP: Dict[int, tuple] = {

    # ════════════════════════════════════════════════════════════════════
    # MID-Led-Mode-Request  (Image 3)
    # ════════════════════════════════════════════════════════════════════

    # 0x008C (140) — MID led mode request
    # Bits 0-3: requested DER operating mode
    # Bits 4-15: Reserved
    0x008C: (
        "MID-Led-Mode-Request",
        "MID LED Mode Request",
        lambda v: (
            f"Request to DER: {_led_mode(v & 0xF)}"
        ),
    ),

    # ════════════════════════════════════════════════════════════════════
    # DER-Status-Control  (Images 3)
    # ════════════════════════════════════════════════════════════════════

    # 0x0096 (150) — DER Status  [Input register]
    # Bit 0   : Island Detection  (0=No island, 1=Island detected)
    # Bits 1-5: Operating Mode
    # Bits 6-7: Microgrid-Requested-State
    # Bits 8-15: Reserved
    0x0096: (
        "DER-Status-Control",
        "DER Status",
        lambda v: (
            f"Island={'Detected' if (v >> 0) & 0x1 else 'None'} | "
            f"OpMode={_der_op_mode((v >> 1) & 0x1F)} | "
            f"Microgrid={_microgrid_req((v >> 6) & 0x3)}"
        ),
    ),

    # ════════════════════════════════════════════════════════════════════
    # MID-Status  (Images 2 & 3)
    # ════════════════════════════════════════════════════════════════════

    # 0x00A5 (165) — MID Status  [Holding register]
    # Bit 0: Config/SW update needed
    # Bit 1: Metering Fault
    # Bit 2: MID Relay Fault
    # Bit 3: Voltage Sensing / General Fault
    # Bit 4: Autotransformer Fault
    # Bit 5: Uncategorized Fault
    # Bits 6-15: Reserved
    0x00A5: (
        "MID-Status",
        "MID Status",
        lambda v: " | ".join(filter(None, [
            "Update needed"          if (v >> 0) & 1 else "No update needed",
            "Metering Fault"         if (v >> 1) & 1 else None,
            "MID Relay Fault"        if (v >> 2) & 1 else None,
            "Voltage/General Fault"  if (v >> 3) & 1 else None,
            "Autotransformer Fault"  if (v >> 4) & 1 else None,
            "Uncategorized Fault"    if (v >> 5) & 1 else None,
        ])) or "Status OK",
    ),

    # ════════════════════════════════════════════════════════════════════
    # MID-Led-Power-Control  (Image 2)
    # ════════════════════════════════════════════════════════════════════

    # 0x00B4 (180) — Power control operating mode  [Holding]
    0x00B4: (
        "MID-Led-Power-Control",
        "Power Control Operating Mode",
        _pwr_ctrl_mode,
    ),

    # 0x00B5 (181) — DER max import  [Holding, INT16, watts]
    0x00B5: (
        "MID-Led-Power-Control",
        "DER Max Import",
        lambda v: f"Max import = {_signed16(v)} W",
    ),

    # 0x00B6 (182) — DER max export  [Holding, INT16, watts]
    0x00B6: (
        "MID-Led-Power-Control",
        "DER Max Export",
        lambda v: f"Max export = {_signed16(v)} W",
    ),

    # 0x00B7 (183) — DER export power  [Holding, INT16, watts]
    0x00B7: (
        "MID-Led-Power-Control",
        "DER Export Power",
        lambda v: f"Export power request = {_signed16(v)} W",
    ),

    # 0x00B8 (184) — DER import power  [Holding, INT16, watts]
    0x00B8: (
        "MID-Led-Power-Control",
        "DER Import Power",
        lambda v: f"Import power request = {_signed16(v)} W",
    ),

    # ════════════════════════════════════════════════════════════════════
    # DER-Config  (Image 1)
    # ════════════════════════════════════════════════════════════════════

    # 0x1000 (4096) — Modbus version  [Holding, UINT16]
    0x1000: (
        "Modbus version",
        "Modbus Version",
        lambda v: f"Register map version {v}",
    ),

    # 0x1004 (4100) — DER type  [Input]
    # Bits 0-4 : Equipment type
    # Bits 5-15: DER manufacturer ID
    0x1004: (
        "DER-Config",
        "DER Type",
        lambda v: (
            f"Type={_der_type(v)} | "
            f"Manufacturer ID={(v >> 5) & 0x7FF}"
        ),
    ),

    # 0x1005 (4101) — DER size  [Input]
    # Bits 0-7 : DER max energy capacity (100 Wh precision)
    # Bits 8-15: DER max power
    0x1005: (
        "DER-Config",
        "DER Size",
        lambda v: (
            f"Max energy capacity={(v & 0xFF) * 100} Wh | "
            f"Max power={(v >> 8) & 0xFF} (per spec units)"
        ),
    ),

    # 0x1006 (4102) — DER Manufacturer HW Version  [Input, UINT16]
    0x1006: (
        "DER-Config",
        "DER Manufacturer HW Version",
        lambda v: f"HW version = {v}",
    ),

    # 0x1007 (4103) — DER Manufacturer SW Version  [Input, UINT32 low word]
    0x1007: (
        "DER-Config",
        "DER Manufacturer SW Version",
        lambda v: f"SW version = {v} (low 16-bit word)",
    ),

    # 0x1009 (4105) — DER sub-meter config  [Input]
    # Bit 0   : Sub-metered by MID (0=No, 1=Yes)
    # Bits 1-3: Sub-meter number
    # Bits 4-15: Reserved
    0x1009: (
        "DER-Config",
        "DER Sub-Meter Config",
        _sub_meter_cfg,
    ),

    # 0x100A (4106) — DER minimum microgrid SOE  [Input]
    # Bits 0-7 : SOE percent
    # Bits 8-15: SOE in 100 Wh precision
    0x100A: (
        "DER-Config",
        "DER Minimum Microgrid SOE",
        _soe_pct,
    ),

    # ════════════════════════════════════════════════════════════════════
    # MID ID  (Image 1)
    # ════════════════════════════════════════════════════════════════════

    # 0x1068 (4200) — MID Manufacturer ID  [Holding, UINT16]
    0x1068: (
        "MID ID",
        "MID Manufacturer ID",
        lambda v: f"MID manufacturer ID = {v}  (used by DER to identify this MID)",
    ),

    # 0x1069 (4201) — MID Type  [Holding, UINT16]
    0x1069: (
        "MID ID",
        "MID Type",
        lambda v: f"MID type = {v}",
    ),

    # 0x106A (4203) — MID Manufacturer HW Version  [Holding, UINT16]
    0x106A: (
        "MID ID",
        "MID Manufacturer HW Version",
        lambda v: f"MID HW version = {v}",
    ),

    # 0x106B (4204) — MID Manufacturer SW Version  [Holding, UINT16]
    0x106B: (
        "MID ID",
        "MID Manufacturer SW Version",
        lambda v: f"MID SW version = {v}",
    ),

    # ════════════════════════════════════════════════════════════════════
    # IEEE1547 Ride Through Category  (Image 1)
    # ════════════════════════════════════════════════════════════════════

    # 0x10CC (4300) — Ride Through Category  [Input, UINT16]
    0x10CC: (
        "IEEE1547 Ride Through Category",
        "Ride Through Category",
        _ride_cat,
    ),

    # ════════════════════════════════════════════════════════════════════
    # Voltage Ride Through Table  (Image 1)
    # ════════════════════════════════════════════════════════════════════

    # 0x10D6 (4310) — OV1-Voltage  [Input, UINT16, per unit]
    0x10D6: (
        "Voltage Ride Through Table",
        "OV1-Voltage",
        lambda v: f"OV1 shall-trip = {v} per unit  (Over-voltage threshold 1)",
    ),

    # 0x10D7 (4311) — OV1-Time  [Input, UINT16, ms]
    0x10D7: (
        "Voltage Ride Through Table",
        "OV1-Time",
        lambda v: f"OV1 clearing time = {v} ms",
    ),

    # 0x10D8 (4312) — OV2-Voltage  [Input, UINT16, per unit]
    0x10D8: (
        "Voltage Ride Through Table",
        "OV2-Voltage",
        lambda v: f"OV2 shall-trip = {v} per unit  (must be < OV1-Voltage)",
    ),

    # 0x10D9 (4313) — OV2-Time  [Input, UINT16, ms]
    0x10D9: (
        "Voltage Ride Through Table",
        "OV2-Time",
        lambda v: f"OV2 clearing time = {v} ms",
    ),

    # 0x10DA (4314) — UV1-Voltage  [Input, UINT16, per unit]
    0x10DA: (
        "Voltage Ride Through Table",
        "UV1-Voltage",
        lambda v: f"UV1 shall-trip = {v} per unit  (must be < OV2-Voltage)",
    ),

    # 0x10DB (4315) — UV1-Time  [Input, UINT16, ms]
    0x10DB: (
        "Voltage Ride Through Table",
        "UV1-Time",
        lambda v: f"UV1 clearing time = {v} ms",
    ),

    # 0x10DC (4316) — UV2-Voltage  [Input, UINT16, per unit]
    0x10DC: (
        "Voltage Ride Through Table",
        "UV2-Voltage",
        lambda v: f"UV2 shall-trip = {v} per unit  (must be < UV1-Voltage)",
    ),

    # 0x10DD (4317) — UV2-Time  [Input, UINT16, ms]
    0x10DD: (
        "Voltage Ride Through Table",
        "UV2-Time",
        lambda v: f"UV2 clearing time = {v} ms",
    ),

    # ════════════════════════════════════════════════════════════════════
    # Voltage Ride Through Hysteresis  (Image 1)
    # ════════════════════════════════════════════════════════════════════

    # 0x10DE (4318) — OV1-Voltage Hysteresis  [Input, UINT16, per unit]
    0x10DE: (
        "Voltage Ride Through Hysteresis",
        "OV1-Voltage Hysteresis",
        lambda v: f"OV1 hysteresis threshold = {v} per unit  (clear condition)",
    ),

    # 0x10DF (4319) — OV2-Voltage Hysteresis  [Input, UINT16, per unit]
    0x10DF: (
        "Voltage Ride Through Hysteresis",
        "OV2-Voltage Hysteresis",
        lambda v: f"OV2 hysteresis threshold = {v} per unit  (clear condition)",
    ),

    # 0x10E0 (4320) — UV1-Voltage Hysteresis  [Input, UINT16, per unit]
    0x10E0: (
        "Voltage Ride Through Hysteresis",
        "UV1-Voltage Hysteresis",
        lambda v: f"UV1 hysteresis threshold = {v} per unit  (clear condition)",
    ),
}

# Function-code descriptions used in the human-readable prefix
FC_DESCRIPTIONS: Dict[int, str] = {
    0x01: "Read Coils",
    0x02: "Read Discrete Inputs",
    0x03: "Read Holding Registers",
    0x04: "Read Input Registers",
    0x05: "Write Single Coil",
    0x06: "Write Single Register",
    0x0F: "Write Multiple Coils",
    0x10: "Write Multiple Registers",
}


def decode_frame(hex_frame: str) -> str:
    """
    Decode an 8-byte Modbus RTU frame (space-separated hex string) into a
    human-readable description.

    Frame layout (RTU, 8 bytes):
      [0]     Device address      (always 0x01 in this setup)
      [1]     Function code
      [2][3]  Register address    (big-endian)
      [4][5]  Value / quantity    (big-endian)
      [6][7]  CRC                 (little-endian, not decoded)

    Returns a string like:
      "Write Single Register | LED Mode (0x008C) -> Grid Following [DER-Status-Control]"
    """
    try:
        raw = bytes(int(b, 16) for b in hex_frame.strip().split())
    except ValueError:
        return "Malformed frame"

    if len(raw) < 6:
        return "Frame too short to decode"

    device_addr = raw[0]
    fc          = raw[1]
    reg_addr    = (raw[2] << 8) | raw[3]
    value       = (raw[4] << 8) | raw[5]

    fc_name = FC_DESCRIPTIONS.get(fc, f"FC=0x{fc:02X}")

    # Look up register in the map
    if reg_addr in REGISTER_MAP:
        dataset, reg_name, decoder = REGISTER_MAP[reg_addr]
        val_str = decoder(value) if decoder else f"0x{value:04X} ({value})"
        return (
            f"{fc_name} | {reg_name} (0x{reg_addr:04X}) -> {val_str} "
            f"[{dataset}]"
        )

    # Unknown register — still emit something useful
    return (
        f"{fc_name} | Register 0x{reg_addr:04X} = 0x{value:04X} ({value}) "
        f"[Device 0x{device_addr:02X}]"
    )


# =========================================================================
# MODBUS FRAME PARSING
# =========================================================================

def parse_modbus_frames(chunk: bytes, buf: bytearray) -> List[str]:
    """
    Accumulate raw bytes into buf, then extract 8-byte Modbus RTU frames
    that start with 0x01.  Returns a list of space-separated hex strings.
    """
    frames: List[str] = []
    buf.extend(chunk)

    while True:
        if not buf:
            break
        if buf[0] != 0x01:
            try:
                idx = buf.index(0x01)
                del buf[:idx]
            except ValueError:
                buf.clear()
                break
        if len(buf) < 8:
            break
        frame = buf[:8]
        del buf[:8]
        frames.append(" ".join(f"{b:02x}" for b in frame))

    return frames


# =========================================================================
# MODBUS CAPTURE
# =========================================================================

def capture_modbus(
    ssh_client:  paramiko.SSHClient,
    device_path: str,
    identifier:  str,
    log_path:    str,
    stop_event:  threading.Event,
) -> None:
    """
    Open a dedicated paramiko channel, run picocom on the remote serial
    device (inside a login shell so PATH is correct), parse 8-byte Modbus
    RTU frames, and write ALL timestamped lines to log_path.

    No sshpass / subprocess needed -- pure paramiko throughout.
    """
    print(f"[MOD ] Starting capture -> {log_path}")

    # Verify picocom is installed before trying to stream
    if not resolve_tool(ssh_client, "picocom"):
        print(f"[MOD ] picocom not found on remote host -- Modbus capture aborted.")
        print(f"[MOD ] Install it with:  sudo apt-get install picocom")
        with open(log_path, "w") as fh:
            fh.write("=== ERROR: picocom not installed on remote host ===\n")
            fh.write("=== Install: sudo apt-get install picocom         ===\n")
        return

    picocom_cmd    = f"picocom -b {MODBUS_BAUD} {device_path} --imap lfcrlf"
    channel        = open_channel(ssh_client, picocom_cmd)
    buf            = bytearray()
    transport_info = ssh_client.get_transport().getpeername()
    remote_host    = transport_info[0] if transport_info else "unknown"

    with open(log_path, "w", buffering=1, encoding="utf-8") as log_fh:
        # File header
        log_fh.write(f"=== Modbus Log Started : {datetime.datetime.now().isoformat()} ===\n")
        log_fh.write(f"=== Device             : {identifier} ({device_path}) ===\n")
        log_fh.write(f"=== Host               : {remote_host} ===\n")
        log_fh.write(f"=== Format             : TIMESTAMP | RAW FRAME (hex) | DESCRIPTION ===\n\n")

        # Streaming read loop
        while not stop_event.is_set():
            if channel.recv_ready():
                raw = channel.recv(256)
                for hex_frame in parse_modbus_frames(raw, buf):
                    now         = ts()
                    description = decode_frame(hex_frame)
                    stamped     = f"{now} | {hex_frame}  | {description}\n"

                    log_fh.write(stamped)
                    log_fh.flush()
                    print(f"[MOD ] {stamped}", end="", flush=True)
            elif channel.exit_status_ready():
                print(f"\n[MOD ] Remote picocom process exited.")
                break
            else:
                time.sleep(0.01)

        # File footer
        log_fh.write(f"\n=== Modbus Log Ended : {datetime.datetime.now().isoformat()} ===\n")

    channel.close()
    print(f"[MOD ] Capture ended -> {log_path}")


# =========================================================================
# MAIN ORCHESTRATOR
# =========================================================================

def run_capture(
    username: str,
    host: str,
    password: str,
    output_dir: str,
    duration: Optional[int],
    stop_requested: Optional[threading.Event] = None,
) -> None:
    """
    Stream CAN (filtered) + Modbus RTU to two files under ``output_dir/<user>@<host>/``.
    Pass ``stop_requested`` so ``.set()`` stops capture from an HTTP API.
    """

    # 1. SSH connect FIRST -- no folders created until auth succeeds
    client = connect_ssh(username, host, password)

    # 2. Create output folder only after successful login
    host_label = f"{username}@{host}"
    folder     = Path(output_dir) / host_label
    folder.mkdir(parents=True, exist_ok=True)
    stamp      = ts_file()

    can_capture_path = str(folder / f"canbus_capture_{stamp}.txt")
    modbus_log_path = str(folder / f"modbus_logs_{stamp}.txt")

    print(f"\n[*] Output folder : {folder.resolve()}")
    print(f"    CAN capture   : canbus_capture_{stamp}.txt")
    print(f"    Modbus log    : modbus_logs_{stamp}.txt")

    # 3. Detect PCAN (lsusb) AND Pico serial (pio device list) for CAN bus
    can_ready   = detect_pcan(client)
    pico_device = get_pico_can_device(client)
    if not can_ready:
        print(f"\n[!] '{PCAN_DEVICE_STRING}' not detected via lsusb.\n")
    if pico_device:
        pico_ident, pico_path, pico_desc = pico_device
        print(f"[OK] Pico serial confirmed for CAN BUS: {pico_path}  ('{pico_desc}')")
    else:
        print("[!] No Pico serial (CAN BUS) found in pio device list.")

    if not can_ready and not pico_device:
        print("[!] Neither PCAN adapter nor Pico serial detected -- CAN capture skipped.\n")
        can_ready = False

    # 4. Detect Modbus USB serial device (USB Single Serial via pio device list)
    modbus_device = get_usb_device(client)
    if not modbus_device:
        print("\n[!] No Modbus USB serial device found -- Modbus capture skipped.\n")

    if not can_ready and not modbus_device:
        client.close()
        raise RuntimeError(
            "No capture targets available (no CAN interface and no Modbus serial)."
        )

    # 5. Shared stop event (optional external caller for dashboard API)
    stop = stop_requested if stop_requested is not None else threading.Event()
    threads: List[threading.Thread] = []

    # 6. CAN capture thread
    if can_ready:
        threads.append(threading.Thread(
            target=capture_can,
            args=(client, can_capture_path, username, host, stop),
            daemon=True, name="CAN-capture",
        ))

    # 7. Modbus capture thread
    if modbus_device:
        ident, dev_path, _ = modbus_device
        threads.append(threading.Thread(
            target=capture_modbus,
            args=(client, dev_path, ident, modbus_log_path, stop),
            daemon=True, name="MOD-capture",
        ))

    for t in threads:
        t.start()

    active = ("CAN + Modbus" if (can_ready and modbus_device)
              else "CAN only" if can_ready else "Modbus only")
    print("\n" + "=" * 55)
    print(f"  Capturing: {active}.  Press Ctrl+C to stop.")
    if duration:
        print(f"  Auto-stopping in {duration} seconds.")
    print("=" * 55 + "\n")

    # 8. Wait for Ctrl+C, duration, or external stop
    try:
        if duration:
            deadline = time.time() + float(duration)
            while time.time() < deadline:
                if stop.is_set():
                    break
                time.sleep(min(0.5, deadline - time.time()))
        else:
            while any(t.is_alive() for t in threads):
                if stop.is_set():
                    break
                time.sleep(0.5)
    except KeyboardInterrupt:
        print("\n\n[*] Ctrl+C -- stopping all capture streams ...")
    finally:
        stop.set()
        for t in threads:
            t.join(timeout=8)
        client.close()

    # 9. Summary
    print("\n" + "=" * 55)
    print("  CAPTURE COMPLETE")
    print("=" * 55)
    print(f"\n  Folder : {folder.resolve()}")
    if can_ready:
        print(f"  CAN cap: canbus_capture_{stamp}.txt")
    if modbus_device:
        print(f"  Modbus : modbus_logs_{stamp}.txt")
    print()


# =========================================================================
# CLI
# =========================================================================

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Unified CAN + Modbus logger for SPAN lab cells",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples
--------
  python3 unified_logger.py capture -h 10.91.0.96 -u systems
  python3 unified_logger.py capture -h 10.91.0.96 -u systems -o ./lab_logs
  python3 unified_logger.py capture -h 10.91.0.96 -u systems --duration 120

Output
------
  systems@10.91.0.96/
    CAN bus capture_<timestamp>.txt  <- filtered: MID_RELAY / DER / NFT states
    Modbus logs_<timestamp>.txt      <- all 8-byte RTU frames
        """,
    )
    sub = parser.add_subparsers(dest="command")
    cap = sub.add_parser("capture", help="Start unified capture", add_help=False)
    cap.add_argument("-h", "--host",     required=True, dest="ssh_host", metavar="IP",
                     help="Lab cell IP address")
    cap.add_argument("-u", "--username", required=True, metavar="USER",
                     help="SSH username (e.g. systems)")
    cap.add_argument("-o", "--output",   default=".",   metavar="DIR",
                     help="Base output directory (default: current dir)")
    cap.add_argument("--duration",       type=int, default=None, metavar="SEC",
                     help="Auto-stop after N seconds (default: run until Ctrl+C)")
    cap.add_argument("--help", action="help", help="Show this help message")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    print("\n" + "=" * 55)
    print("  SSH AUTHENTICATION")
    print("=" * 55)
    password = getpass.getpass(f"\nPassword for {args.username}@{args.ssh_host}: ")

    signal.signal(signal.SIGINT, lambda *_: None)

    try:
        run_capture(
            username=args.username,
            host=args.ssh_host,
            password=password,
            output_dir=args.output,
            duration=args.duration,
            stop_requested=None,
        )
    except (ConnectionError, RuntimeError) as exc:
        print(f"\n[X] {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
