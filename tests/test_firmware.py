import unittest

from _paths import *  # noqa: F401,F403
from app import firmware


def rel(tag, assets=("firmware.bin.signed",), **kw):
    return {"tag_name": tag, "html_url": f"https://example/{tag}", "published_at": "2026-10-24T12:00:00Z",
            "assets": [{"name": a} for a in assets], **kw}


class FakeHttp:
    def __init__(self, data=None, fail=False):
        self.data, self.fail, self.calls = data, fail, 0

    def get(self, url, **kw):
        self.calls += 1
        if self.fail:
            raise OSError("no network")
        http = self

        class R:
            def raise_for_status(self):
                pass

            def json(self):
                return http.data
        return R()


class FirmwareTest(unittest.TestCase):
    def setUp(self):
        firmware._cache.update(at=0.0, ttl=0.0, data=None)

    def test_only_published_signed_versions_newest_first(self):
        got = firmware.signed_versions([
            rel("v0.6.0", assets=("firmware.bin",)),   # no signed image: the board could not install it
            rel("v0.7.0"), rel("v0.10.0"), rel("v0.9.1"),
            rel("v0.11.0", draft=True), rel("v0.12.0", prerelease=True),
            rel("latest"), rel("v1.0.0-rc1"), "junk"])
        self.assertEqual([v["version"] for v in got], ["0.10.0", "0.9.1", "0.7.0"])

    def test_cached_and_errors_do_not_raise(self):
        http = FakeHttp([rel("v0.7.0")])
        self.assertEqual(firmware.releases(http, "o/r", now=1000)["versions"][0]["version"], "0.7.0")
        firmware.releases(http, "o/r", now=1100)
        self.assertEqual(http.calls, 1)
        firmware._cache.update(at=0.0, ttl=0.0, data=None)
        bad = firmware.releases(FakeHttp(fail=True), "o/r", now=2000)
        self.assertEqual(bad["versions"], [])
        self.assertIn("no network", bad["error"])


if __name__ == "__main__":
    unittest.main()
