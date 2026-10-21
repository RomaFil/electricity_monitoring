"""power-guard state machine with a fake /sys, fake clock, fake rtcwake and fake server."""
import tempfile
import unittest
from pathlib import Path

from _paths import *  # noqa: F401,F403
from power_guard import DEFAULTS, NORMAL, OFF, ON_BATTERY, Guard, Store, Sysfs


class Clock:
    def __init__(self):
        self.t = 1_000_000.0

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += s


class FakeSys:
    """Writes files like /sys/class/power_supply/{ADP1,BAT1}."""

    def __init__(self, root):
        self.ac_dir, self.bat_dir = Path(root, "ADP1"), Path(root, "BAT1")
        self.ac_dir.mkdir()
        self.bat_dir.mkdir()
        self.set(True, 90)

    def set(self, ac=None, battery=None):
        if ac is not None:
            (self.ac_dir / "online").write_text("1\n" if ac else "0\n")
            (self.bat_dir / "status").write_text("Charging\n" if ac else "Discharging\n")
        if battery is not None:
            (self.bat_dir / "capacity").write_text(f"{battery}\n")
        (self.bat_dir / "current_now").write_text("1500000\n")


class Server:
    def __init__(self):
        self.up, self.events, self.samples = True, [], []

    def send(self, payload):
        if not self.up:
            raise OSError("network down")
        self.samples.append(payload["sample"])
        ids = []
        for e in payload["events"]:
            if e["id"] not in {x["id"] for x in self.events}:
                self.events.append(e)
            ids.append(e["id"])
        return ids

    def types(self):
        return [e["type"] for e in self.events]


class Runner:
    """Fake command runner: rtcwake advances the clock and can run a hook (e.g. plug the charger in)."""

    def __init__(self, clock):
        self.clock, self.calls, self.rtcwake_rc, self.during_sleep = clock, [], 0, None

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "rtcwake":
            if self.rtcwake_rc:
                return self.rtcwake_rc
            self.clock.t += int(args[-1])
            if self.during_sleep:
                self.during_sleep()
        return 0


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "sys").mkdir()
        self.sys = FakeSys(root / "sys")
        self.clock = Clock()
        self.server = Server()
        self.run = Runner(self.clock)
        self.cfg = dict(DEFAULTS, STATE_DIR=str(root / "state"), AC_PATH=str(self.sys.ac_dir),
                        BAT_PATH=str(self.sys.bat_dir))
        self.g = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **over):
        cfg = dict(self.cfg, **over)
        return Guard(cfg, Sysfs(cfg["AC_PATH"], cfg["BAT_PATH"]), Store(cfg["STATE_DIR"]), self.server,
                     run=self.run, clock=self.clock, sleep=self.clock.sleep)

    def loop(self, seconds):
        end = self.clock.t + seconds
        while self.clock.t < end:
            wait = self.g.step()  # step() itself may move the clock (rtcwake), so not "t += step()"
            self.clock.t += wait

    def rtcwake_calls(self):
        return [c for c in self.run.calls if c[0] == "rtcwake"]

    # ---------- tests ----------

    def test_normal_reports_every_30s(self):
        self.g.start()
        self.loop(120)
        self.assertEqual(self.g.state, NORMAL)
        self.assertGreaterEqual(len(self.server.samples), 4)
        self.assertEqual(self.server.types(), ["guard_start"])

    def test_ac_lost_reported_immediately_and_no_sleep_before_10_min(self):
        self.g.start()
        self.loop(60)
        self.sys.set(ac=False)
        self.g.step()
        self.assertEqual(self.server.types()[-1], "ac_lost")
        self.loop(9 * 60)
        self.assertEqual(self.g.state, ON_BATTERY)
        self.assertEqual(self.rtcwake_calls(), [])

    def test_sleep_loop_and_restore_while_sleeping(self):
        self.g.start()
        self.sys.set(ac=False)
        self.g.step()
        lost_at = self.clock.t
        self.loop(10 * 60 + 1)  # first sleep starts here
        self.assertEqual(self.rtcwake_calls()[0], ["rtcwake", "-m", "mem", "-s", "300"])
        self.assertIn(["sync"], self.run.calls)
        # charger comes back during the 3rd sleep
        n = {"i": 0}

        def hook():
            n["i"] += 1
            if n["i"] == 2:
                self.sys.set(ac=True)
        self.run.during_sleep = hook
        self.loop(20 * 60)
        self.assertEqual(self.g.state, NORMAL)
        restored = [e for e in self.server.events if e["type"] == "ac_restored"][0]
        self.assertEqual(restored["detail"]["start"], lost_at)
        self.assertEqual(restored["detail"]["precision_s"], 300)
        ends = [e for e in self.server.events if e["type"] == "sleep_end"]
        self.assertIn("drain_pct_per_h", ends[0]["detail"])
        self.assertEqual(self.server.types().count("sleep_start"), self.server.types().count("sleep_end"))

    def test_low_battery_sleeps_early(self):
        self.g.start()
        self.sys.set(ac=False, battery=25)
        self.g.step()
        self.g.step()
        self.assertEqual(len(self.rtcwake_calls()), 1)

    def test_keep_awake_never_sleeps(self):
        self.g = self.make(KEEP_AWAKE="true")
        self.g.start()
        self.sys.set(ac=False)
        self.loop(60 * 60)
        self.assertEqual(self.rtcwake_calls(), [])

    def test_critical_battery_powers_off_and_journals(self):
        self.g.start()
        self.sys.set(ac=False, battery=7)
        self.g.step()
        self.g.step()
        self.assertEqual(self.run.calls[-1], ["systemctl", "poweroff"])
        self.assertEqual(self.g.state, OFF)
        self.assertEqual(self.server.types()[-1], "forced_poweroff")
        journal = Path(self.cfg["STATE_DIR"], "journal.jsonl").read_text()
        self.assertIn("forced_poweroff", journal)

    def test_after_poweroff_manual_start_records_gap_and_restore(self):
        self.g.start()
        self.sys.set(ac=False, battery=7)
        self.g.step()
        lost_at = self.clock.t
        self.g.step()  # poweroff
        self.clock.t += 5 * 3600  # off for 5 hours, then Roman presses the button
        self.sys.set(ac=True, battery=40)
        g2 = self.make()
        g2.start()
        g2.step()
        gap = [e for e in self.server.events if e["type"] == "unknown_gap"][-1]
        self.assertEqual(gap["detail"]["reason"], "poweroff")
        restored = [e for e in self.server.events if e["type"] == "ac_restored"][-1]
        self.assertEqual(restored["detail"]["start"], lost_at)
        self.assertGreater(restored["detail"]["precision_s"], 4 * 3600)

    def test_crash_detected_on_start(self):
        self.g.start()
        self.loop(60)
        self.clock.t += 3600  # killed without a clean stop
        g2 = self.make()
        g2.start()
        g2.step()  # first step sends the outbox
        gap = [e for e in self.server.events if e["type"] == "unknown_gap"][-1]
        self.assertEqual(gap["detail"]["reason"], "crash")

    def test_quick_restart_is_not_a_gap(self):
        self.g.start()
        self.loop(30)
        self.g.stop()
        self.clock.t += 20
        self.make().start()
        self.assertNotIn("unknown_gap", self.server.types())

    def test_rtcwake_error_backs_off(self):
        self.run.rtcwake_rc = 1
        self.g.start()
        self.sys.set(ac=False)
        self.g.step()
        self.loop(10 * 60 + 5 * 60)
        # first try at 10 min, then retry after >= 60 s, not a tight loop
        self.assertLessEqual(len(self.rtcwake_calls()), 4)
        self.assertIn("rtcwake_error", self.server.types())

    def test_events_survive_network_loss(self):
        self.g.start()
        self.server.up = False
        self.sys.set(ac=False)
        self.g.step()
        self.assertNotIn("ac_lost", self.server.types())
        self.server.up = True
        self.loop(60)
        self.assertIn("ac_lost", self.server.types())
        self.assertEqual(Store(self.cfg["STATE_DIR"]).outbox(), [])

    def test_network_wait_after_wake_is_bounded(self):
        self.g.start()
        self.sys.set(ac=False)
        self.g.step()
        self.server.up = False
        t0 = self.clock.t
        self.loop(10 * 60 + 1)
        # one sleep (300 s) + at most NET_WAIT_S of waiting, then it sleeps again
        self.assertGreaterEqual(len(self.rtcwake_calls()), 1)
        self.assertLess(self.clock.t - t0, 10 * 60 + 1 + 300 + 30 + 60)


if __name__ == "__main__":
    unittest.main()
