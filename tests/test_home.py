import unittest

from _paths import *  # noqa: F401,F403
from app.home import map_event, status


def sample(ts, ac=True, battery=90):
    return {"ts": ts, "ac": ac, "battery": battery, "status": "Charging", "state": "NORMAL"}


class HomeStatusTest(unittest.TestCase):
    def test_online_and_on_battery(self):
        self.assertEqual(status(sample(100), None, 130, 120, 300)["status"], "online")
        self.assertEqual(status(sample(100, ac=False), None, 130, 120, 300)["status"], "on_battery")

    def test_stale_report_is_no_data(self):
        self.assertEqual(status(sample(100), None, 400, 120, 300)["status"], "no_data")

    def test_sleeping_then_late(self):
        ctrl = {"type": "sleep_start", "ts": 200}
        self.assertEqual(status(sample(190, ac=False), ctrl, 400, 120, 300)["status"], "sleeping")
        late = status(sample(190, ac=False), ctrl, 700, 120, 300)
        self.assertEqual(late["status"], "no_data")
        self.assertIn("banner", late)

    def test_forced_poweroff_banner(self):
        st = status(sample(190, ac=False, battery=7), {"type": "forced_poweroff", "ts": 200}, 5000, 120, 300)
        self.assertEqual(st["status"], "off")
        self.assertIn("кнопкою", st["banner"])

    def test_sample_after_sleep_end_wins(self):
        ctrl = {"type": "sleep_start", "ts": 200}
        self.assertEqual(status(sample(600, ac=True), ctrl, 610, 120, 300)["status"], "online")

    def test_map_event(self):
        row = map_event({"id": "abc", "ts": 10, "type": "ac_restored", "battery": 50,
                         "detail": {"start": 1, "end": 10, "duration": 9}})
        self.assertEqual((row["type"], row["start"], row["end"], row["ext_id"]), ("power_restored", 1, 10, "pg:abc"))
        self.assertEqual(row["detail"]["battery"], 50)
        self.assertIsNone(map_event({"id": "x", "ts": 1, "type": "bogus"}))


if __name__ == "__main__":
    unittest.main()
