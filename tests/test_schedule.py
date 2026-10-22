import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from _paths import *  # noqa: F401,F403
from app.schedule import group_days, intervals
from app.stats import compare, daily, on_schedule, planned_segments

TZ = "Europe/Kyiv"
H = 3600


def ts(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(TZ)).timestamp()


def hours(off=(), first=(), second=(), maybe=()):
    """24 values; arguments are 0-based hour numbers."""
    out = []
    for h in range(24):
        out.append("no" if h in off else "first" if h in first else "second" if h in second
                   else "maybe" if h in maybe else "yes")
    return out


class ScheduleTest(unittest.TestCase):
    def test_group_days_from_fact(self):
        day = int(ts(23, 0))
        data = {"fact": {"data": {str(day): {"GPV4.2": {str(h): ("no" if h == 12 else "yes") for h in range(1, 25)}}}}}
        d = group_days(data, "GPV4.2", TZ)
        self.assertEqual(list(d), ["2026-10-23"])
        self.assertEqual(d["2026-10-23"][11], "no")  # slot "12" = 11:00-12:00
        self.assertEqual(group_days(data, "GPV1.1", TZ), {})

    def test_intervals_merge_halves(self):
        iv = intervals("2026-10-23", hours(off=(11, 12), second=(10,), first=(13,)), TZ)
        self.assertEqual(iv, [{"kind": "off", "start": ts(23, 10, 30), "end": ts(23, 13, 30)}])

    def test_maybe_is_separate(self):
        iv = intervals("2026-10-23", hours(maybe=(5,)), TZ)
        self.assertEqual(iv[0]["kind"], "maybe")


class CompareTest(unittest.TestCase):
    def test_match_share_and_on_schedule(self):
        plan = planned_segments([{"kind": "off", "start": ts(23, 10), "end": ts(23, 14)}], ts(23, 0), ts(23, 20))
        # real outage 10:10-14:00 (10 min late), the rest light
        actual = [("light", ts(23, 0), ts(23, 10, 10)), ("outage", ts(23, 10, 10), ts(23, 14)),
                  ("light", ts(23, 14), ts(23, 20))]
        same, both = compare(actual, plan)
        self.assertEqual(both, 20 * H)
        self.assertEqual(same, 20 * H - 600)
        self.assertEqual(on_schedule(actual, plan, 900), 1)
        self.assertEqual(on_schedule(actual, plan, 300), 0)

    def test_daily_with_schedule(self):
        planned = {"2026-10-23": intervals("2026-10-23", hours(off=(10, 11, 12, 13)), TZ)}
        periods = [{"kind": "outage", "start": ts(23, 10), "end": ts(23, 14)}]
        r = daily(periods, ts(23, 0), ts(24, 0), 1, TZ, end_day=datetime(2026, 10, 23).date(), planned=planned)
        day = r["days"][0]
        self.assertEqual((day["planned_off_s"], day["planned_light_s"]), (4 * H, 20 * H))
        s = r["summary"]["schedule"]
        self.assertEqual(s["match_share"], 1.0)
        self.assertEqual(s["on_schedule_15"], 1)

    def test_no_schedule(self):
        r = daily([], ts(23, 0), ts(24, 0), 1, TZ, end_day=datetime(2026, 10, 23).date())
        self.assertIsNone(r["summary"]["schedule"])
        self.assertEqual(r["days"][0]["planned"], [])


if __name__ == "__main__":
    unittest.main()
