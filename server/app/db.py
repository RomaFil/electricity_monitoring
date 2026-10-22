"""SQLite storage. Web and poller share one file in WAL mode;
only the poller writes village state, only the web writes home reports."""
import json
import sqlite3
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS events(
    id      INTEGER PRIMARY KEY,
    source  TEXT NOT NULL,          -- village | home | server
    type    TEXT NOT NULL,          -- power_lost, power_restored, gap_unknown, ...
    ts      REAL NOT NULL,          -- when it happened (unix seconds)
    start   REAL,                   -- for periods: start/end
    end     REAL,
    detail  TEXT,                   -- json
    ext_id  TEXT UNIQUE,            -- dedup key for events sent by devices
    created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS events_ts ON events(source, ts);

CREATE TABLE IF NOT EXISTS pings(
    id      INTEGER PRIMARY KEY,
    source  TEXT NOT NULL,
    ts      REAL NOT NULL,
    seq     INTEGER,
    detail  TEXT,
    UNIQUE(source, seq)
);
CREATE INDEX IF NOT EXISTS pings_ts ON pings(source, ts);

CREATE TABLE IF NOT EXISTS power_samples(
    ts          REAL PRIMARY KEY,
    ac          INTEGER,
    battery     REAL,
    status      TEXT,
    current_now REAL,
    state       TEXT
);

-- village temperature / humidity / pressure from the ESP (BME280), ~every 5 minutes
CREATE TABLE IF NOT EXISTS sensor_samples(
    ts      REAL PRIMARY KEY,       -- ping time the reading came with
    t       REAL,                   -- °C
    h       REAL,                   -- %
    p       REAL                    -- hPa
);

-- official schedule per source and local day: 24 hour values ("yes", "no", "first", ...), latest version
CREATE TABLE IF NOT EXISTS schedule_days(
    source  TEXT NOT NULL,
    date    TEXT NOT NULL,          -- YYYY-MM-DD, Europe/Kyiv
    hours   TEXT NOT NULL,          -- json list of 24 values
    grp     TEXT,                   -- e.g. GPV4.2
    updated TEXT,                   -- "update" string from the source (dd.MM.yyyy HH:mm)
    fetched REAL,
    PRIMARY KEY(source, date)
);

-- direct-mode heartbeats wait here until the poller processes them
CREATE TABLE IF NOT EXISTS inbox(
    id      INTEGER PRIMARY KEY,
    ts      REAL NOT NULL,
    detail  TEXT
);

CREATE TABLE IF NOT EXISTS meta(
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""


def connect(path=None):
    # check_same_thread=False: FastAPI may open the connection in one thread and use it in another
    con = sqlite3.connect(path or config.DB_PATH, timeout=30, check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=30000")
    con.executescript(SCHEMA)
    return con


def get_meta(con, key, default=None):
    row = con.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def set_meta(con, key, value):
    con.execute("INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, json.dumps(value)))


def add_event(con, e: dict) -> bool:
    """Insert an event; returns False if ext_id already exists (duplicate)."""
    cur = con.execute(
        "INSERT OR IGNORE INTO events(source,type,ts,start,end,detail,ext_id,created) VALUES(?,?,?,?,?,?,?,?)",
        (e["source"], e["type"], e["ts"], e.get("start"), e.get("end"),
         json.dumps(e.get("detail") or {}, ensure_ascii=False), e.get("ext_id"), time.time()))
    return cur.rowcount == 1


def add_sample(con, ts, detail: dict):
    """Store t/h/p from an ESP ping body, if it has them."""
    def num(k):
        try:
            return float(detail[k]) if detail.get(k) is not None else None
        except (TypeError, ValueError):
            return None
    t = num("t")
    if ts is None or t is None:
        return
    con.execute("INSERT OR IGNORE INTO sensor_samples(ts,t,h,p) VALUES(?,?,?,?)", (ts, t, num("h"), num("p")))


def row_to_event(r) -> dict:
    d = dict(r)
    d["detail"] = json.loads(d["detail"] or "{}")
    return d
