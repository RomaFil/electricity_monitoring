"""API smoke test with a temporary database. Needs fastapi + httpx (skipped otherwise)."""
import hashlib
import importlib
import os
import tempfile
import unittest

from _paths import *  # noqa: F401,F403

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    TestClient = None

TOKEN = "test-token"


@unittest.skipIf(TestClient is None, "fastapi not installed")
class ApiTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ.update(DB_PATH=os.path.join(self.tmp.name, "t.db"), VILLAGE_SOURCE="healthchecks",
                          POWER_KEY_SHA256=hashlib.sha256(TOKEN.encode()).hexdigest())
        from app import config, main
        importlib.reload(config)
        self.main = importlib.reload(main)
        self.c = TestClient(self.main.app)

    def tearDown(self):
        self.tmp.cleanup()

    def power(self, ac, events=(), ts=None, key=TOKEN):
        import time
        ts = ts or time.time()
        return self.c.post("/api/power", headers={"X-Device-Key": key},
                           json={"sample": {"ts": ts, "ac": ac, "battery": 80, "status": "x", "state": "s"},
                                 "events": list(events)})

    def test_bad_key_rejected(self):
        self.assertEqual(self.power(True, key="nope").status_code, 401)

    def test_ac_lost_shows_in_status_and_events(self):
        import time
        now = time.time()
        r = self.power(False, [{"id": "e1", "ts": now, "type": "ac_lost", "battery": 80}])
        self.assertEqual(r.json()["acked"], ["e1"])
        # resending the same event is acknowledged but not duplicated
        self.power(False, [{"id": "e1", "ts": now, "type": "ac_lost", "battery": 80}])
        st = self.c.get("/api/status").json()
        self.assertEqual(st["home"]["status"], "on_battery")
        ev = self.c.get("/api/events").json()
        self.assertEqual([e["type"] for e in ev], ["power_lost"])
        periods = self.c.get("/api/periods").json()["periods"]
        self.assertEqual(periods[0]["source"], "home")
        self.assertEqual(len(self.c.get("/api/battery").json()), 2)

    def test_battery_thinning_keeps_every_outage_sample(self):
        """A sleeping laptop reports exactly every step-th row when densely sampled (30 min == 60 records
        of 30 s), so the plain [::step] slice could drop the whole outage window; every ac=0 row is kept."""
        import time
        from app import db
        con = db.connect()
        con.isolation_level = None
        base = int(time.time()) // 30 * 30
        for i in range(0, 24 * 120 + 1):  # 2881 dense on-charger samples, 30 s apart -> step 4
            con.execute("INSERT OR REPLACE INTO power_samples(ts,ac,battery) VALUES(?,1,80.0)",
                        (base - 86400 + i * 30,))
        outage = [base - 43200 + 60 + k * 1800 for k in range(16)]  # an unlucky slice residue
        for ts in outage:
            con.execute("INSERT OR REPLACE INTO power_samples(ts,ac,battery) VALUES(?,0,45.0)", (ts,))
        con.commit()
        con.close()
        out = self.c.get("/api/battery?hours=24").json()
        self.assertEqual([r["ts"] for r in out if r["ac"] == 0], outage)
        self.assertLessEqual(len(out), 760)  # still thinned to ~600 points for the chart

    def test_bad_power_body_is_400_and_unknown_events_are_acked(self):
        h = {"X-Device-Key": TOKEN}
        self.assertEqual(self.c.post("/api/power", headers=h, json=[1, 2]).status_code, 400)
        self.assertEqual(self.c.post("/api/power", headers=h, json={"sample": {"ts": "abc"}}).status_code, 400)
        r = self.c.post("/api/power", headers=h, json={"events": [{"id": "x1", "ts": 1, "type": "new_kind"},
                                                                   {"id": "x2", "ts": "bad", "type": "ac_lost"}]})
        self.assertEqual(r.json()["acked"], ["x1", "x2"])
        self.assertEqual(self.c.get("/api/events?technical=true").json(), [])

    def test_sleeping_since_is_the_outage_start(self):
        import time
        now = time.time()
        self.power(False, [{"id": "a", "ts": now - 2400, "type": "ac_lost"},
                           {"id": "b", "ts": now - 120, "type": "sleep_start"}], ts=now - 125)
        h = self.c.get("/api/status").json()["home"]
        self.assertEqual(h["status"], "sleeping")
        self.assertAlmostEqual(h["since"], now - 2400, delta=1)

    def test_dashboard_served(self):
        r = self.c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Світло", r.text)

    def test_sensor_samples(self):
        import time
        from app import db
        c = db.connect()
        db.add_sample(c, time.time() - 60, {"t": "21.5", "h": 40, "p": 1010.2, "uptime_s": 5})
        db.add_sample(c, time.time(), {"uptime_s": 6})  # no sensor in this body -> ignored
        c.execute("INSERT INTO outdoor_samples(ts,t) VALUES(?,?)", (time.time() - 1800, 7.4))
        c.commit()
        c.close()
        r = self.c.get("/api/sensors").json()
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual((r["last"]["t"], r["last"]["h"]), (21.5, 40))
        self.assertEqual(r["outdoor_last"]["t"], 7.4)
        self.assertEqual(len(r["outdoor"]), 1)

    def test_heartbeat_off_in_relay_mode(self):
        self.assertEqual(self.c.post("/api/heartbeat", json={}).status_code, 404)


    def test_esp_command_is_signed_and_guarded(self):
        from app import config, firmware, healthchecks
        h = {"Content-Type": "application/json"}
        self.assertEqual(self.c.post("/api/esp/command", json={"cmd": "reboot"}).status_code, 503)  # no key
        config.ESP_CMD_KEY, config.HC_API_KEY, config.HC_CHECK_UUID = "k" * 20, "api", "uuid"
        sent = []
        orig = healthchecks.HealthchecksRelay.set_cmd
        healthchecks.HealthchecksRelay.set_cmd = lambda self, token: sent.append(token)
        firmware._cache.update(at=1e12, ttl=1e12, data={"versions": [{"version": "0.7.1"}], "error": None})
        try:
            self.assertEqual(self.c.post("/api/esp/command", json={"cmd": "factory_reset"}).status_code, 400)
            self.assertEqual(self.c.post("/api/esp/command", content="cmd=reboot",
                                         headers={"Content-Type": "application/x-www-form-urlencoded"}).status_code, 415)
            self.assertEqual(self.c.post("/api/esp/command", json={"cmd": "reboot"},
                                         headers={"Origin": "https://evil.example"}).status_code, 403)
            self.assertEqual(self.c.post("/api/esp/command", json={"cmd": "ota", "version": "9.9.9"}).status_code, 400)
            r = self.c.post("/api/esp/command", json={"cmd": "ota", "version": "0.7.1"},
                            headers={**h, "Origin": "http://testserver"})
            self.assertEqual(r.status_code, 200, r.text)
            cmd, ts, mac = sent[-1].split("#")
            self.assertEqual(cmd, "ota:0.7.1")
            self.assertEqual(sent[-1], healthchecks.sign_cmd("k" * 20, cmd, int(ts)))
            self.assertEqual(self.c.get("/api/status").json()["village"]["esp_cmd_sent"]["cmd"], "ota:0.7.1")
            self.assertEqual(self.c.post("/api/esp/command", json={"cmd": "golden"}).status_code, 200)
            self.assertEqual(sent[-1].split("#")[0], "golden")
        finally:
            healthchecks.HealthchecksRelay.set_cmd = orig
            firmware._cache.update(at=0.0, ttl=0.0, data=None)

    def test_home_esp_takes_over_the_home_light_and_the_laptop_becomes_a_server(self):
        import time
        from app import config, db, healthchecks
        config.HC_HOME_API_KEY, config.HC_HOME_CHECK_UUID = "api2", "uuid2"
        now = time.time()
        # the laptop's charger events no longer count as outages of the home light
        self.power(False, [{"id": "e1", "ts": now, "type": "ac_lost", "battery": 80}])
        self.assertEqual([e["type"] for e in self.c.get("/api/events?technical=true").json() if e["type"].startswith("laptop")],
                         ["laptop_ac_lost"])
        con = db.connect()
        con.isolation_level = None
        db.set_meta(con, "home_state", {"status": "offline", "since": now - 600, "last_ping_ts": now - 600})
        db.add_event(con, {"source": "home", "type": "power_lost", "ts": now - 600, "detail": {}})
        con.close()
        st = self.c.get("/api/status").json()
        self.assertTrue(st["home_esp"])
        self.assertEqual(st["home"]["status"], "offline")      # the ESP says there is no light
        self.assertEqual(st["laptop"]["status"], "on_battery")  # the laptop is only a server with a battery
        periods = self.c.get("/api/periods").json()["periods"]
        self.assertEqual([(p["source"], p["kind"]) for p in periods], [("home", "outage")])
        # a command for the home board goes to its own check, with its own key
        sent = []
        orig = healthchecks.HealthchecksRelay.set_cmd
        healthchecks.HealthchecksRelay.set_cmd = lambda self, token: sent.append((self.base, self.headers, token))
        config.ESP_CMD_KEY = "k" * 20
        try:
            r = self.c.post("/api/esp/command", json={"cmd": "reboot", "target": "home"},
                            headers={"Origin": "http://testserver"})
            self.assertEqual(r.status_code, 200, r.text)
            self.assertTrue(sent[-1][0].endswith("/uuid2"))
            self.assertEqual(sent[-1][1], {"X-Api-Key": "api2"})
            self.assertEqual(self.c.get("/api/status").json()["home"]["esp_cmd_sent"]["cmd"], "reboot")
            self.assertEqual(self.c.post("/api/esp/command", json={"cmd": "reboot", "target": "moon"}).status_code, 400)
        finally:
            healthchecks.HealthchecksRelay.set_cmd = orig


if __name__ == "__main__":
    unittest.main()
