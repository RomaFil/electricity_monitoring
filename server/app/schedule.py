"""Official outage schedules (DTEK), for comparing with what really happened.

Source: github.com/Baskerville42/outage-data-ua (MIT) — a bot copies the hourly schedules from the
DTEK websites into data/<region>.json every few minutes. We use the "fact" part: the schedule for
today and (in the evening) tomorrow, per group, e.g. "GPV4.2" = черга 4.2.

Per group and day there are 24 hour slots "1".."24" (slot 1 = 00:00-01:00) with values:
  yes      light                          no       no light
  first    no light the first 30 min      second   no light the second 30 min
  maybe / mfirst / msecond                outage possible (whole hour / first / second half)
Pure logic here; the poller does the HTTP part.
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

# value -> list of (from_minute, to_minute, kind) inside the hour
SLOT = {
    "yes": [],
    "no": [(0, 60, "off")],
    "first": [(0, 30, "off")],
    "second": [(30, 60, "off")],
    "maybe": [(0, 60, "maybe")],
    "mfirst": [(0, 30, "maybe")],
    "msecond": [(30, 60, "maybe")],
}


def group_days(region_json: dict, group: str, tz_name: str) -> dict:
    """{ "2026-10-23": ["yes", "no", ...24 values] } for the group, from the "fact" block."""
    tz = ZoneInfo(tz_name)
    out = {}
    for day_ts, groups in ((region_json.get("fact") or {}).get("data") or {}).items():
        hours = (groups or {}).get(group)
        if not hours:
            continue
        date = datetime.fromtimestamp(int(day_ts), tz).date().isoformat()
        out[date] = [hours.get(str(h), "yes") for h in range(1, 25)]
    return out


def intervals(date: str, hours: list, tz_name: str) -> list:
    """Planned periods of one day as [{"kind": "off"|"maybe", "start": ts, "end": ts}], merged."""
    tz = ZoneInfo(tz_name)
    d = datetime.fromisoformat(date).replace(tzinfo=tz)
    out = []
    for h, value in enumerate(hours):
        for a, z, kind in SLOT.get(value, []):
            # going through local wall time keeps DST days right (23 or 25 hours)
            start = (d + timedelta(hours=h, minutes=a)).timestamp()
            end = (d + timedelta(hours=h, minutes=z)).timestamp()
            if out and out[-1]["kind"] == kind and abs(out[-1]["end"] - start) < 1:
                out[-1]["end"] = end
            else:
                out.append({"kind": kind, "start": start, "end": end})
    return out
