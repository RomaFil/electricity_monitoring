"""Background worker: reads the village heartbeat source and runs the state machine.

Run: python -m app.poller
"""
import json
import logging
import time

import httpx

from . import config, db, outdoor, schedule
from .healthchecks import HealthchecksRelay
from .village import Ping, State, on_ping, on_tick

log = logging.getLogger("poller")
TICK_S = 15


def save_events(con, events):
    for e in events:
        db.add_event(con, e)
        log.info("event %s %s", e["type"], e.get("detail"))


def record_server_gap(con, now):
    """If the poller itself was down, mark that time as unknown (not as an outage)."""
    last = db.get_meta(con, "poller_tick")
    if last and now - last > 3 * max(TICK_S, config.POLL_INTERVAL_S):
        db.add_event(con, {"source": "server", "type": "gap_unknown", "ts": now,
                           "start": last, "end": now, "detail": {"reason": "server down"}})
        log.warning("server was down %.0f s", now - last)


def store_pings(con, pings):
    for p in pings:
        db.add_sample(con, p.ts, p.detail or {})
        con.execute("INSERT OR IGNORE INTO pings(source,ts,seq,detail) VALUES('village',?,?,?)",
                    (p.ts, p.seq, json.dumps({**(p.detail or {}), "uptime_s": p.uptime_s,
                                              "contiguous": p.contiguous}, ensure_ascii=False)))


def process(con, st, pings):
    for p in pings:
        st, ev = on_ping(st, p, config.VILLAGE_THRESHOLD_S)
        save_events(con, ev)
    return st


class Relay:
    """healthchecks-relay source."""

    def __init__(self, con):
        self.con = con
        self.client = HealthchecksRelay(httpx.Client(), config.HC_API_BASE,
                                        config.HC_API_KEY, config.HC_CHECK_UUID)
        self.next_poll = 0.0
        self.next_diag = 0.0

    def step(self, st, now):
        obs = db.get_meta(self.con, "village_obs", {})
        if now >= self.next_poll:
            self.next_poll = now + config.POLL_INTERVAL_S
            cursor = db.get_meta(self.con, "hc_cursor")
            try:
                pings, cursor, complete = self.client.poll(cursor, st.last_ping_ts, config.VILLAGE_THRESHOLD_S)
                store_pings(self.con, pings)
                st = process(self.con, st, pings)
                db.set_meta(self.con, "hc_cursor", cursor)
                # keep the last ESP diagnostics (IP, display, RSSI): they are refreshed only every few minutes
                obs = {**obs, "ok": True, "complete": complete, "last_ok": now, "error": None}
                if db.get_meta(self.con, "diag_soon"):  # a dashboard command was sent: confirm it sooner
                    db.set_meta(self.con, "diag_soon", False)
                    self.next_diag = 0
                if now >= self.next_diag and cursor:
                    self.next_diag = now + config.DIAG_BODY_EVERY_S
                    obs["diag"] = self.client.body(cursor)
                    obs["diag_at"] = now
                    db.add_sample(self.con, st.last_ping_ts, obs["diag"])
            except Exception as exc:  # network, 4xx/5xx, bad json
                log.warning("healthchecks poll failed: %s", exc)
                obs = {**obs, "ok": False, "complete": False, "error": str(exc)[:200]}
            db.set_meta(self.con, "village_obs", obs)
        fresh = obs.get("last_ok") and now - obs["last_ok"] < 2.5 * config.POLL_INTERVAL_S
        return st, bool(obs.get("ok") and obs.get("complete") and fresh)


class Direct:
    """direct source: heartbeats land in the inbox table via POST /api/heartbeat."""

    def __init__(self, con, observing_since):
        self.con = con
        self.observing_since = observing_since

    def step(self, st, now):
        rows = self.con.execute("SELECT id, ts, detail FROM inbox ORDER BY ts").fetchall()
        pings = []
        last_ts = st.last_ping_ts
        for r in rows:
            d = json.loads(r["detail"] or "{}")
            up = d.get("uptime_s")
            contiguous = last_ts is not None and last_ts >= self.observing_since
            pings.append(Ping(ts=r["ts"], uptime_s=float(up) if isinstance(up, (int, float)) else None,
                              contiguous=contiguous, detail=d))
            last_ts = r["ts"]
        store_pings(self.con, pings)
        st = process(self.con, st, pings)
        if rows:
            self.con.execute("DELETE FROM inbox WHERE id<=?", (rows[-1]["id"],))
        db.set_meta(self.con, "village_obs", {"ok": True, "complete": True, "last_ok": now})
        complete = st.last_ping_ts is not None and st.last_ping_ts >= self.observing_since
        return st, complete


class Schedules:
    """Downloads the official schedules (one JSON per region) and stores the days of our groups."""

    def __init__(self, con):
        self.con = con
        self.http = httpx.Client(timeout=30, follow_redirects=True)
        self.etags = {}
        self.next = 0.0

    def step(self, now):
        if now < self.next:
            return
        self.next = now + config.SCHEDULE_EVERY_S
        status = db.get_meta(self.con, "schedule_status", {}) or {}
        targets = config.schedule_targets()
        for region in sorted({r for r, _ in targets.values()}):
            url = f"{config.SCHEDULE_BASE_URL}/{region}.json"
            try:
                headers = {"If-None-Match": self.etags[region]} if region in self.etags else {}
                r = self.http.get(url, headers=headers)
                if r.status_code == 304:  # unchanged since the last download
                    status[region] = {**status.get(region, {}), "ok": True, "checked": now}
                    continue
                r.raise_for_status()
                data = r.json()
                if r.headers.get("etag"):
                    self.etags[region] = r.headers["etag"]
                for source, (reg, group) in targets.items():
                    if reg != region:
                        continue
                    for date, hours in schedule.group_days(data, group, config.TZ_NAME).items():
                        self.con.execute(
                            "INSERT INTO schedule_days(source,date,hours,grp,updated,fetched) VALUES(?,?,?,?,?,?) "
                            "ON CONFLICT(source,date) DO UPDATE SET hours=excluded.hours, grp=excluded.grp, "
                            "updated=excluded.updated, fetched=excluded.fetched",
                            (source, date, json.dumps(hours), group, (data.get("fact") or {}).get("update"), now))
                status[region] = {"ok": True, "checked": now, "update": (data.get("fact") or {}).get("update"),
                                  "source_ok": (data.get("lastUpdateStatus") or {}).get("ok")}
            except Exception as exc:
                log.warning("schedule %s failed: %s", region, exc)
                status[region] = {**status.get(region, {}), "ok": False, "checked": now, "error": str(exc)[:200]}
        db.set_meta(self.con, "schedule_status", status)


class Outdoor:
    """Hourly outdoor temperature at the village (Open-Meteo)."""

    def __init__(self, con):
        self.con = con
        self.http = httpx.Client(timeout=30)
        self.next = 0.0

    def step(self, now):
        if now < self.next:
            return
        self.next = now + config.OUTDOOR_EVERY_S
        empty = self.con.execute("SELECT 1 FROM outdoor_samples LIMIT 1").fetchone() is None
        try:
            r = self.http.get(outdoor.URL, params=outdoor.params(float(config.OUTDOOR_LAT), float(config.OUTDOOR_LON),
                                                                 7 if empty else 2))
            r.raise_for_status()
            rows = outdoor.parse(r.json(), now)
            self.con.executemany("INSERT INTO outdoor_samples(ts,t) VALUES(?,?) "
                                 "ON CONFLICT(ts) DO UPDATE SET t=excluded.t", rows)
            db.set_meta(self.con, "outdoor_status", {"ok": True, "checked": now})
        except Exception as exc:
            log.warning("outdoor temperature failed: %s", exc)
            db.set_meta(self.con, "outdoor_status", {"ok": False, "checked": now, "error": str(exc)[:200]})


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    # httpx logs every request URL, and the URL contains the secret check UUID
    logging.getLogger("httpx").setLevel(logging.WARNING)
    con = db.connect()
    con.isolation_level = None  # autocommit; every statement is its own transaction
    now = time.time()
    record_server_gap(con, now)

    mode = config.VILLAGE_SOURCE
    if mode == "healthchecks" and not config.hc_configured():
        log.warning("healthchecks source selected but HC_API_KEY / HC_CHECK_UUID are empty; village disabled")
        mode = "off"
    db.set_meta(con, "village_mode", mode)
    source = Relay(con) if mode == "healthchecks" else Direct(con, now) if mode == "direct" else None
    log.info("poller started, village source: %s", mode)
    schedules = Schedules(con) if config.schedule_targets() else None
    outdoor_temp = Outdoor(con) if config.OUTDOOR_LAT else None

    while True:
        now = time.time()
        if source:
            st = State.from_dict(db.get_meta(con, "village_state"))
            st, complete = source.step(st, now)
            st, ev = on_tick(st, now, complete, config.VILLAGE_THRESHOLD_S)
            save_events(con, ev)
            db.set_meta(con, "village_state", st.to_dict())
        if schedules:
            schedules.step(now)
        if outdoor_temp:
            outdoor_temp.step(now)
        db.set_meta(con, "poller_tick", now)
        time.sleep(TICK_S)


if __name__ == "__main__":
    main()
