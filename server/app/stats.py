"""Daily statistics for the weekly chart. Pure logic, no I/O.

A timeline is cut into pieces of four kinds:
  light   - light was on
  outage  - no light (or no signal)
  nolink  - light was on but there was no connection (counted as light)
  unknown - no data (before the first report, server down, ...)
"""
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

PRIORITY = {"unknown": 3, "outage": 2, "nolink": 1}
LIGHT = ("light", "nolink")


def segments(periods, first, lo, hi):
    """Cut [lo, hi) into pieces. periods: [{"kind", "start", "end" or None}]; first: first data time."""
    if hi <= lo:
        return []
    marks = []
    if first is None:
        marks.append(("unknown", lo, hi))
    elif first > lo:
        marks.append(("unknown", lo, min(first, hi)))
    for p in periods:
        a, z = max(p["start"], lo), min(p["end"] if p["end"] is not None else hi, hi)
        if z > a:
            marks.append((p["kind"], a, z))
    cuts = sorted({lo, hi, *(m[1] for m in marks), *(m[2] for m in marks)})
    out = []
    for a, z in zip(cuts, cuts[1:]):
        covering = [m[0] for m in marks if m[1] <= a and m[2] >= z]
        kind = max(covering, key=PRIORITY.get) if covering else "light"
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1], z)
        else:
            out.append((kind, a, z))
    return out


def _sum(segs, kinds):
    return sum(z - a for k, a, z in segs if k in kinds)


def _longest(segs, kinds):
    """Longest run of the given kinds (neighbouring light + nolink pieces join into one run)."""
    best = run = 0.0
    for k, a, z in segs:
        run = run + (z - a) if k in kinds else 0.0
        best = max(best, run)
    return best


def daily(periods, first, now, days, tz_name, end_day=None):
    """Rows for `days` local days ending with end_day (default: today), plus a summary."""
    tz = ZoneInfo(tz_name)
    today = datetime.fromtimestamp(now, tz).date()
    end_day = end_day or today
    rows = []
    for i in range(days - 1, -1, -1):
        d = end_day - timedelta(days=i)
        lo = datetime(d.year, d.month, d.day, tzinfo=tz).timestamp()
        hi_full = datetime.combine(d + timedelta(days=1), datetime.min.time(), tz).timestamp()
        hi = min(hi_full, now)
        segs = segments(periods, first, lo, hi) if hi > lo else []
        rows.append({
            "date": d.isoformat(), "weekday": d.weekday(), "from": lo, "to": hi_full,
            "segments": [{"kind": k, "start": a, "end": z} for k, a, z in segs],
            "light_s": _sum(segs, LIGHT), "outage_s": _sum(segs, ("outage",)), "unknown_s": _sum(segs, ("unknown",)),
        })
    lo, hi = rows[0]["from"], min(rows[-1]["to"], now)
    segs = segments(periods, first, lo, hi)
    light, off = _sum(segs, LIGHT), _sum(segs, ("outage",))
    known = light + off
    summary = {
        "light_s": light, "outage_s": off, "unknown_s": _sum(segs, ("unknown",)),
        "outages": sum(1 for k, _, _ in segs if k == "outage"),
        "longest_light_s": _longest(segs, LIGHT), "longest_outage_s": _longest(segs, ("outage",)),
        "light_share": light / known if known else None,
        "avg_light_per_day_s": light / known * 86400 if known else None,
    }
    return {"days": rows, "summary": summary}
