import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from _paths import *  # noqa: F401,F403
from app import schedule
from app.stats import daily, outage_summary, schedule_compare, segments, worst_day

TZ = "Europe/Kyiv"
H = 3600


def ts(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=ZoneInfo(TZ)).timestamp()


class SegmentsTest(unittest.TestCase):
    def test_light_outage_light(self):
        s = segments([{"kind": "outage", "start": 10, "end": 20}], 0, 0, 30)
        self.assertEqual(s, [("light", 0, 10), ("outage", 10, 20), ("light", 20, 30)])

    def test_before_first_data_is_unknown(self):
        self.assertEqual(segments([], 15, 0, 30), [("unknown", 0, 15), ("light", 15, 30)])
        self.assertEqual(segments([], None, 0, 30), [("unknown", 0, 30)])

    def test_unknown_wins_over_outage_and_open_period(self):
        s = segments([{"kind": "outage", "start": 5, "end": None}, {"kind": "unknown", "start": 10, "end": 15}], 0, 0, 20)
        self.assertEqual(s, [("light", 0, 5), ("outage", 5, 10), ("unknown", 10, 15), ("outage", 15, 20)])


class DailyTest(unittest.TestCase):
    def test_outage_across_midnight_split_by_day_counted_once(self):
        periods = [{"kind": "outage", "start": ts(20, 22), "end": ts(21, 2)}]
        r = daily(periods, ts(20, 0), ts(21, 12), 2, TZ)
        d20, d21 = r["days"]
        self.assertEqual((d20["date"], d20["outage_s"], d20["light_s"]), ("2026-10-20", 2 * H, 22 * H))
        self.assertEqual((d21["outage_s"], d21["light_s"]), (2 * H, 10 * H))  # day still running: up to 12:00
        s = r["summary"]
        self.assertEqual(s["outages"], 1)
        self.assertEqual(s["longest_outage_s"], 4 * H)
        self.assertEqual(s["longest_light_s"], 22 * H)
        self.assertAlmostEqual(s["light_share"], 32 / 36)

    def test_nolink_counts_as_light(self):
        periods = [{"kind": "nolink", "start": ts(20, 10), "end": ts(20, 11)}]
        r = daily(periods, ts(20, 0), ts(21, 0), 1, TZ, end_day=datetime(2026, 10, 20).date())
        self.assertEqual(r["days"][0]["light_s"], 24 * H)
        self.assertEqual(r["summary"]["longest_light_s"], 24 * H)


    def test_day_when_dst_ends_has_25_hours(self):
        # 25.10.2026, 04:00 -> 03:00: the dashboard bars are in real seconds, so the day must be 25 h long
        # and a planned 14:00-18:00 outage must still be 4 h at 14:00 wall time
        tz = ZoneInfo("Europe/Kyiv")
        now = datetime(2026, 10, 25, 23, 0, tzinfo=tz).timestamp()
        hours = ["no" if 14 <= h < 18 else "yes" for h in range(24)]
        plan = {"2026-10-25": schedule.intervals("2026-10-25", hours, "Europe/Kyiv")}
        d = daily([], datetime(2026, 10, 20, tzinfo=tz).timestamp(), now, 1, "Europe/Kyiv", planned=plan)["days"][0]
        self.assertEqual(d["to"] - d["from"], 25 * H)
        self.assertEqual(d["planned_off_s"], 4 * H)
        off = next(x for x in d["planned"] if x["kind"] == "off")
        self.assertEqual(datetime.fromtimestamp(off["start"], tz).hour, 14)
        self.assertEqual(d["light_s"], 24 * H)  # up to 23:00 wall time = 24 real hours


class LongestRunTest(unittest.TestCase):
    """The longest outage is one unbroken run; the worst day adds up several outages of one local day."""

    def setUp(self):
        self.lo, self.hi = ts(10, 0), ts(13, 0)
        periods = [{"kind": "outage", "start": ts(10, 22), "end": ts(11, 16)},      # 18 h, over midnight
                   {"kind": "outage", "start": ts(11, 17), "end": ts(11, 18, 5)},   # after a 1 h break
                   {"kind": "outage", "start": ts(11, 20), "end": ts(12, 0)}]
        self.segs = segments(periods, 0, self.lo, self.hi)

    def test_longest_is_one_unbroken_run_with_its_dates(self):
        s = outage_summary(self.segs)
        self.assertEqual(s["longest_s"], 18 * H)
        self.assertEqual((s["longest_start"], s["longest_end"]), (ts(10, 22), ts(11, 16)))

    def test_worst_day_adds_up_the_day(self):
        w = worst_day(self.segs, self.lo, self.hi, TZ)
        # Oct 10: 2 h (22-24), Oct 11: 16 + 1h05 + 4 h = 21 h 05 min - more than the longest single outage
        self.assertEqual(w, {"date": "2026-10-11", "outage_s": 16 * H + H + 5 * 60 + 4 * H})

    def test_no_outage_means_no_run_and_no_worst_day(self):
        segs = segments([], 0, self.lo, self.hi)
        s = outage_summary(segs)
        self.assertEqual((s["longest_s"], s["longest_start"], s["longest_end"]), (0, None, None))
        self.assertIsNone(worst_day(segs, self.lo, self.hi, TZ))

    def test_worst_day_is_cut_at_the_range_borders(self):
        w = worst_day(self.segs, ts(11, 12), ts(11, 14), TZ)  # only 12:00-14:00 of Oct 11 is inside
        self.assertEqual(w["outage_s"], 2 * H)


class MonthScheduleTest(unittest.TestCase):
    """Real light against the DTEK schedule: only days that have a saved schedule take part."""

    def setUp(self):
        self.lo, self.hi = ts(10, 0), ts(12, 0)
        periods = [{"kind": "outage", "start": ts(10, 10, 30), "end": ts(10, 12, 30)},   # day with a schedule
                   {"kind": "outage", "start": ts(11, 8), "end": ts(11, 9)}]             # day without one
        self.segs = segments(periods, 0, self.lo, self.hi)
        hours = ["no" if 10 <= h < 12 else "yes" for h in range(24)]
        self.planned = {"2026-10-10": schedule.intervals("2026-10-10", hours, TZ)}

    def test_summary_has_light_and_share(self):
        s = outage_summary(self.segs)
        self.assertEqual((s["outages"], s["outage_s"], s["light_s"]), (2, 3 * H, 45 * H))
        self.assertAlmostEqual(s["light_share"], 45 / 48)
        self.assertIsNone(outage_summary([])["light_share"])

    def test_only_scheduled_days_are_compared(self):
        c = schedule_compare(self.segs, self.planned, self.lo, self.hi, self.hi, TZ)
        self.assertEqual((c["days"], c["days_elapsed"]), (1, 2))
        self.assertEqual((c["actual_off_s"], c["actual_light_s"]), (2 * H, 22 * H))  # Oct 11 is left out
        self.assertEqual((c["plan_off_s"], c["plan_light_s"], c["plan_maybe_s"]), (2 * H, 22 * H, 0))
        self.assertAlmostEqual(c["match_share"], 23 / 24)  # 30 min early on both ends
        self.assertEqual((c["outages"], c["on_schedule_30"]), (1, 1))

    def test_no_schedule_means_none(self):
        self.assertIsNone(schedule_compare(self.segs, {}, self.lo, self.hi, self.hi, TZ))
        self.assertIsNone(schedule_compare(self.segs, self.planned, self.hi, self.lo, self.lo, TZ))

    def test_today_is_compared_only_up_to_now(self):
        now = ts(10, 11)  # the planned 10:00-12:00 outage is half over
        segs = segments([{"kind": "outage", "start": ts(10, 10), "end": None}], 0, self.lo, now)
        c = schedule_compare(segs, self.planned, self.lo, self.hi, now, TZ)
        self.assertEqual((c["actual_off_s"], c["plan_off_s"], c["actual_light_s"]), (H, H, 10 * H))


if __name__ == "__main__":
    unittest.main()
