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


def planned_segments(intervals, lo, hi):
    """Official schedule for [lo, hi): pieces "light" / "off" / "maybe" (intervals from schedule.intervals)."""
    if hi <= lo:
        return []
    cuts = sorted({lo, hi, *(max(lo, min(hi, x[k])) for x in intervals for k in ("start", "end"))})
    out = []
    for a, z in zip(cuts, cuts[1:]):
        kind = next((x["kind"] for x in intervals if x["start"] <= a and x["end"] >= z), "light")
        if out and out[-1][0] == kind:
            out[-1] = (kind, out[-1][1], z)
        else:
            out.append((kind, a, z))
    return out


def compare(actual, planned):
    """How the real light matched the schedule, over time where both are known (no "maybe", no "unknown")."""
    same = both = 0.0
    for ka, a1, a2 in actual:
        if ka == "unknown":
            continue
        for kp, p1, p2 in planned:
            if kp == "maybe":
                continue
            ov = min(a2, p2) - max(a1, p1)
            if ov > 0:
                both += ov
                if (ka in LIGHT) == (kp == "light"):
                    same += ov
    return same, both


def on_schedule(actual, planned, tolerance):
    """Outages that started within ±tolerance seconds of a planned switch-off."""
    starts = [a for k, a, _ in planned if k == "off"]
    return sum(1 for k, a, _ in actual if k == "outage" and any(abs(a - s) <= tolerance for s in starts))


def daily(periods, first, now, days, tz_name, end_day=None, planned=None):
    """Rows for `days` local days ending with end_day (default: today), plus a summary.
    planned: {"YYYY-MM-DD": intervals} — official schedule, optional."""
    planned = planned or {}
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
        plan = planned_segments(planned[d.isoformat()], lo, hi_full) if d.isoformat() in planned else []
        rows.append({
            "planned": [{"kind": k, "start": a, "end": z} for k, a, z in plan],
            "planned_light_s": _sum(plan, ("light",)) if plan else None,
            "planned_off_s": _sum(plan, ("off",)) if plan else None,
            "date": d.isoformat(), "weekday": d.weekday(), "from": lo, "to": hi_full,
            "segments": [{"kind": k, "start": a, "end": z} for k, a, z in segs],
            "light_s": _sum(segs, LIGHT), "outage_s": _sum(segs, ("outage",)), "unknown_s": _sum(segs, ("unknown",)),
        })
    lo, hi = rows[0]["from"], min(rows[-1]["to"], now)
    segs = segments(periods, first, lo, hi)
    light, off = _sum(segs, LIGHT), _sum(segs, ("outage",))
    # schedule pieces up to "now" only, so "мало бути" compares the same span as "було"
    plan_all = [p for r in rows for p in planned_segments(planned.get(r["date"], []), r["from"], min(r["to"], now))
                if r["date"] in planned]
    same, both = compare(segs, plan_all)
    outages = [s for s in segs if s[0] == "outage"]
    known = light + off
    summary = {
        "light_s": light, "outage_s": off, "unknown_s": _sum(segs, ("unknown",)),
        "outages": sum(1 for k, _, _ in segs if k == "outage"),
        "longest_light_s": _longest(segs, LIGHT), "longest_outage_s": _longest(segs, ("outage",)),
        "light_share": light / known if known else None,
        "avg_light_per_day_s": light / known * 86400 if known else None,
        "schedule": {
            "days": sum(1 for r in rows if r["planned"]),
            "light_s": _sum(plan_all, ("light",)),
            "off_s": _sum(plan_all, ("off",)),
            "match_share": same / both if both else None,
            "on_schedule_15": on_schedule(outages, plan_all, 900),
            "on_schedule_30": on_schedule(outages, plan_all, 1800),
            "outages": len(outages),
        } if plan_all else None,
    }
    return {"days": rows, "summary": summary}
