import unittest

from _paths import *  # noqa: F401,F403
from app.healthchecks import HealthchecksRelay, needs_body, new_success_pings, parse_body


def ping(n, minute, type_="success"):
    return {"type": type_, "date": f"2026-10-21T10:{minute:02d}:00.000000+00:00", "n": n,
            "remote_addr": "192.0.2.1", "body_url": None}


class PingListTest(unittest.TestCase):
    def test_newest_first_is_sorted_and_cursor_moves(self):
        raw = [ping(12, 12), ping(11, 11), ping(10, 10)]
        pings, cur = new_success_pings(raw, 10)
        self.assertEqual([p.seq for p in pings], [11, 12])
        self.assertEqual(cur, 12)
        self.assertTrue(all(p.contiguous for p in pings))

    def test_gap_in_n_means_not_contiguous(self):
        raw = [ping(250, 50), ping(249, 49)]  # API keeps only the last 100; 11..248 are gone
        pings, _ = new_success_pings(raw, 10)
        self.assertFalse(pings[0].contiguous)
        self.assertTrue(pings[1].contiguous)

    def test_non_success_pings_keep_contiguity(self):
        raw = [ping(13, 13), ping(12, 12, "start"), ping(11, 11)]
        pings, cur = new_success_pings(raw, 10)
        self.assertEqual([p.seq for p in pings], [11, 13])
        self.assertTrue(pings[1].contiguous)
        self.assertEqual(cur, 13)

    def test_first_poll_is_not_contiguous(self):
        pings, _ = new_success_pings([ping(1, 1)], None)
        self.assertFalse(pings[0].contiguous)

    def test_body_only_after_gap(self):
        raw = [ping(3, 30), ping(2, 2), ping(1, 1)]
        pings, _ = new_success_pings(raw, 0)
        self.assertEqual([p.seq for p in needs_body(pings, None, 180)], [3])

    def test_parse_body(self):
        self.assertEqual(parse_body('{"uptime_s": 12}'), {"uptime_s": 12})
        self.assertEqual(parse_body("uptime_s=12&rssi=-70"), {"uptime_s": "12", "rssi": "-70"})
        self.assertEqual(parse_body(""), {})


class FakeResp:
    def __init__(self, data=None, text="", status=200):
        self._data, self.text, self.status_code = data, text, status

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        assert headers == {"X-Api-Key": "k"}
        for suffix, resp in self.routes.items():
            if url.endswith(suffix):
                return resp
        return FakeResp(status=404)


class PollTest(unittest.TestCase):
    def test_nothing_new_is_one_request(self):
        http = FakeHttp({"/checks/u": FakeResp({"n_pings": 5})})
        pings, cur, complete = HealthchecksRelay(http, "https://hc", "k", "u").poll(5, 0, 180)
        self.assertEqual((pings, cur, complete, len(http.calls)), ([], 5, True, 1))

    def test_uptime_read_from_body_after_gap(self):
        http = FakeHttp({
            "/checks/u": FakeResp({"n_pings": 2}),
            "/checks/u/pings/": FakeResp({"pings": [ping(2, 40)]}),
            "/pings/2/body": FakeResp(text='{"uptime_s": 95, "rssi": -71}'),
        })
        last_ts = new_success_pings([ping(1, 1)], 0)[0][0].ts
        pings, cur, complete = HealthchecksRelay(http, "https://hc", "k", "u").poll(1, last_ts, 180)
        self.assertEqual(pings[0].uptime_s, 95)
        self.assertEqual(pings[0].detail["rssi"], -71)
        self.assertTrue(complete)


if __name__ == "__main__":
    unittest.main()
