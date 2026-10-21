"""Web app: API for devices + dashboard.

Run: uvicorn app.main:app
"""
import hashlib
import hmac
import json
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from . import config, db, home, stats

app = FastAPI(title="Svitlo-Local", docs_url=None, redoc_url=None)
STATIC = Path(__file__).parent / "static"

# Events that matter to a human; the rest is the "technical" log.
MAIN_TYPES = ("power_lost", "power_restored", "link_restored", "gap_unknown",
              "no_data", "forced_poweroff", "first_ping")


def con():
    """One connection per request, always closed afterwards."""
    c = db.connect()
    c.isolation_level = None
    try:
        yield c
    finally:
        c.close()


# ---------- auth + rate limit ----------

_hits: dict[str, deque] = defaultdict(deque)


def rate_limit(request: Request):
    ip = request.client.host if request.client else "?"
    q, now = _hits[ip], time.time()
    while q and now - q[0] > 60:
        q.popleft()
    if len(q) >= config.RATE_LIMIT_PER_MIN:
        raise HTTPException(429, "too many requests")
    q.append(now)


def check_key(key: str | None, expected_sha256: str):
    if not expected_sha256:
        raise HTTPException(503, "source not configured")
    got = hashlib.sha256((key or "").encode()).hexdigest()
    if not hmac.compare_digest(got, expected_sha256):
        raise HTTPException(401, "bad device key")


# ---------- device API ----------

@app.post("/api/power")
async def api_power(request: Request, x_device_key: str | None = Header(None), c=Depends(con)):
    """Reports from power-guard: {"sample": {...}, "events": [{id, ts, type, ...}]}.
    Returns ids of accepted events so power-guard can drop them from its outbox."""
    rate_limit(request)
    check_key(x_device_key, config.POWER_KEY_SHA256)
    body = await request.json()
    acked = []
    for e in body.get("events") or []:
        row = home.map_event(e)
        if row is None:
            continue
        db.add_event(c, row)  # duplicates are ignored but still acknowledged
        acked.append(e["id"])
    s = body.get("sample")
    if s and s.get("ts") is not None:
        c.execute("INSERT OR REPLACE INTO power_samples(ts,ac,battery,status,current_now,state) VALUES(?,?,?,?,?,?)",
                  (float(s["ts"]), int(bool(s.get("ac"))), s.get("battery"), s.get("status"),
                   s.get("current_now"), s.get("state")))
    return {"ok": True, "acked": acked, "server_time": time.time()}


@app.post("/api/heartbeat")
async def api_heartbeat(request: Request, x_device_key: str | None = Header(None), c=Depends(con)):
    """Direct mode: ESP posts here (only when VILLAGE_SOURCE=direct)."""
    rate_limit(request)
    if config.VILLAGE_SOURCE != "direct":
        raise HTTPException(404, "direct mode is off")
    check_key(x_device_key, config.HEARTBEAT_KEY_SHA256)
    try:
        detail = await request.json()
    except (ValueError, UnicodeDecodeError):
        detail = {}
    if not isinstance(detail, dict):
        detail = {}
    c.execute("INSERT INTO inbox(ts, detail) VALUES(?,?)", (time.time(), json.dumps(detail)[:2000]))
    return {"ok": True}


# ---------- dashboard API ----------

def _last_event(c, source, types):
    q = f"SELECT * FROM events WHERE source=? AND type IN ({','.join('?' * len(types))}) ORDER BY ts DESC LIMIT 1"
    r = c.execute(q, (source, *types)).fetchone()
    return db.row_to_event(r) if r else None


@app.get("/api/status")
def api_status(c=Depends(con)):
    now = time.time()
    vs = db.get_meta(c, "village_state", {}) or {}
    village = {
        "status": vs.get("status", "never"), "since": vs.get("since"), "last_ping": vs.get("last_ping_ts"),
        "mode": db.get_meta(c, "village_mode", config.VILLAGE_SOURCE),
        "obs": db.get_meta(c, "village_obs", {}),
        "threshold_s": config.VILLAGE_THRESHOLD_S,
    }
    sample = c.execute("SELECT * FROM power_samples ORDER BY ts DESC LIMIT 1").fetchone()
    ctrl = _last_event(c, "home", ("sleep_start", "sleep_end", "forced_poweroff", "guard_start"))
    h = home.status(dict(sample) if sample else None, ctrl, now, config.HOME_STALE_S, config.HOME_WAKE_INTERVAL_S)
    if h.get("status") in ("online", "on_battery"):
        lost = _last_event(c, "home", ("power_lost", "power_restored"))
        h["since"] = lost["ts"] if lost else None
        # After a manual start, remind for a day that the server had been off.
        gap = _last_event(c, "home", ("gap_unknown",))
        if gap and gap["detail"].get("reason") == "poweroff" and now - gap["ts"] < 86400:
            h["info"] = "Домашній сервер був вимкнений (розряд) і запущений вручну."
            h["info_start"], h["info_end"] = gap["start"], gap["end"]
    tick = db.get_meta(c, "poller_tick")
    return {"now": now, "village": village, "home": h,
            "poller_alive": bool(tick and now - tick < 90)}


@app.get("/api/events")
def api_events(limit: int = 100, technical: bool = False, c=Depends(con)):
    limit = max(1, min(limit, 1000))
    if technical:
        rows = c.execute("SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)).fetchall()
    else:
        q = f"SELECT * FROM events WHERE type IN ({','.join('?' * len(MAIN_TYPES))}) ORDER BY ts DESC LIMIT ?"
        rows = c.execute(q, (*MAIN_TYPES, limit)).fetchall()
    return [db.row_to_event(r) for r in rows]


@app.get("/api/periods")
def api_periods(hours: float = 24, c=Depends(con)):
    """Closed outage / unknown / no-link periods overlapping the window, plus the open one."""
    now = time.time()
    since = now - max(1.0, min(hours, 24 * 92)) * 3600
    rows = c.execute(
        "SELECT source,type,start,end,detail FROM events WHERE type IN ('power_restored','gap_unknown','link_restored') "
        "AND start IS NOT NULL AND end >= ? ORDER BY start", (since,)).fetchall()
    out = [{"source": r["source"], "kind": {"power_restored": "outage", "gap_unknown": "unknown",
                                            "link_restored": "nolink"}[r["type"]],
            "start": r["start"], "end": r["end"]} for r in rows]
    st = api_status(c)
    if st["village"]["mode"] != "direct":
        # In relay mode Healthchecks keeps the pings while our server is down,
        # so our own downtime is not a gap in village data.
        out = [p for p in out if p["source"] != "server"]
    v = st["village"]
    if v["status"] in ("offline", "unknown") and v["since"]:
        out.append({"source": "village", "kind": "outage" if v["status"] == "offline" else "unknown",
                    "start": v["since"], "end": None})
    h = st["home"]
    lost = _last_event(c, "home", ("power_lost", "power_restored"))
    if lost and lost["type"] == "power_lost":
        out.append({"source": "home", "kind": "outage", "start": lost["ts"], "end": None})
    if h.get("status") in ("off", "no_data") and h.get("since"):
        out.append({"source": "home", "kind": "unknown", "start": h["since"], "end": None})
    # Before the first data point we know nothing: the dashboard paints that part grey.
    first = {
        "village": c.execute("SELECT MIN(ts) FROM pings WHERE source='village'").fetchone()[0],
        "home": c.execute("SELECT MIN(ts) FROM power_samples").fetchone()[0],
    }
    return {"from": since, "to": now, "periods": out, "first": first}


@app.get("/api/daily")
def api_daily(source: str = "home", days: int = 7, week: int = 0, c=Depends(con)):
    """Weekly chart: one row per local day. week=0 is the last 7 days, week=1 the 7 before, ..."""
    if source not in ("home", "village"):
        raise HTTPException(400, "source must be home or village")
    days = max(1, min(days, 31))
    week = max(0, min(week, 52))
    now = time.time()
    p = api_periods(hours=(week + 1) * days * 24 + 24, c=c)
    periods = [x for x in p["periods"] if x["source"] == source or (source == "village" and x["source"] == "server")]
    today = datetime.fromtimestamp(now, ZoneInfo(config.TZ_NAME)).date()
    return stats.daily(periods, p["first"][source], now, days, config.TZ_NAME,
                       end_day=today - timedelta(days=week * days))


@app.get("/api/battery")
def api_battery(hours: float = 24, c=Depends(con)):
    now = time.time()
    since = now - max(1.0, min(hours, 24 * 31)) * 3600
    rows = c.execute("SELECT ts, ac, battery FROM power_samples WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    step = max(1, len(rows) // 600)  # at most ~600 points for the chart
    return [dict(r) for r in rows[::step]]


@app.get("/")
def index():
    # no-cache: after an update the browser must not keep showing the old page
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
