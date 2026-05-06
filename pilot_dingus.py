#!/usr/bin/env python3
"""
Pilot Dingus Lab Cell Controller
Device : Raspberry Pi Pico (Pilot Dingus by Garrett)
Port   : /dev/ttyACM15  Baud: 115200
Update : every 100ms when logging enabled (UPDATE_INTERVAL_MS=100)

Serial Commands (single byte):
  A → WATCH (HIZ)   ~12V
  B → CONNECT       ~9V
  C → CHARGE        ~6V
  D → VENT          ~3V
  L → Toggle logging on/off
  R → Request one JSON reading
  S → Query current status
  Z → Reset state change counter

Usage:
  Standalone CLI  : python3 pilot_dingus.py
  FastAPI backend : from pilot_dingus import PilotDingus
"""

import json
import os
import serial
import threading
import time
from datetime import datetime

# ── Config ─────────────────────────────────────────────────────────────────────
PORT     = "/dev/ttyACM15"
BAUD     = 115200
LOG_FILE = f"pilot_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"

# Pilot state letter → description (matches pilot_glyph_map in interface.cpp)
STATE_DESC = {
    "U":  "Unknown",
    "A":  "Standby - No EV",
    "B1": "EV Connected (no PWM)",
    "B2": "EV Connected (PWM active)",
    "C":  "Charging",
    "D":  "Charging + Ventilation",
    "E":  "No Power",
    "F":  "EVSE Error",
}

# ── Display helpers ─────────────────────────────────────────────────────────────
def format_data_packet(p: dict) -> str:
    mode       = p.get("mode",          "?")
    state      = p.get("state",         "U")
    state_desc = STATE_DESC.get(state,  "?")
    changes    = p.get("state_changes", 0)
    high_v     = p.get("high_v",        0.0)
    low_v      = p.get("low_v",         0.0)
    duty       = p.get("duty",          0.0)
    freq       = p.get("frequency",     0.0)
    adv_a      = p.get("adv_current",   0.0)
    ts         = p.get("timestamp",     "")

    return (
        f"\n  ┌──────────────────────────────────────────┐\n"
        f"  │  TIME     : {ts:<30}│\n"
        f"  │  MODE     : {mode:<30}│\n"
        f"  │  STATE    : {state} - {state_desc:<26}│\n"
        f"  │  CHANGES  : {str(changes):<30}│\n"
        f"  │  HIGH V   : {high_v:<6.2f} V                        │\n"
        f"  │  LOW V    : {low_v:<6.2f} V                        │\n"
        f"  │  DUTY     : {duty*100:<6.1f} %                        │\n"
        f"  │  FREQ     : {freq:<6.1f} Hz                       │\n"
        f"  │  ADV CUR  : {adv_a:<6.1f} A                        │\n"
        f"  └──────────────────────────────────────────┘"
    )

# ── Serial reader thread (CLI mode) ────────────────────────────────────────────
def reader_thread(ser: serial.Serial, stop_event: threading.Event):
    with open(LOG_FILE, "a") as f:
        while not stop_event.is_set():
            try:
                raw = ser.readline()
                if not raw:
                    continue
                line = raw.decode("utf-8", errors="replace").strip()
                if not line:
                    continue

                ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]

                try:
                    parsed = json.loads(line)
                    parsed["timestamp"] = ts
                    ack = parsed.get("ack")

                    if ack == "boot":
                        print(f"\n  ✔  DEVICE BOOTED | mode=WATCH | ver={parsed.get('ver')}")

                    elif ack == "mode_set":
                        print(f"\n  ✔  MODE CHANGED → {parsed['mode']}")

                    elif ack == "status":
                        state = parsed.get("pilot_state", "U")
                        print(
                            f"\n  ℹ  STATUS REPORT"
                            f"\n     Mode         : {parsed.get('mode')}"
                            f"\n     Pilot State  : {state} — {STATE_DESC.get(state, '?')}"
                            f"\n     State Changes: {parsed.get('state_changes')}"
                            f"\n     Logging      : {parsed.get('logging')}"
                            f"\n     Protocol Ver : {parsed.get('ver')}"
                        )

                    elif ack == "logging":
                        status = "ON ✔" if parsed.get("enabled") else "OFF ✗"
                        print(f"\n  ℹ  LOGGING is now {status}")

                    elif ack == "counter_reset":
                        print(f"\n  ℹ  State change counter reset to 0")

                    elif "state" in parsed:
                        # Regular 100ms data packet
                        print(format_data_packet(parsed))

                    else:
                        print(f"[{ts}] {line}")

                    # Save everything to log file
                    f.write(json.dumps(parsed) + "\n")
                    f.flush()

                except json.JSONDecodeError:
                    print(f"[{ts}] RAW: {line}")

            except serial.SerialException as e:
                print(f"\n[ERROR] Serial disconnected: {e}")
                stop_event.set()

# ── CLI Entry Point ─────────────────────────────────────────────────────────────
MENU = """
╔════════════════════════════════════════════╗
║      Pilot Dingus — Lab Cell Controller    ║
╠══════════╦═════════════════════════════════╣
║  Key     ║  Action                        ║
╠══════════╬═════════════════════════════════╣
║  1       ║  WATCH   / HIZ    (~12V)       ║
║  2       ║  CONNECT          (~9V)        ║
║  3       ║  CHARGE           (~6V)        ║
╠══════════╬═════════════════════════════════╣
║  s       ║  Query current status          ║
║  r       ║  Request single reading        ║
║  l       ║  Toggle logging (100ms)        ║
║  z       ║  Reset state change counter    ║
║  q       ║  Quit                          ║
╚══════════╩═════════════════════════════════╝
"""

def run_cli():
    print(f"[INFO] Connecting to {PORT} @ {BAUD} baud...")
    try:
        ser = serial.Serial(PORT, BAUD, timeout=1)
    except serial.SerialException as e:
        print(f"[ERROR] Cannot open {PORT}: {e}")
        return

    time.sleep(1)  # Wait for Pico to initialize
    stop_event = threading.Event()

    # Start background reader thread
    t = threading.Thread(target=reader_thread, args=(ser, stop_event), daemon=True)
    t.start()

    # Auto-enable logging on connect
    ser.write(b'L')
    time.sleep(0.2)

    print(f"[INFO] Log file: {LOG_FILE}")
    print(MENU)

    while True:
        try:
            cmd = input("Command > ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            print("\n[INFO] Interrupted.")
            break

        if   cmd == 'q': break
        elif cmd == '1':
            print("  → Sending: WATCH (HIZ)")
            ser.write(b'A')
        elif cmd == '2':
            print("  → Sending: CONNECT")
            ser.write(b'B')
        elif cmd == '3':
            print("  → Sending: CHARGE")
            ser.write(b'C')
        elif cmd == 's': ser.write(b'S')
        elif cmd == 'r': ser.write(b'R')
        elif cmd == 'l': ser.write(b'L')
        elif cmd == 'z': ser.write(b'Z')
        else:
            print("  Unknown command. Use 1/2/3/s/r/l/z/q")

    stop_event.set()
    ser.close()
    print(f"\n[INFO] Session ended. Log saved → {LOG_FILE}")


# ── PilotDingus Class (used by FastAPI backend) ─────────────────────────────────
class PilotDingus:
    """
    Class interface for controlling Pilot Dingus from FastAPI backend.

    Usage:
        from pilot_dingus import PilotDingus
        pilot = PilotDingus(port="/dev/ttyACM15")
        pilot.set_mode("CHARGE")
        state = pilot.read_state()
    """

    CMD_MAP = {
        "WATCH":   b'A',
        "HIZ":     b'A',
        "CONNECT": b'B',
        "CHARGE":  b'C',
        "VENT":    b'D',
    }

    def __init__(self, port=PORT, baud=BAUD):
        self.port = port
        self.baud = baud
        self._logs        = []          # Rolling buffer of last 1000 packets
        self._last_state  = {}          # Most recent data packet
        self._lock        = threading.Lock()
        self._log_file    = f"pilot_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jsonl"

        print(f"[PilotDingus] Connecting to {port} @ {baud}...")
        self.ser = serial.Serial(port, baud, timeout=2)
        time.sleep(1)  # Wait for Pico to initialize
        try:
            self.ser.reset_input_buffer()
        except Exception:
            pass

        # Start background reader
        t = threading.Thread(target=self._reader, daemon=True)
        t.start()

        # Enable logging automatically, then nudge a few frames (many builds only stream after R/L).
        self.enable_logging()
        time.sleep(0.2)
        self.ser.write(b"R")
        time.sleep(0.15)
        self.ser.write(b"R")
        print(f"[PilotDingus] Ready. Logging to {self._log_file}")

    def _reader(self):
        """Background thread: reads serial, updates state, writes log file."""
        with open(self._log_file, "a") as f:
            while True:
                try:
                    raw = self.ser.readline()
                    if not raw:
                        continue
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line:
                        continue

                    ts = datetime.now().isoformat()

                    try:
                        parsed = json.loads(line)
                        parsed["timestamp"] = ts

                        with self._lock:
                            # Any line that carries pilot measurements counts as telemetry,
                            # even if the firmware also includes an "ack" field on every frame.
                            tel_keys = (
                                "state",
                                "pilot_state",
                                "high_v",
                                "low_v",
                                "duty",
                                "frequency",
                                "adv_current",
                                "mode",
                            )
                            has_telemetry = any(k in parsed for k in tel_keys)
                            if has_telemetry:
                                row = dict(parsed)
                                if "state" not in row and "pilot_state" in row:
                                    row["state"] = row.get("pilot_state", "U")
                                row.pop("ack", None)
                                self._last_state = {**self._last_state, **row}
                            elif parsed.get("ack") == "mode_set" and "mode" in parsed:
                                self._last_state = {
                                    **self._last_state,
                                    "mode": parsed["mode"],
                                }
                            # Keep rolling log buffer
                            self._logs.append(parsed)
                            if len(self._logs) > 1000:
                                self._logs.pop(0)

                        # Write to log file
                        f.write(json.dumps(parsed) + "\n")
                        f.flush()

                    except json.JSONDecodeError:
                        if os.environ.get("PILOT_SERIAL_DEBUG", "").strip():
                            print(f"[PilotDingus] non-JSON line: {line[:240]!r}")

                except serial.SerialException as e:
                    print(f"[PilotDingus] Serial error: {e}")
                    time.sleep(1)
                except Exception:
                    continue

    def enable_logging(self):
        """Enable continuous 100ms JSON logging from device."""
        self.ser.write(b'L')

    def disable_logging(self):
        """Disable continuous logging from device."""
        self.ser.write(b'L')

    def set_mode(self, mode: str) -> dict:
        """
        Set pilot mode.
        mode: 'WATCH', 'CONNECT', 'CHARGE', or 'VENT'
        Returns: dict with mode confirmation
        """
        cmd = self.CMD_MAP.get(mode.upper())
        if not cmd:
            raise ValueError(f"Unknown mode '{mode}'. Use: WATCH, CONNECT, CHARGE, VENT")
        self.ser.write(cmd)
        try:
            self.ser.flush()
        except Exception:
            pass
        time.sleep(0.08)
        # Single-shot reading so HTTP clients see fresh values without waiting for 100 ms stream.
        self.ser.write(b"R")
        time.sleep(0.25)
        print(f"[PilotDingus] Mode set → {mode}")
        return {"ok": True, "mode": mode}

    def read_state(self) -> dict:
        """Return the most recently received data packet."""
        with self._lock:
            return self._last_state.copy()

    def request_reading(self) -> dict:
        """Request one immediate reading from device (sends 'R')."""
        self.ser.write(b'R')
        time.sleep(0.2)  # Wait for response
        return self.read_state()

    def get_status(self) -> None:
        """Request status report from device (sends 'S')."""
        self.ser.write(b'S')

    def reset_counter(self) -> None:
        """Reset the state change counter on device (sends 'Z')."""
        self.ser.write(b'Z')

    def get_recent_logs(self, n: int = 100) -> list:
        """Return last n log entries."""
        with self._lock:
            return self._logs[-n:].copy()

    def get_log_file(self) -> str:
        """Return path to current log file."""
        return self._log_file

    def close(self):
        """Close serial connection."""
        self.ser.close()
        print("[PilotDingus] Connection closed.")


# ── Run CLI if executed directly ────────────────────────────────────────────────
if __name__ == "__main__":
    run_cli()
