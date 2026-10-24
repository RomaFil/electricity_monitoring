#!/usr/bin/env python3
"""Watch the village board for restarts, e.g. overnight after a firmware change.

    nohup python3 night-monitor.py 10 >> night-monitor.log 2>&1 &

Every minute reads the dashboard API and writes one line: the board's boot count, uptime, reason of
the last start, memory, signal, last ping. A restart (boot count up or uptime down) and a change of
firmware, golden copy, trial or status are marked at the start of the line. Stops after N hours.
For a detail of one line per minute set DIAG_BODY_EVERY_S=60 in .env (the poller then reads the
ESP diagnostics every minute instead of every 5).
"""
import json
import sys
import time
import urllib.request

URL = "http://10.0.0.1:3003/api/status"
EVERY_S = 60


def now():
    return time.strftime("%d.%m %H:%M:%S")


def main():
    hours = float(sys.argv[1]) if len(sys.argv) > 1 else 10
    end = time.time() + hours * 3600
    prev, restarts = None, 0
    print(f"{now()} start, {hours:g} h", flush=True)
    while time.time() < end:
        try:
            s = json.load(urllib.request.urlopen(URL, timeout=20))
            v = s["village"]
            d = (v.get("obs") or {}).get("diag") or {}
            cur = {k: d.get(k) for k in ("boot_count", "uptime_s", "rst", "fw", "gold", "trial", "heap", "blk", "rssi", "ota")}
            cur["status"] = v.get("status")
            cur["last_ping"] = time.strftime("%H:%M:%S", time.localtime(v["last_ping"])) if v.get("last_ping") else None
            notes = []
            if prev:
                if cur["boot_count"] is not None and prev["boot_count"] is not None and cur["boot_count"] > prev["boot_count"]:
                    restarts += cur["boot_count"] - prev["boot_count"]
                    notes.append(f"RESTART x{cur['boot_count'] - prev['boot_count']}")
                elif (cur["uptime_s"] or 0) < (prev["uptime_s"] or 0):
                    restarts += 1
                    notes.append("RESTART (uptime went down)")
                for k in ("fw", "gold", "trial", "ota", "status"):
                    if cur[k] != prev[k]:
                        notes.append(f"{k}: {prev[k]} -> {cur[k]}")
            mark = "; ".join(notes) if notes else "ok"
            print(f"{now()} {mark} | restarts {restarts} | boot {cur['boot_count']} up {cur['uptime_s']} "
                  f"rst={cur['rst']} heap {cur['heap']} blk {cur['blk']} rssi {cur['rssi']} fw {cur['fw']} "
                  f"gold {cur['gold']} trial {cur['trial']} {cur['status']} ping {cur['last_ping']}", flush=True)
            prev = cur
        except Exception as exc:  # the dashboard restarting, network: just note it
            print(f"{now()} error: {exc}", flush=True)
        time.sleep(EVERY_S)
    print(f"{now()} end, restarts {restarts}", flush=True)


if __name__ == "__main__":
    main()
