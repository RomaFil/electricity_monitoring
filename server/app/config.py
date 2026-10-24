"""Settings from environment (.env via docker compose)."""
import os


def _f(name, default):
    return float(os.environ.get(name, default))


DB_PATH = os.environ.get("DB_PATH", "/data/svitlo.db")
TZ_NAME = os.environ.get("TZ", "Europe/Kyiv")

# Village source: "healthchecks" (relay), "direct" (/api/heartbeat) or "off"
VILLAGE_SOURCE = os.environ.get("VILLAGE_SOURCE", "healthchecks")
VILLAGE_THRESHOLD_S = _f("VILLAGE_THRESHOLD_S", 180)   # 3 missed pings of 60 s
HC_API_BASE = os.environ.get("HC_API_BASE", "https://healthchecks.io")
HC_API_KEY = os.environ.get("HC_API_KEY", "")
HC_CHECK_UUID = os.environ.get("HC_CHECK_UUID", "")
POLL_INTERVAL_S = _f("POLL_INTERVAL_S", 60)
DIAG_BODY_EVERY_S = _f("DIAG_BODY_EVERY_S", 300)       # newest ping body: diagnostics + temperature sample

# Tokens are stored only as sha256 hex hashes
POWER_KEY_SHA256 = os.environ.get("POWER_KEY_SHA256", "").lower()
HEARTBEAT_KEY_SHA256 = os.environ.get("HEARTBEAT_KEY_SHA256", "").lower()

# Home (power-guard) expectations
HOME_STALE_S = _f("HOME_STALE_S", 120)
HOME_WAKE_INTERVAL_S = _f("HOME_WAKE_INTERVAL_S", 300)

# Official DTEK schedules (via github.com/Baskerville42/outage-data-ua) for the comparison on the dashboard.
# source -> "<region file>:<group>"; empty = no comparison for that source.
SCHEDULE_BASE_URL = os.environ.get("SCHEDULE_BASE_URL",
                                   "https://raw.githubusercontent.com/Baskerville42/outage-data-ua/main/data")
SCHEDULE_HOME = os.environ.get("SCHEDULE_HOME", "kyiv:GPV1.1")            # просп. Червоної Калини 54/9
SCHEDULE_VILLAGE = os.environ.get("SCHEDULE_VILLAGE", "kyiv-region:GPV4.2")  # село, черга 4.2
SCHEDULE_EVERY_S = _f("SCHEDULE_EVERY_S", 900)


def schedule_targets():
    out = {}
    for source, spec in (("home", SCHEDULE_HOME), ("village", SCHEDULE_VILLAGE)):
        if spec and ":" in spec:
            region, group = spec.split(":", 1)
            out[source] = (region.strip(), group.strip())
    return out


# Outdoor temperature for the village chart (Open-Meteo); empty OUTDOOR_LAT = off
OUTDOOR_LAT = os.environ.get("OUTDOOR_LAT", "50.6331")   # смт Бабинці
OUTDOOR_LON = os.environ.get("OUTDOOR_LON", "30.0239")
OUTDOOR_EVERY_S = _f("OUTDOOR_EVERY_S", 1800)

RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", 30))


def hc_configured():
    return bool(HC_API_KEY and HC_CHECK_UUID)
