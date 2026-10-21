import unittest

from _paths import *  # noqa: F401,F403  (adds server/ and power-guard/ to sys.path)
from app.village import NEVER, OFFLINE, ONLINE, UNKNOWN, Ping, State, on_ping, on_tick

TH = 180  # 3 missed pings


def feed(st, *pings):
    events = []
    for p in pings:
        st, ev = on_ping(st, p, TH)
        events += ev
    return st, events


class VillageTest(unittest.TestCase):
    def test_first_ping(self):
        st, ev = on_ping(State(), Ping(1000), TH)
        self.assertEqual(st.status, ONLINE)
        self.assertEqual([e["type"] for e in ev], ["first_ping"])

    def test_regular_pings_no_events(self):
        st, _ = feed(State(), Ping(0))
        st, ev = feed(st, *(Ping(60 * i) for i in range(1, 30)))
        self.assertEqual(ev, [])
        self.assertEqual(st.since, 0)

    def test_one_lost_ping_is_not_an_outage(self):
        """Debounce: a gap of 2 minutes is below the threshold."""
        st, _ = feed(State(), Ping(0), Ping(60))
        st, ev = on_tick(st, 180, True, TH)
        self.assertEqual((st.status, ev), (ONLINE, []))
        st, ev = feed(st, Ping(180))
        self.assertEqual(ev, [])

    def test_live_outage_then_restore_with_uptime(self):
        st, _ = feed(State(), Ping(0), Ping(60))
        st, ev = on_tick(st, 60 + TH + 1, True, TH)
        self.assertEqual(st.status, OFFLINE)
        self.assertEqual(ev[0]["type"], "power_lost")
        self.assertEqual(ev[0]["ts"], 60)
        # light returns at 3600, router boots, first ping at 3700 with uptime 100
        st, ev = feed(st, Ping(3700, uptime_s=100))
        self.assertEqual(st.status, ONLINE)
        e = ev[0]
        self.assertEqual(e["type"], "power_restored")
        self.assertEqual((e["start"], e["end"]), (60, 3600))
        self.assertEqual(e["detail"]["duration"], 3540)
        self.assertTrue(e["detail"]["precise_end"])
        self.assertEqual(st.since, 3600)

    def test_restore_without_uptime_uses_ping_time(self):
        st, _ = feed(State(), Ping(0))
        st, ev = feed(st, Ping(1000))
        self.assertEqual(ev[0]["type"], "power_restored")
        self.assertEqual(ev[0]["end"], 1000)
        self.assertFalse(ev[0]["detail"]["precise_end"])

    def test_no_reboot_means_link_was_down(self):
        """ESP uptime longer than the gap: it never lost power."""
        st, _ = feed(State(), Ping(0, uptime_s=50))
        st, ev = feed(st, Ping(1000, uptime_s=1050))
        self.assertEqual(ev[0]["type"], "link_restored")

    def test_missing_history_is_unknown_not_outage(self):
        st, _ = feed(State(), Ping(0, seq=1))
        st, ev = feed(st, Ping(9000, seq=250, contiguous=False, uptime_s=10))
        self.assertEqual(ev[0]["type"], "gap_unknown")
        self.assertEqual(st.status, ONLINE)

    def test_not_observing_gives_unknown(self):
        st, _ = feed(State(), Ping(0))
        st, ev = on_tick(st, 500, False, TH)
        self.assertEqual(st.status, UNKNOWN)
        self.assertEqual(ev[0]["type"], "no_data")
        # observation comes back, still no pings -> it was an outage, detected late
        st, ev = on_tick(st, 600, True, TH)
        self.assertEqual(st.status, OFFLINE)
        self.assertTrue(ev[0]["detail"]["late"])

    def test_duplicates_ignored(self):
        st, _ = feed(State(), Ping(100, seq=5))
        st2, ev = feed(st, Ping(100, seq=5), Ping(90, seq=4))
        self.assertEqual((st2, ev), (st, []))

    def test_short_blink_is_reboot_event(self):
        st, _ = feed(State(), Ping(0, uptime_s=500))
        st, ev = feed(st, Ping(60, uptime_s=20))
        self.assertEqual([e["type"] for e in ev], ["esp_reboot"])

    def test_tick_without_pings(self):
        st, ev = on_tick(State(), 10_000, True, TH)
        self.assertEqual((st.status, ev), (NEVER, []))


if __name__ == "__main__":
    unittest.main()
