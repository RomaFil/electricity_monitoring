import unittest
from datetime import datetime
from zoneinfo import ZoneInfo

from _paths import *  # noqa: F401,F403
from app.stats import daily, segments

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


if __name__ == "__main__":
    unittest.main()
