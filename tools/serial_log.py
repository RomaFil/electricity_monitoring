"""Read the board's USB log without touching GPIO0 / EN.

    python tools/serial_log.py COM16 300            # 300 s, prints to stdout
    python tools/serial_log.py COM16 300 reset      # restart the board first (EN pulse via RTS)

The NodeMCU auto-reset circuit pulls GPIO0 (the FLASH button) low when only DTR is asserted. Firmware
0.6.0-0.8.6 does a factory reset when FLASH is held 10 s, and on 25.10.2026 the board wiped its settings
after the port had been closed in such a state. So the port is opened with DTR and RTS released and kept
open; unplug the cable while this script runs, or let it finish and unplug right away.
"""
import sys
import time

import serial

port, secs = sys.argv[1], float(sys.argv[2])
s = serial.Serial()
s.port, s.baudrate, s.timeout = port, 115200, 0.2
s.dtr = False
s.rts = False
s.open()
if len(sys.argv) > 3 and sys.argv[3] == "reset":
    s.rts = True  # EN low
    time.sleep(0.1)
    s.rts = False
end = time.time() + secs
try:
    while time.time() < end:
        data = s.read(4096)
        if data:
            sys.stdout.buffer.write(data)
            sys.stdout.flush()
except serial.SerialException as exc:
    print(f"\n[port closed: {exc}]")
