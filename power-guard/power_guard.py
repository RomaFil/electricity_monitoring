#!/usr/bin/env python3
"""power-guard: watches the laptop charger on the Proxmox host and reports to Svitlo-Local.

Runs on the Proxmox HOST (not in a container): only the host sees /sys/class/power_supply.
Standard library only, so nothing has to be installed on the host.

States:
  NORMAL      charger connected. Sends a sample every REPORT_INTERVAL seconds.
  ON_BATTERY  charger gone. Logs "ac_lost" at once and works like a UPS.
              After SLEEP_AFTER_MIN minutes (or battery <= SLEEP_BELOW_PCT) it goes to
              a sleep loop: rtcwake -m mem -s WAKE_INTERVAL, wake, check, sleep again.
              Battery <= CRITICAL_PCT -> clean poweroff.
  OFF         written right before the clean poweroff, so the next start knows why.

Every event goes to an append-only journal (survives reboots) and to an outbox
that is resent until the server confirms it.
"""
import json
import logging
import os
import signal
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

log = logging.getLogger("power-guard")

NORMAL, ON_BATTERY, OFF = "NORMAL", "ON_BATTERY", "OFF"

DEFAULTS = {
    "SERVER_URL": "http://10.0.0.1:3003/api/power",
    "TOKEN": "",
    "REPORT_INTERVAL": "30",
    "SLEEP_AFTER_MIN": "10",
    "SLEEP_BELOW_PCT": "30",
    "WAKE_INTERVAL": "300",
    "CRITICAL_PCT": "7",
    "KEEP_AWAKE": "false",
    "NET_WAIT_S": "30",
    "AC_PATH": "/sys/class/power_supply/ADP1",
    "BAT_PATH": "/sys/class/power_supply/BAT1",
    "STATE_DIR": "/var/lib/power-guard",
    "POWEROFF_CMD": "systemctl poweroff",
}


def load_config(path="/etc/power-guard.conf") -> dict:
    """Simple KEY=VALUE file; lines starting with # are comments."""
    cfg = dict(DEFAULTS)
    p = Path(path)
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


class Sysfs:
    """Reads charger and battery. Any value may be None if the file is missing."""

    def __init__(self, ac_path, bat_path):
        self.ac, self.bat = Path(ac_path), Path(bat_path)

    @staticmethod
    def _read(p):
        try:
            return p.read_text().strip()
        except OSError:
            return None

    def read(self):
        online = self._read(self.ac / "online")
        cap = self._read(self.bat / "capacity")
        cur = self._read(self.bat / "current_now")
        return {
            "ac": None if online is None else online == "1",
            "battery": float(cap) if cap and cap.isdigit() else None,
            "status": self._read(self.bat / "status"),
            "current_now": float(cur) if cur and cur.lstrip("-").isdigit() else None,
        }


class Store:
    """state.json (atomic rewrite), journal.jsonl (append-only), outbox.json (unsent events)."""

    def __init__(self, state_dir):
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_f, self.journal_f, self.outbox_f = (self.dir / n for n in ("state.json", "journal.jsonl", "outbox.json"))

    def _write_atomic(self, path, data):
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)

    def _load(self, path, default):
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def load_state(self):
        return self._load(self.state_f, None)

    def save_state(self, st):
        self._write_atomic(self.state_f, st)

    def journal(self, event):
        with open(self.journal_f, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())

    def outbox(self):
        return self._load(self.outbox_f, [])

    def save_outbox(self, items):
        self._write_atomic(self.outbox_f, items)


class Sender:
    def __init__(self, url, token, timeout=5):
        self.url, self.token, self.timeout = url, token, timeout

    def send(self, payload) -> list:
        """POST to the server; returns ids of accepted events. Raises on any failure."""
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Device-Key": self.token})
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read()).get("acked", [])


def run_cmd(args) -> int:
    log.info("run: %s", " ".join(args))
    try:
        return subprocess.run(args, check=False).returncode
    except OSError as exc:
        log.error("cannot run %s: %s", args[0], exc)
        return 127


class Guard:
    def __init__(self, cfg, sysfs, store, sender, run=run_cmd, clock=time.time, sleep=time.sleep):
        self.cfg, self.sysfs, self.store, self.sender = cfg, sysfs, store, sender
        self.run, self.clock, self.sleep = run, clock, sleep
        self.report_interval = float(cfg["REPORT_INTERVAL"])
        self.sleep_after = float(cfg["SLEEP_AFTER_MIN"]) * 60
        self.sleep_below = float(cfg["SLEEP_BELOW_PCT"])
        self.wake_interval = int(float(cfg["WAKE_INTERVAL"]))
        self.critical = float(cfg["CRITICAL_PCT"])
        self.keep_awake = cfg["KEEP_AWAKE"].lower() in ("1", "true", "yes")
        self.net_wait = float(cfg["NET_WAIT_S"])
        self.state = NORMAL
        self.lost_at = None
        self.restore_precision = None  # seconds of uncertainty for the next ac_restored
        self.next_report = 0.0
        self.next_sleep_try = 0.0
        self.backoff = 0.0
        self.last = {}

    # ---------- persistence ----------

    def _save(self, **extra):
        st = {"state": self.state, "lost_at": self.lost_at, "last_alive": self.clock(), "clean_exit": False}
        st.update(extra)
        self.store.save_state(st)

    def emit(self, type_, **detail):
        r = self.last
        e = {"id": uuid.uuid4().hex, "ts": detail.pop("ts", self.clock()), "type": type_,
             "ac": r.get("ac"), "battery": r.get("battery"), "status": r.get("status"), "detail": detail}
        self.store.journal(e)
        self.store.save_outbox(self.store.outbox() + [e])
        log.info("event %s %s", type_, detail)
        return e

    def flush(self) -> bool:
        """Send the latest sample and all unsent events. Returns True on success."""
        out = self.store.outbox()
        r = self.last
        sample = {"ts": self.clock(), "state": self.state, **{k: r.get(k) for k in ("ac", "battery", "status", "current_now")}}
        try:
            acked = set(self.sender.send({"sample": sample, "events": out}))
        except Exception as exc:  # network down, server down, bad token...
            log.warning("send failed: %s", exc)
            return False
        if acked:
            self.store.save_outbox([e for e in self.store.outbox() if e["id"] not in acked])
        return True

    # ---------- lifecycle ----------

    def start(self):
        """Called once at service start: recover the previous state from disk."""
        now = self.clock()
        self.last = self.sysfs.read()
        prev = self.store.load_state()
        self.emit("guard_start")
        if prev:
            self.lost_at = prev.get("lost_at")
            gap = now - (prev.get("last_alive") or now)
            if prev.get("state") == OFF:
                self.emit("unknown_gap", start=prev.get("poweroff_at") or prev.get("last_alive"), end=now, reason="poweroff")
                self.restore_precision = gap
            elif gap > (self.wake_interval if prev.get("sleeping") else 0) + 3 * self.report_interval:
                reason = "shutdown" if prev.get("clean_exit") else "crash"
                self.emit("unknown_gap", start=prev.get("last_alive"), end=now, reason=reason)
                self.restore_precision = gap
            if prev.get("state") in (ON_BATTERY, OFF) and self.lost_at:
                self.state = ON_BATTERY  # step() will close the outage if the charger is back
        self._save()

    def stop(self):
        self._save(clean_exit=True)

    def step(self):
        """One loop iteration. Returns seconds to wait before the next one."""
        now = self.clock()
        r = self.sysfs.read()
        if r.get("ac") is None:
            log.warning("cannot read charger state, keeping %s", self.state)
            r["ac"] = self.state == NORMAL
        self.last = r

        if self.state == NORMAL and not r["ac"]:
            self.state, self.lost_at = ON_BATTERY, now
            self.emit("ac_lost")
            self.next_report = 0
        elif self.state == ON_BATTERY:
            if r["ac"]:
                self._restored(now)
            elif r["battery"] is not None and r["battery"] <= self.critical:
                self._poweroff()
                return self.report_interval
            elif not self.keep_awake and now >= self.next_sleep_try and (
                    now - (self.lost_at or now) >= self.sleep_after
                    or (r["battery"] is not None and r["battery"] <= self.sleep_below)):
                self._sleep_cycle()
                self._save()
                return 0  # check again right after waking

        self._save()
        if now >= self.next_report:
            self.flush()
            self.next_report = now + self.report_interval
        return self.report_interval

    def _restored(self, end, precision=None):
        detail = {"start": self.lost_at, "end": end}
        if self.lost_at:
            detail["duration"] = end - self.lost_at
        precision = precision if precision is not None else self.restore_precision
        if precision:
            detail["precision_s"] = precision
        self.emit("ac_restored", ts=end, **detail)
        self.state, self.lost_at, self.restore_precision = NORMAL, None, None
        self.next_report = 0

    def _sleep_cycle(self):
        before = self.last.get("battery")
        t0 = self.clock()
        self.emit("sleep_start", wake_in=self.wake_interval)
        self.flush()
        self.run(["sync"])
        self._save(sleeping=True)
        rc = self.run(["rtcwake", "-m", "mem", "-s", str(self.wake_interval)])
        t1 = self.clock()
        if rc != 0:
            self.backoff = min(max(60.0, self.backoff * 2), 900.0)
            self.next_sleep_try = t1 + self.backoff
            self.emit("rtcwake_error", rc=rc, retry_in=self.backoff)
            self.flush()
            return
        self.backoff = 0.0
        self.last = r = self.sysfs.read()
        slept = t1 - t0
        detail = {"slept_s": round(slept), "battery_before": before}
        if before is not None and r.get("battery") is not None and slept > 0:
            detail["drain_pct_per_h"] = round((before - r["battery"]) / slept * 3600, 2)
        self.emit("sleep_end", **detail)
        if r.get("ac"):
            # Charger came back sometime between t0 and t1; we only know it at wake-up.
            self._restored(t1, precision=slept)
        # Wait for the network (WireGuard handshake) but never forever: no internet is normal here.
        deadline = self.clock() + self.net_wait
        while not self.flush() and self.clock() < deadline:
            self.sleep(3)
        self.next_report = self.clock() + self.report_interval

    def _poweroff(self):
        self.emit("forced_poweroff", critical_pct=self.critical)
        self.state = OFF
        self._save(poweroff_at=self.clock())
        deadline = self.clock() + 10
        while not self.flush() and self.clock() < deadline:
            self.sleep(2)
        self.run(["sync"])
        self.run(self.cfg["POWEROFF_CMD"].split())


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s", stream=sys.stdout)
    cfg = load_config(os.environ.get("POWER_GUARD_CONF", "/etc/power-guard.conf"))
    if not cfg["TOKEN"]:
        log.warning("TOKEN is empty: the server will reject reports (events stay in the outbox)")
    g = Guard(cfg, Sysfs(cfg["AC_PATH"], cfg["BAT_PATH"]), Store(cfg["STATE_DIR"]),
              Sender(cfg["SERVER_URL"], cfg["TOKEN"]))

    def on_term(*_):
        g.stop()
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    g.start()
    while True:
        wait = g.step()
        if wait:
            time.sleep(wait)


if __name__ == "__main__":
    main()
