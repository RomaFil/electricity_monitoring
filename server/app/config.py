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

RATE_LIMIT_PER_MIN = int(os.environ.get("RATE_LIMIT_PER_MIN", 30))


def hc_configured():
    return bool(HC_API_KEY and HC_CHECK_UUID)
