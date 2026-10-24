#!/usr/bin/env python3
"""Watch the village board for restarts, e.g. overnight after a firmware change.

    nohup python3 night-monitor.py 10 >> night-monitor.log 2>&1 &

Every 2 minutes reads the dashboard API (the ESP diagnostics the poller fetches every ~5 minutes)
and writes a line when the board restarted (boot count up or uptime down), when its firmware,
golden copy or trial state changed, and a summary line every 30 minutes. Stops after N hours.
"""
import json
import sys
import time
import urllib.request

URL = "http://10.0.0.1:3003/api/status"
EVERY_S = 120
SUMMARY_S = 1800


def now():
    return time.strftime("%d.%m %H:%M:%S")


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 10
    end = time.time() + hours * 3600
    prev, last_summary, restarts = None, 0.0, 0
    print(f"{now()} start, {hours:g} h", flush=True)
    while time.time() < end:
        try:
            s = json.load(urllib.request.urlopen(URL, timeout=20))
            v = s["village"]
            d = (v.get("obs") or {}).get("diag") or {}
            cur = {k: d.get(k) for k in ("boot_count", "uptime_s", "fw", "gold", "trial", "heap", "blk", "rssi", "ota")}
            cur["status"] = v.get("status")
            if prev:
                notes = []
                if cur["boot_count"] is not None and prev["boot_count"] is not None and cur["boot_count"] > prev["boot_count"]:
                    restarts += cur["boot_count"] - prev["boot_count"]
                    notes.append(f"RESTART x{cur['boot_count'] - prev['boot_count']}")
                elif (cur["uptime_s"] or 0) < (prev["uptime_s"] or 0):
                    restarts += 1
                    notes.append("RESTART (uptime went down)")
                for k in ("fw", "gold", "trial", "ota", "status"):
                    if cur[k] != prev[k]:
                        notes.append(f"{k}: {prev[k]} -> {cur[k]}")
                if notes:
                    print(f"{now()} {'; '.join(notes)} | {cur}", flush=True)
            if time.time() - last_summary >= SUMMARY_S:
                last_summary = time.time()
                print(f"{now()} ok, restarts so far {restarts} | {cur}", flush=True)
            prev = cur
        except Exception as exc:  # the dashboard restarting, network: just note it
            print(f"{now()} error: {exc}", flush=True)
        time.sleep(EVERY_S)
    print(f"{now()} end, restarts {restarts}", flush=True)


if __name__ == "__main__":
    main()
