#!/usr/bin/env python3
"""power-guard: watches the laptop charger on the Proxmox host and reports to Svitlo-Local.

Runs on the Proxmox HOST (not in a container): only the host sees /sys/class/power_supply.
Standard library only, so nothing has to be installed on the host.

States:
  NORMAL      charger connected. Sends a sample every REPORT_INTERVAL seconds.
  ON_BATTERY  charger gone. Logs "ac_lost" at once; after SLEEP_AFTER_MIN minutes (default 0: at once,
              or when battery <= SLEEP_BELOW_PCT) it goes to a sleep loop:
              rtcwake -m mem -s WAKE_INTERVAL, wake for a few seconds, check, report, sleep again.
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
    "SLEEP_AFTER_MIN": "0",   # 0 = sleep right after the charger is gone (awake laptop drains fast)
    "SLEEP_BELOW_PCT": "30",
    "WAKE_INTERVAL": "300",
    "CRITICAL_PCT": "7",
    "KEEP_AWAKE": "false",
    "NET_WAIT_S": "8",        # after waking: try to report for this long, then sleep (unsent events wait)
    "MIN_AWAKE_S": "4",
    "DISK_DETACH_CMD": "",    # before every sleep on battery (idempotent), e.g. takes a failing disk away
    "DISK_ATTACH_CMD": "",    # after the charger is back and at the start of power-guard (idempotent)
    "WG_IFACE": "",           # WireGuard to the VPS: its peers are renewed after waking ("" = do not touch)
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


def link_up(net_dir="/sys/class/net") -> bool:
    """True when any real network card (one with a 'device'; not a bridge, veth or wg) has a link.
    True as well when there is nothing to check, so a missing /sys never blocks the reports."""
    cards = [p for p in Path(net_dir).glob("*") if (p / "device").exists()]
    for p in cards:
        try:
            if (p / "carrier").read_text().strip() == "1":
                return True
        except OSError:  # the card is down: reading carrier fails
            pass
    return not cards


class Sender:
    def __init__(self, url, token, timeout=5):
        self.url, self.token, self.timeout = url, token, timeout

    def send(self, payload, timeout=None) -> list:
        """POST to the server; returns ids of accepted events. Raises on any failure."""
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Device-Key": self.token})
        with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
            return json.loads(r.read()).get("acked", [])


def renew_wg_peers(iface) -> bool:
    """Replace the WireGuard peers of iface with fresh copies of themselves (same config, endpoints already
    resolved, no DNS needed). A fresh peer sends its handshake at once instead of waiting out the 5 s
    pause after a lost one. The config holds the private key: it only goes through the pipe."""
    try:
        conf = subprocess.run(["wg", "showconf", iface], capture_output=True, check=True, timeout=5).stdout
        subprocess.run(["wg", "setconf", iface, "/dev/stdin"], input=conf, capture_output=True, check=True, timeout=5)
        return True
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("cannot renew the WireGuard peers of %s: %s", iface, type(exc).__name__)
        return False


def run_cmd(args) -> int:
    log.info("run: %s", " ".join(args))
    try:
        return subprocess.run(args, check=False).returncode
    except OSError as exc:
        log.error("cannot run %s: %s", args[0], exc)
        return 127


# The outbox is only what the server has not confirmed yet; the journal keeps everything.
# With a wrong token nothing is ever confirmed, so keep it bounded and send it in portions.
OUTBOX_MAX = 500
SEND_MAX = 100
POWEROFF_RETRY_S = 300


class Guard:
    def __init__(self, cfg, sysfs, store, sender, run=run_cmd, clock=time.time, sleep=time.sleep, link=link_up,
                 renew_wg=renew_wg_peers):
        self.cfg, self.sysfs, self.store, self.sender = cfg, sysfs, store, sender
        self.run, self.clock, self.sleep, self.link, self.renew_wg = run, clock, sleep, link, renew_wg
        self.wg_iface = cfg.get("WG_IFACE", "")
        self.detach_cmd = cfg.get("DISK_DETACH_CMD", "").split()
        self.attach_cmd = cfg.get("DISK_ATTACH_CMD", "").split()
        self.attach_needed = bool(self.attach_cmd)  # at the start too: a crash may have left the disk detached
        self.report_interval = float(cfg["REPORT_INTERVAL"])
        self.sleep_after = float(cfg["SLEEP_AFTER_MIN"]) * 60
        self.sleep_below = float(cfg["SLEEP_BELOW_PCT"])
        self.wake_interval = int(float(cfg["WAKE_INTERVAL"]))
        self.critical = float(cfg["CRITICAL_PCT"])
        self.keep_awake = cfg["KEEP_AWAKE"].lower() in ("1", "true", "yes")
        self.net_wait = float(cfg["NET_WAIT_S"])
        # This Samsung reset itself twice when it was put back to sleep less than a second
        # after waking (21.10.2026). Stay awake a little so the hardware settles first
        # (the disk and the network card are back ~2.5 s after waking).
        self.min_awake = float(cfg["MIN_AWAKE_S"])
        self.state = NORMAL
        self.lost_at = None
        self.restore_precision = None  # seconds of uncertainty for the next ac_restored
        self.next_report = 0.0
        self.next_sleep_try = 0.0
        self.next_poweroff_try = 0.0
        self.backoff = 0.0
        self.woke_at = None
        self.slept_until = None  # when the last sleep ended, for "awake_s" in the next sleep_start
        self.net_ok = True
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
        self.store.save_outbox((self.store.outbox() + [e])[-OUTBOX_MAX:])
        log.info("event %s %s", type_, detail)
        return e

    def flush(self, timeout=None) -> bool:
        """Send the latest sample and all unsent events. Returns True on success."""
        out = self.store.outbox()[:SEND_MAX]  # oldest first; the rest goes with the next reports
        r = self.last
        sample = {"ts": self.clock(), "state": self.state, **{k: r.get(k) for k in ("ac", "battery", "status", "current_now")}}
        try:
            acked = set(self.sender.send({"sample": sample, "events": out}, timeout=timeout))
        except Exception as exc:  # network down, server down, bad token...
            log.warning("send failed: %s", exc)
            self.net_ok = False
            return False
        self.net_ok = True
        if acked:
            self.store.save_outbox([e for e in self.store.outbox() if e["id"] not in acked])
        return True

    # ---------- lifecycle ----------

    def start(self):
        """Called once at service start: recover the previous state from disk."""
        now = self.clock()
        self.woke_at = now  # a fresh boot / restart also gets the settle time before sleeping
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
            if not self.keep_awake and self.sleep_after == 0:
                self._save()
                return 0  # go straight to the next step, which sends "ac_lost" and falls asleep
        elif self.state == ON_BATTERY:
            if r["ac"]:
                self._restored(now)
            elif r["battery"] is not None and r["battery"] <= self.critical and now >= self.next_poweroff_try:
                self._poweroff()
                return self.report_interval
            elif not self.keep_awake and now >= self.next_sleep_try and (
                    now - (self.lost_at or now) >= self.sleep_after
                    or (r["battery"] is not None and r["battery"] <= self.sleep_below)):
                settle = 0 if self.woke_at is None else self.woke_at + self.min_awake - now
                if settle > 0:
                    return settle  # just woke up: let the hardware settle before the next sleep
                self._sleep_cycle()
                self._save()
                return 0  # check again right after waking
        elif self.state == OFF and r["ac"]:
            # the poweroff command was accepted but the laptop is still running and the charger is back
            self._restored(now)

        self._save()
        if now >= self.next_report:
            self.flush()
            self.next_report = now + self.report_interval
        if self.attach_needed and self.state == NORMAL:
            self.attach_needed = False
            self.run(self.attach_cmd)  # after the report: it may take a minute
        return self.report_interval

    def _restored(self, end, precision=None):
        detail = {"start": self.lost_at, "end": end}
        if self.lost_at:
            detail["duration"] = end - self.lost_at
        precision = precision if precision is not None else self.restore_precision
        if precision:
            detail["precision_s"] = precision
        self.emit("ac_restored", ts=end, **detail)
        self.state, self.lost_at, self.restore_precision, self.slept_until = NORMAL, None, None, None
        self.attach_needed = bool(self.attach_cmd)
        self.next_report = 0

    def _sleep_cycle(self):
        before = self.last.get("battery")
        if self.detach_cmd:
            self.run(self.detach_cmd)  # may take a minute (stops the NAS); the laptop is awake meanwhile
        t0 = self.clock()
        extra = {"awake_s": round(t0 - self.slept_until, 1)} if self.slept_until else {}
        self.emit("sleep_start", wake_in=self.wake_interval, **extra)
        if self.net_ok:  # no network a moment ago: do not wait for it again, the event stays in the outbox
            self.flush(timeout=2)
        # No separate "sync": the kernel syncs the disks itself on the way to sleep (/sys/power/sync_on_suspend).
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
        self.woke_at = self.slept_until = t1
        self.last = r = self.sysfs.read()
        slept = t1 - t0
        detail = {"slept_s": round(slept), "battery_before": before}
        if before is not None and r.get("battery") is not None and slept > 0:
            detail["drain_pct_per_h"] = round((before - r["battery"]) / slept * 3600, 2)
        self.emit("sleep_end", **detail)
        if r.get("ac"):
            # Charger came back sometime between t0 and t1; we only know it at wake-up.
            self._restored(t1, precision=slept)
        # Report, but never wait forever: no internet is normal here. Measured 26.10.2026: the network card
        # has its link 2.6 s after waking, the router answers only ~5 s after waking. A lost WireGuard
        # handshake is retried only 5 s later, so the tunnel came up at ~7.8 s. So: wait for the link, then
        # give WireGuard fresh peers before every short try (a fresh peer handshakes at once): the report
        # goes out as soon as the router lets it through.
        deadline = self.clock() + self.net_wait
        while not self.link() and self.clock() < deadline:
            self.sleep(0.2)
        while self.clock() < deadline:
            if self.wg_iface:
                self.renew_wg(self.wg_iface)
            if self.flush(timeout=max(0.3, min(0.8, deadline - self.clock()))):
                break
            self.sleep(0.2)
        self.next_report = self.clock() + self.report_interval

    def _poweroff(self):
        self.emit("forced_poweroff", critical_pct=self.critical)
        self.state = OFF
        self._save(poweroff_at=self.clock())
        deadline = self.clock() + 10
        while not self.flush() and self.clock() < deadline:
            self.sleep(2)
        self.run(["sync"])
        rc = self.run(self.cfg["POWEROFF_CMD"].split())
        if rc != 0:
            # Still running: go on as "on battery" (sleep cycles keep saving power) and retry later,
            # otherwise the state would stay OFF and the charger coming back would go unnoticed.
            log.error("poweroff command failed (rc=%s), retry in %d s", rc, POWEROFF_RETRY_S)
            self.state = ON_BATTERY
            self.next_poweroff_try = self.clock() + POWEROFF_RETRY_S
            self._save()


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
