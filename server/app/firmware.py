"""Firmware releases on GitHub for the dashboard's "update the board" button (outgoing request only).

The board downloads releases/download/v<version>/firmware.bin.signed itself (firmware ota.cpp) and
checks its RSA signature, so here we only offer versions that really have a signed image.
"""
import re
import time

ASSET = "firmware.bin.signed"
VERSION_RE = re.compile(r"^\d+(\.\d+){1,3}$")  # same rule as otaVersionOk() in the firmware
CACHE_S = 600  # GitHub allows 60 unauthenticated requests an hour

_cache = {"at": 0.0, "ttl": 0.0, "data": None}


def version_key(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def signed_versions(releases: list) -> list:
    """Published (not draft / pre-release) versions with a signed image, newest first."""
    out = []
    for r in releases or []:
        if not isinstance(r, dict) or r.get("draft") or r.get("prerelease"):
            continue
        tag = str(r.get("tag_name") or "")
        ver = tag[1:] if tag.startswith("v") else tag
        if not VERSION_RE.match(ver):
            continue
        if any(isinstance(a, dict) and a.get("name") == ASSET for a in r.get("assets") or []):
            out.append({"version": ver, "url": r.get("html_url"), "published": r.get("published_at")})
    return sorted(out, key=lambda x: version_key(x["version"]), reverse=True)


def releases(http, repo: str, now: float | None = None) -> dict:
    """{"versions": [...], "error": None | text}, cached for CACHE_S seconds."""
    now = now or time.time()
    if _cache["data"] is not None and now - _cache["at"] < _cache["ttl"]:
        return _cache["data"]
    try:
        r = http.get(f"https://api.github.com/repos/{repo}/releases", params={"per_page": 20},
                     headers={"Accept": "application/vnd.github+json"}, timeout=15)
        r.raise_for_status()
        data = {"versions": signed_versions(r.json()), "error": None}
    except Exception as exc:  # network, rate limit, bad json: the button just is not offered
        data = {"versions": [], "error": str(exc)[:200]}
    _cache.update(at=now, ttl=CACHE_S if data["error"] is None else 60, data=data)
    return data
