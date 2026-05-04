"""
Contactor Control - Waveshare 16-ch Modbus PoE Ethernet Relay
--------------------------------------------------------------
Initial State : C1=ON, C2=OFF, C3=ON
V2H Sequence  : C1 OFF → C3 OFF → C2 ON (with delay between each)

Usage:
    pip install pymodbus
    python contactor_control.py
"""

from pymodbus.client import ModbusTcpClient
import time

# ─── CONFIG ───────────────────────────────────────────────
RELAY_IP   = "10.91.0.201"   # Change to your Waveshare IP
RELAY_PORT = 502
STEP_DELAY = 2                 # Seconds between each step in V2H sequence

# Channel mapping (change if wired differently)
C1 = 1   # Grid Contactor
C2 = 2   # DER_V2H Contactor (V2H)
C3 = 3   # DER Contactor
# ──────────────────────────────────────────────────────────

client = ModbusTcpClient(RELAY_IP, port=RELAY_PORT)


def connect():
    if not client.connect():
        print(f"ERROR: Could not connect to relay at {RELAY_IP}:{RELAY_PORT}")
        print("Check IP address and network connection.")
        exit(1)
    print(f"Connected to Waveshare relay at {RELAY_IP}\n")


def set_relay(channel, state):
    """Set relay channel ON (True) or OFF (False). Channel is 1-indexed."""
    client.write_coil(channel - 1, state)
    time.sleep(0.2)  # small settle time


def read_relay(channel):
    """Read relay channel status. Returns True if ON."""
    result = client.read_coils(address=channel - 1, count=1)
    return result.bits[0]


def status_label(state):
    return "ON  [CLOSED]" if state else "OFF [OPEN] "


def print_status():
    c1 = read_relay(C1)
    c2 = read_relay(C2)
    c3 = read_relay(C3)
    print("┌─────────────────────────────────────┐")
    print("│         Contactor Status             │")
    print("├─────────────────────────────────────┤")
    print(f"│  C1 - Grid Contactor  : {status_label(c1)}   │")
    print(f"│  C2 - DER_V2H Contactor    : {status_label(c2)}   │")
    print(f"│  C3 - DER Contactor    : {status_label(c3)}   │")
    print("└─────────────────────────────────────┘")


def set_initial_state():
    """C1=ON, C2=OFF, C3=ON"""
    print("\n Setting initial state: C1=ON, C2=OFF, C3=ON ...")
    set_relay(C1, True)
    set_relay(C2, False)
    set_relay(C3, True)
    time.sleep(0.5)
    print(" Done.")
    print_status()


def v2h_sequence():
    """V2H automation: C1 OFF → C3 OFF → C2 ON"""
    print("\n Starting V2H sequence...")
    print(f" (Delay between steps: {STEP_DELAY}s)\n")

    print(f" Step 1/3 → Turning OFF C1 (Grid Contactor)...")
    set_relay(C1, False)
    time.sleep(STEP_DELAY)
    print(f"           C1 is now {status_label(read_relay(C1))}")

    print(f" Step 2/3 → Turning OFF C3 (Dr Contactor)...")
    set_relay(C3, False)
    time.sleep(STEP_DELAY)
    print(f"           C3 is now {status_label(read_relay(C3))}")

    print(f" Step 3/3 → Turning ON  C2 (DS Contactor)...")
    set_relay(C2, True)
    time.sleep(STEP_DELAY)
    print(f"           C2 is now {status_label(read_relay(C2))}")

    print("\n V2H sequence complete.")
    print_status()


def manual_control():
    """Manual toggle for individual contactors."""
    while True:
        print("\n─── Manual Control ────────────────────")
        print("  c1on / c1off   → Control C1")
        print("  c2on / c2off   → Control C2")
        print("  c3on / c3off   → Control C3")
        print("  status         → Read all statuses")
        print("  back           → Return to main menu")
        cmd = input("\nCommand: ").strip().lower()

        if cmd == "c1on":   set_relay(C1, True);  print("C1 ON")
        elif cmd == "c1off": set_relay(C1, False); print("C1 OFF")
        elif cmd == "c2on":  set_relay(C2, True);  print("C2 ON")
        elif cmd == "c2off": set_relay(C2, False); print("C2 OFF")
        elif cmd == "c3on":  set_relay(C3, True);  print("C3 ON")
        elif cmd == "c3off": set_relay(C3, False); print("C3 OFF")
        elif cmd == "status": print_status()
        elif cmd == "back": break
        else: print("Unknown command.")


def main():
    connect()

    while True:
        print("\n══════════════════════════════════════")
        print("      EV Contactor Control System     ")
        print("══════════════════════════════════════")
        print("  1. Set initial state (C1=ON C2=OFF C3=ON)")
        print("  2. Run V2H sequence (C1 OFF → C3 OFF → C2 ON)")
        print("  3. Manual contactor control")
        print("  4. Read current status")
        print("  q. Quit")
        print("──────────────────────────────────────")

        choice = input("Select: ").strip().lower()

        if choice == "1":
            set_initial_state()
        elif choice == "2":
            print_status()
            confirm = input("\n Run V2H sequence? (yes/no): ").strip().lower()
            if confirm == "yes":
                v2h_sequence()
            else:
                print("Cancelled.")
        elif choice == "3":
            manual_control()
        elif choice == "4":
            print_status()
        elif choice == "q":
            print("Closing connection.")
            client.close()
            break
        else:
            print("Invalid choice.")


if __name__ == "__main__":
    main()
