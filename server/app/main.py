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
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from typing import Any

from fastapi import Body, Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse

from . import config, db, firmware, home, schedule, stats

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

def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        raise HTTPException(400, "not a number")


# Plain "def" (not async): FastAPI runs these in a thread pool, so a slow SQLite write
# (busy_timeout up to 30 s while the poller holds the lock) does not stop the whole web server.
@app.post("/api/power")
def api_power(request: Request, body: Any = Body(None), x_device_key: str | None = Header(None), c=Depends(con)):
    """Reports from power-guard: {"sample": {...}, "events": [{id, ts, type, ...}]}.
    Returns ids of accepted events so power-guard can drop them from its outbox."""
    rate_limit(request)
    check_key(x_device_key, config.POWER_KEY_SHA256)
    if not isinstance(body, dict) or not isinstance(body.get("events") or [], list):
        raise HTTPException(400, "expected {sample, events}")
    acked = []
    for e in body.get("events") or []:
        if not isinstance(e, dict) or not isinstance(e.get("id"), str):
            continue
        try:
            row = home.map_event(e, esp=config.home_esp_configured())
        except (TypeError, ValueError):
            row = None
        if row is not None:
            db.add_event(c, row)  # duplicates are ignored but still acknowledged
        # unknown or broken events are acknowledged too: they can never be stored, and keeping
        # them would grow the outbox on the laptop forever (its journal still has them)
        acked.append(e["id"])
    s = body.get("sample")
    if isinstance(s, dict) and s.get("ts") is not None:
        c.execute("INSERT OR REPLACE INTO power_samples(ts,ac,battery,status,current_now,state) VALUES(?,?,?,?,?,?)",
                  (_num(s["ts"]), int(bool(s.get("ac"))), _num(s.get("battery")), str(s.get("status") or "")[:40],
                   _num(s.get("current_now")), str(s.get("state") or "")[:20]))
    return {"ok": True, "acked": acked, "server_time": time.time()}


@app.post("/api/heartbeat")
def api_heartbeat(request: Request, detail: Any = Body(None), x_device_key: str | None = Header(None),
                  c=Depends(con)):
    """Direct mode: ESP posts here (only when VILLAGE_SOURCE=direct)."""
    rate_limit(request)
    if config.VILLAGE_SOURCE != "direct":
        raise HTTPException(404, "direct mode is off")
    check_key(x_device_key, config.HEARTBEAT_KEY_SHA256)
    if not isinstance(detail, dict):
        detail = {}
    c.execute("INSERT INTO inbox(ts, detail) VALUES(?,?)", (time.time(), json.dumps(detail)[:2000]))
    return {"ok": True}


# ---------- dashboard API ----------

def _last_event(c, source, types):
    q = f"SELECT * FROM events WHERE source=? AND type IN ({','.join('?' * len(types))}) ORDER BY ts DESC LIMIT 1"
    r = c.execute(q, (source, *types)).fetchone()
    return db.row_to_event(r) if r else None


def same_origin(request: Request):
    """Dashboard buttons: refuse a request that a page from another site made the browser send.
    The dashboard is reachable only through WireGuard, this is the second lock. No Origin = not a browser."""
    origin = request.headers.get("origin")
    if origin is not None and urlparse(origin).netloc != request.headers.get("host", ""):
        raise HTTPException(403, "request from a foreign page")


def _board(body):
    """The ESP a dashboard command is meant for: the village one (default) or the one at home.
    Returns (target, relay credentials, meta key that asks the poller to confirm sooner)."""
    target = body.get("target", "village") if isinstance(body, dict) else "village"
    if target == "home":
        if not config.home_esp_configured():
            raise HTTPException(503, "the home ESP is not configured")
        return target, (config.HC_HOME_API_KEY, config.HC_HOME_CHECK_UUID), "diag_soon_home"
    if target != "village":
        raise HTTPException(400, "target must be village or home")
    if not config.hc_configured():
        raise HTTPException(503, "Healthchecks is not configured")
    return target, (config.HC_API_KEY, config.HC_CHECK_UUID), "diag_soon"


@app.post("/api/esp/display")
def api_esp_display(request: Request, body: Any = Body(None), c=Depends(con)):
    """Dashboard button: monitoring mode on the ESP (display off) or back to normal.
    Goes through the Healthchecks check description; the ESP picks it up within ~2 minutes."""
    rate_limit(request)
    same_origin(request)
    if not config.hc_configured():
        raise HTTPException(503, "Healthchecks is not configured")  # the display exists only on the village board
    on = bool(body.get("on")) if isinstance(body, dict) else False
    now = int(time.time())
    import httpx
    from .healthchecks import HealthchecksRelay
    cmd = {"on": on, "at": now}
    try:
        with httpx.Client() as http:
            HealthchecksRelay(http, config.HC_API_BASE, config.HC_API_KEY, config.HC_CHECK_UUID).set_mode(
                "normal" if on else "monitor", now)
        cmd["ok"] = True
    except Exception as exc:
        cmd.update(ok=False, error=str(exc)[:200].replace(config.HC_CHECK_UUID, "<uuid>"))
    db.set_meta(c, "esp_cmd", cmd)
    db.set_meta(c, "diag_soon", True)
    if not cmd["ok"]:
        raise HTTPException(502, cmd["error"])
    return cmd


ESP_COMMANDS = ("reboot", "sensor", "ota", "golden")


@app.get("/api/esp/firmware")
def api_esp_firmware():
    """Signed firmware versions on GitHub Releases, for the update button."""
    import httpx
    with httpx.Client() as http:
        rel = firmware.releases(http, config.FW_REPO)
    return {**rel, "repo": config.FW_REPO, "commands": bool(config.ESP_CMD_KEY)}


@app.post("/api/esp/command")
def api_esp_command(request: Request, body: Any = Body(None), c=Depends(con)):
    """Dashboard buttons: reboot the ESP, re-read its sensor, update its firmware from GitHub.
    The command is signed with ESP_CMD_KEY (the board has the same key) and goes through the
    Healthchecks check description like the display button; the ESP picks it up within ~2 minutes."""
    rate_limit(request)
    same_origin(request)
    if "application/json" not in request.headers.get("content-type", ""):
        raise HTTPException(415, "JSON only")  # a cross-site HTML form cannot send JSON
    if not config.ESP_CMD_KEY:
        raise HTTPException(503, "ESP_CMD_KEY is not set on the server")
    target, (api_key, uuid), soon_key = _board(body)
    kind = body.get("cmd") if isinstance(body, dict) else None
    if kind not in ESP_COMMANDS:
        raise HTTPException(400, "cmd must be one of " + ", ".join(ESP_COMMANDS))
    import httpx
    from .healthchecks import HealthchecksRelay, sign_cmd
    cmd = kind
    with httpx.Client() as http:
        if kind == "ota":
            ver = str(body.get("version") or "")
            if not firmware.VERSION_RE.match(ver):
                raise HTTPException(400, "bad version")
            if ver not in [v["version"] for v in firmware.releases(http, config.FW_REPO)["versions"]]:
                raise HTTPException(400, f"release v{ver} has no {firmware.ASSET}")
            cmd = f"ota:{ver}"
        now = int(time.time())
        sent = {"cmd": cmd, "at": now}
        try:
            HealthchecksRelay(http, config.HC_API_BASE, api_key, uuid).set_cmd(
                sign_cmd(config.ESP_CMD_KEY, cmd, now))
            sent["ok"] = True
        except Exception as exc:
            sent.update(ok=False, error=str(exc)[:200].replace(uuid, "<uuid>"))
    db.set_meta(c, "esp_cmd_sent" if target == "village" else "esp_cmd_sent_home", sent)
    db.set_meta(c, soon_key, True)
    if not sent["ok"]:
        raise HTTPException(502, sent["error"])
    return sent


@app.get("/api/status")
def api_status(c=Depends(con)):
    now = time.time()
    vs = db.get_meta(c, "village_state", {}) or {}
    village = {
        "status": vs.get("status", "never"), "since": vs.get("since"), "last_ping": vs.get("last_ping_ts"),
        "mode": db.get_meta(c, "village_mode", config.VILLAGE_SOURCE),
        "obs": db.get_meta(c, "village_obs", {}),
        "threshold_s": config.VILLAGE_THRESHOLD_S,
        "esp_cmd": db.get_meta(c, "esp_cmd"),
        "esp_cmd_sent": db.get_meta(c, "esp_cmd_sent"),
    }
    sample = c.execute("SELECT * FROM power_samples ORDER BY ts DESC LIMIT 1").fetchone()
    lsrc = home.LAPTOP if config.home_esp_configured() else "home"
    ctrl = _last_event(c, lsrc, ("sleep_start", "sleep_end", "forced_poweroff", "guard_start"))
    h = home.status(dict(sample) if sample else None, ctrl, now, config.HOME_STALE_S, config.HOME_WAKE_INTERVAL_S)
    lost = _last_event(c, lsrc, ("laptop_ac_lost", "laptop_ac_restored") if lsrc != "home"
                       else ("power_lost", "power_restored"))
    if h.get("status") == "sleeping" and lost and lost["type"] in ("power_lost", "laptop_ac_lost"):
        h["since"] = lost["ts"]  # without light since then; the last sleep_start is only the latest 5-minute nap
    if h.get("status") in ("online", "on_battery"):
        h["since"] = lost["ts"] if lost and lost["type"] in ("power_lost", "laptop_ac_lost") else (lost["ts"] if lost else None)
        # After a manual start, remind for a day that the server had been off.
        gap = _last_event(c, lsrc, ("gap_unknown",))
        if gap and gap["detail"].get("reason") == "poweroff" and now - gap["ts"] < 86400:
            h["info"] = "Домашній сервер був вимкнений (розряд) і запущений вручну."
            h["info_start"], h["info_end"] = gap["start"], gap["end"]
    out = {"now": now, "village": village, "home": h}
    if config.home_esp_configured():
        # The ESP at home watches the light; the laptop is only a server then (battery, sleep), shown separately.
        hs = db.get_meta(c, "home_state", {}) or {}
        esp = {
            "status": hs.get("status", "never"), "since": hs.get("since"), "last_ping": hs.get("last_ping_ts"),
            "obs": db.get_meta(c, "home_obs", {}), "esp_cmd_sent": db.get_meta(c, "esp_cmd_sent_home"),
        }
        laptop = dict(h)
        out["laptop"] = laptop
        out["home"] = esp
        out["home_esp"] = True
    tick = db.get_meta(c, "poller_tick")
    out["poller_alive"] = bool(tick and now - tick < 90)
    return out


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
    if st.get("home_esp"):
        if h.get("status") == "unknown" and h.get("since"):
            out.append({"source": "home", "kind": "unknown", "start": h["since"], "end": None})
    elif h.get("status") in ("off", "no_data") and h.get("since"):
        out.append({"source": "home", "kind": "unknown", "start": h["since"], "end": None})
    # Before the first data point we know nothing: the dashboard paints that part grey.
    # Home: the laptop's samples (until the ESP took over) or the ESP's first ping, whichever is older.
    first_home = [x for x in (c.execute("SELECT MIN(ts) FROM power_samples").fetchone()[0],
                              c.execute("SELECT MIN(ts) FROM pings WHERE source='home'").fetchone()[0]) if x is not None]
    first = {
        "village": c.execute("SELECT MIN(ts) FROM pings WHERE source='village'").fetchone()[0],
        "home": min(first_home) if first_home else None,
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
    planned = {r["date"]: schedule.intervals(r["date"], json.loads(r["hours"]), config.TZ_NAME)
               for r in c.execute("SELECT date, hours FROM schedule_days WHERE source=?", (source,))}
    out = stats.daily(periods, p["first"][source], now, days, config.TZ_NAME,
                      end_day=today - timedelta(days=week * days), planned=planned)
    target = config.schedule_targets().get(source)
    st = (db.get_meta(c, "schedule_status", {}) or {}).get(target[0]) if target else None
    out["schedule_info"] = {"group": target[1].replace("GPV", "") if target else None,
                            "region": target[0] if target else None, "status": st}
    return out


@app.get("/api/sensors")
def api_sensors(hours: float = 24, c=Depends(con)):
    """Village temperature / humidity / pressure for the chart, plus the outdoor temperature (Open-Meteo)."""
    now = time.time()
    since = now - max(1.0, min(hours, 24 * 92)) * 3600
    rows = c.execute("SELECT ts, t, h, p FROM sensor_samples WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    step = max(1, len(rows) // 600)
    last = c.execute("SELECT ts, t, h, p FROM sensor_samples ORDER BY ts DESC LIMIT 1").fetchone()
    out = c.execute("SELECT ts, t FROM outdoor_samples WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    out_last = c.execute("SELECT ts, t FROM outdoor_samples ORDER BY ts DESC LIMIT 1").fetchone()
    return {"from": since, "to": now, "rows": [dict(r) for r in rows[::step]], "last": dict(last) if last else None,
            "outdoor": [dict(r) for r in out[::max(1, len(out) // 600)]],
            "outdoor_last": dict(out_last) if out_last else None}


@app.get("/api/battery")
def api_battery(hours: float = 24, c=Depends(con)):
    now = time.time()
    since = now - max(1.0, min(hours, 24 * 31)) * 3600
    rows = c.execute("SELECT ts, ac, battery FROM power_samples WHERE ts>=? ORDER BY ts", (since,)).fetchall()
    step = max(1, len(rows) // 600)  # at most ~600 points for the chart
    # On charger the laptop reports every 30 s ([::step] is fine for that density), but while
    # sleeping it wakes every 30 min == 60 records, a multiple of the 24 h step (4): all outage
    # samples then share one slice residue and the plain slice can drop the whole outage window.
    # Every charger-off sample (and the newest one) is therefore kept unthinned.
    return [dict(r) for i, r in enumerate(rows)
            if i % step == 0 or not r["ac"] or i == len(rows) - 1]


@app.get("/")
def index():
    # no-cache: after an update the browser must not keep showing the old page
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})
