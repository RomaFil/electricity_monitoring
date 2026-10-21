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
        c.commit()
        c.close()
        r = self.c.get("/api/sensors").json()
        self.assertEqual(len(r["rows"]), 1)
        self.assertEqual((r["last"]["t"], r["last"]["h"]), (21.5, 40))

    def test_heartbeat_off_in_relay_mode(self):
        self.assertEqual(self.c.post("/api/heartbeat", json={}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
