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

    def send(self, payload, timeout=None):
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
        self.poweroff_rc = 0

    def __call__(self, args):
        self.calls.append(args)
        if args[0] == "rtcwake":
            if self.rtcwake_rc:
                return self.rtcwake_rc
            self.clock.t += int(args[-1])
            if self.during_sleep:
                self.during_sleep()
        if args[-1] == "poweroff":
            return self.poweroff_rc
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
        self.link_at = 0  # the network card has its link from this moment on
        self.renewed = []  # (WireGuard interface, time) of each peer renewal
        self.cfg = dict(DEFAULTS, STATE_DIR=str(root / "state"), AC_PATH=str(self.sys.ac_dir),
                        BAT_PATH=str(self.sys.bat_dir))
        self.g = self.make()

    def tearDown(self):
        self.tmp.cleanup()

    def make(self, **over):
        cfg = dict(self.cfg, **over)
        return Guard(cfg, Sysfs(cfg["AC_PATH"], cfg["BAT_PATH"]), Store(cfg["STATE_DIR"]), self.server,
                     run=self.run, clock=self.clock, sleep=self.clock.sleep, link=lambda: self.clock.t >= self.link_at,
                     renew_wg=lambda iface: self.renewed.append((iface, self.clock.t)))

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

    def test_sleeps_right_after_charger_is_gone_by_default(self):
        self.g.start()
        self.loop(30)
        self.sys.set(ac=False)
        self.loop(1)  # "ac_lost" and the first sleep come in the same second
        self.assertEqual(self.server.types()[1:3], ["ac_lost", "sleep_start"])
        self.assertEqual(len(self.rtcwake_calls()), 1)

    def test_ac_lost_reported_immediately_and_no_sleep_before_10_min(self):
        self.g = self.make(SLEEP_AFTER_MIN="10")
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

    def test_stays_awake_after_wake_before_next_sleep(self):
        """The laptop reset itself when put back to sleep right after waking: keep MIN_AWAKE_S."""
        starts = []
        orig = self.run.__call__
        self.g.run = lambda args: (starts.append(self.clock.t) if args[0] == "rtcwake" else None, orig(args))[1]
        self.g.start()
        self.sys.set(ac=False)
        self.g.step()
        self.loop(40 * 60)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        self.assertGreaterEqual(len(gaps), 3)
        self.assertTrue(all(g >= 300 + 4 for g in gaps), gaps)

    def test_low_battery_sleeps_early(self):
        self.g.start()
        self.sys.set(ac=False, battery=25)
        self.loop(60)  # 20 s settle after start, then sleep at once (not after 10 min)
        self.assertGreaterEqual(len(self.rtcwake_calls()), 1)

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

    def test_failed_poweroff_does_not_get_stuck(self):
        self.run.poweroff_rc = 1
        self.g.start()
        self.sys.set(ac=False, battery=7)
        self.g.step()
        self.g.step()  # poweroff fails
        self.assertEqual(self.g.state, ON_BATTERY)
        self.g.step()  # no second attempt right away
        self.assertEqual(self.server.types().count("forced_poweroff"), 1)
        self.sys.set(ac=True)
        self.g.step()
        self.g.step()
        self.assertEqual(self.g.state, NORMAL)
        self.assertIn("ac_restored", self.server.types())

    def test_outbox_is_bounded_and_sent_in_portions(self):
        import power_guard
        self.server.up = False
        self.g.start()
        for _ in range(power_guard.OUTBOX_MAX + 20):
            self.g.emit("guard_start")
        store = Store(self.cfg["STATE_DIR"])
        self.assertEqual(len(store.outbox()), power_guard.OUTBOX_MAX)
        self.server.up = True
        self.g.flush()
        self.assertEqual(len(self.server.events), power_guard.SEND_MAX)
        self.assertEqual(len(store.outbox()), power_guard.OUTBOX_MAX - power_guard.SEND_MAX)

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

    def wake_gaps(self, minutes=40):
        """Seconds between the starts of consecutive sleeps while the charger is gone."""
        starts = []
        orig = self.run.__call__
        self.g.run = lambda args: (starts.append(self.clock.t) if args[0] == "rtcwake" else None, orig(args))[1]
        self.g.start()
        self.sys.set(ac=False)
        self.g.step()
        self.loop(minutes * 60)
        return [b - a for a, b in zip(starts, starts[1:])]

    def test_short_wake_when_the_network_is_fine(self):
        """Awake only MIN_AWAKE_S (4 s), not 8+ s: the report goes out as soon as the link is there."""
        gaps = self.wake_gaps()
        self.assertGreaterEqual(len(gaps), 3)
        self.assertTrue(all(300 + 4 <= g < 300 + 5 for g in gaps), gaps)
        starts = [e for e in self.server.events if e["type"] == "sleep_start"]
        self.assertEqual(starts[1]["detail"]["awake_s"], 4)

    def test_waits_for_the_link_before_the_first_send(self):
        """A send before the link spoils the WireGuard handshake for 5 s, so the first try waits for the link."""
        sent = []
        orig = self.server.send
        self.server.send = lambda p, timeout=None: (sent.append(self.clock.t), orig(p, timeout))[1]
        woke = []

        def hook():  # the link comes back 2.5 s after waking
            woke.append(self.clock.t)
            self.link_at = self.clock.t + 2.5
        self.run.during_sleep = hook
        self.g.start()
        self.sys.set(ac=False)
        self.g.step()
        self.loop(10)  # 4 s settle after the start, then the first sleep
        first = [t for t in sent if t >= woke[0]][0]
        self.assertGreaterEqual(first, woke[0] + 2.5)
        self.assertLess(first, woke[0] + 3)

    def test_wireguard_peers_renewed_after_the_link_and_before_the_send(self):
        sent = []
        orig = self.server.send
        self.server.send = lambda p, timeout=None: (sent.append(self.clock.t), orig(p, timeout))[1]
        woke = []

        def hook():
            woke.append(self.clock.t)
            self.link_at = self.clock.t + 2.5
        self.run.during_sleep = hook
        self.g = self.make(WG_IFACE="wg-svitlo")
        self.g.start()
        self.sys.set(ac=False)
        self.g.step()
        self.loop(10)
        iface, t = self.renewed[0]
        self.assertEqual(iface, "wg-svitlo")
        self.assertGreaterEqual(t, woke[0] + 2.5)
        self.assertLessEqual(t, [x for x in sent if x >= woke[0]][0])

    def test_wireguard_untouched_without_wg_iface(self):
        self.wake_gaps(15)
        self.assertEqual(self.renewed, [])

    def test_disk_detached_before_each_sleep_and_attached_after_the_charger_is_back(self):
        self.g = self.make(DISK_DETACH_CMD="/x/detach.sh", DISK_ATTACH_CMD="/x/attach.sh")
        self.g.start()
        self.g.step()  # NORMAL: attach at the start (a crash may have left the disk detached)
        self.assertEqual(self.run.calls.count(["/x/attach.sh"]), 1)
        self.sys.set(ac=False)
        self.g.step()
        self.loop(11 * 60)
        calls = [c[0] for c in self.run.calls]
        self.assertGreaterEqual(calls.count("/x/detach.sh"), 2)
        for i, c in enumerate(calls):
            if c == "rtcwake":
                self.assertEqual(calls[i - 1], "/x/detach.sh")  # the call right before rtcwake
        self.assertEqual(calls.count("/x/attach.sh"), 1)  # not while on battery
        self.sys.set(ac=True)
        self.loop(6 * 60)
        self.assertEqual([c[0] for c in self.run.calls].count("/x/attach.sh"), 2)
        self.assertEqual(self.g.state, NORMAL)

    def test_no_disk_commands_when_not_configured(self):
        self.wake_gaps(15)
        self.assertFalse([c for c in self.run.calls if "detach" in c[0] or "attach" in c[0]])

    def test_no_network_wake_is_bounded_by_net_wait(self):
        self.server.up = False
        gaps = self.wake_gaps()
        self.assertGreaterEqual(len(gaps), 3)
        self.assertTrue(all(g <= 300 + 8 + 1 for g in gaps), gaps)


if __name__ == "__main__":
    unittest.main()
