#!/usr/bin/env python3
"""Emulator for tests without hardware. Standard library only.

  # pretend to be power-guard (home laptop):
  python emulator.py power --url http://10.0.0.1:3003/api/power --key TOKEN --ac 0 --battery 80 --event ac_lost

  # pretend to be the ESP in direct mode, one ping a minute:
  python emulator.py heartbeat --url http://10.0.0.1:3003/api/heartbeat --key TOKEN --every 60

  # pretend to be the ESP pinging Healthchecks (relay mode):
  python emulator.py hc --uuid <UUID> --every 60
"""
import argparse
import json
import time
import urllib.request
import uuid


def post(url, payload, key=None):
    headers = {"Content-Type": "application/json"}
    if key:
        headers["X-Device-Key"] = key
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=10) as r:
        return r.status, r.read().decode()[:300]


def main():
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("power")
    p.add_argument("--url", required=True)
    p.add_argument("--key", required=True)
    p.add_argument("--ac", type=int, default=1)
    p.add_argument("--battery", type=float, default=90)
    p.add_argument("--event", help="ac_lost, ac_restored, sleep_start, sleep_end, forced_poweroff, unknown_gap")
    h = sub.add_parser("heartbeat")
    h.add_argument("--url", required=True)
    h.add_argument("--key", required=True)
    h.add_argument("--every", type=float, default=0, help="seconds between pings; 0 = send once")
    c = sub.add_parser("hc")
    c.add_argument("--uuid", required=True)
    c.add_argument("--every", type=float, default=0)
    a = ap.parse_args()

    if a.cmd == "power":
        now = time.time()
        sample = {"ts": now, "ac": bool(a.ac), "battery": a.battery,
                  "status": "Charging" if a.ac else "Discharging", "state": "NORMAL" if a.ac else "ON_BATTERY"}
        events = []
        if a.event:
            events.append({"id": uuid.uuid4().hex, "ts": now, "type": a.event, "ac": bool(a.ac),
                           "battery": a.battery, "detail": {}})
        print(post(a.url, {"sample": sample, "events": events}, a.key))
        return

    start, boots = time.time(), 1
    while True:
        body = {"uptime_s": round(time.time() - start), "boot_count": boots, "rssi": -60, "fw": "emulator"}
        url = a.url if a.cmd == "heartbeat" else f"https://hc-ping.com/{a.uuid}"
        try:
            print(time.strftime("%H:%M:%S"), post(url, body, getattr(a, "key", None)))
        except Exception as exc:
            print(time.strftime("%H:%M:%S"), "failed:", exc)
        if not a.every:
            break
        time.sleep(a.every)


if __name__ == "__main__":
    main()
