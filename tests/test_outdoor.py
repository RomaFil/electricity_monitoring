import unittest

from _paths import *  # noqa: F401,F403
from app.outdoor import params, parse


class OutdoorTest(unittest.TestCase):
    def test_only_past_hours_and_no_nulls(self):
        data = {"hourly": {"time": [3600, 7200, 10800, 14400], "temperature_2m": [5.1, None, 6.3, 7.0]}}
        self.assertEqual(parse(data, now=11000), [(3600.0, 5.1), (10800.0, 6.3)])

    def test_bad_answer_is_empty(self):
        self.assertEqual(parse({}, 0), [])
        self.assertEqual(parse(None, 0), [])

    def test_params(self):
        p = params(50.6, 30.0, 7)
        self.assertEqual((p["past_days"], p["hourly"], p["timeformat"]), (7, "temperature_2m", "unixtime"))


if __name__ == "__main__":
    unittest.main()
