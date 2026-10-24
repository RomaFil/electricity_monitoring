"""Heartbeat source "healthchecks-relay".

The ESP pings https://hc-ping.com/<uuid>. We never receive anything directly:
the poller reads the Healthchecks Management API v3 (outgoing connection only).

Endpoints used (docs: https://healthchecks.io/docs/api/):
  GET /api/v3/checks/<uuid>              -> {"n_pings": int, "last_ping": iso, ...}
  GET /api/v3/checks/<uuid>/pings/       -> {"pings": [{"type", "date", "n", ...}]}, newest first,
                                            only the last 100 pings on the free plan
  GET /api/v3/checks/<uuid>/pings/<n>/body -> text/plain body, 404 if the ping had no body
Pings list and bodies need the full (not read-only) API key.
Rate limit: ~100 requests/minute; we use 1-3 per poll.
"""
import json
from datetime import datetime
from urllib.parse import parse_qsl

from .village import Ping


def parse_body(text: str) -> dict:
    """ESP sends JSON; also accept "a=1&b=2" just in case."""
    text = (text or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else {}
    except ValueError:
        return dict(parse_qsl(text))


def to_ts(iso: str) -> float:
    return datetime.fromisoformat(iso).timestamp()


def new_success_pings(raw_pings: list, cursor: int | None):
    """From the API ping list pick success pings newer than `cursor` (last processed n),
    oldest first, and mark whether each one is contiguous: every n between the
    previous one and this one is present in the list (no lost history).
    Returns (pings, new_cursor)."""
    fresh = sorted((p for p in raw_pings if cursor is None or p["n"] > cursor), key=lambda p: p["n"])
    if not fresh:
        return [], cursor
    present = {p["n"] for p in fresh}
    out = []
    prev_n = cursor
    for p in fresh:
        if p.get("type") != "success":
            continue
        if prev_n is None:
            contiguous = False  # first ever poll: history before is unknown
        else:
            contiguous = all(k in present for k in range(prev_n + 1, p["n"]))
        out.append(Ping(ts=to_ts(p["date"]), seq=p["n"], contiguous=contiguous,
                        detail={"remote_addr": p.get("remote_addr")}))
        prev_n = p["n"]
    return out, fresh[-1]["n"]


def with_mode(desc: str, mode: str, ts: int) -> str:
    """Put "svitlo-mode=<mode>#<ts>" into a check description, keeping the rest of it.
    The ESP reads this line with its read-only key (firmware remote.cpp)."""
    kept = [line for line in (desc or "").splitlines() if not line.strip().startswith("svitlo-mode=")]
    return "\n".join(kept + [f"svitlo-mode={mode}#{ts}"]).strip()


def needs_body(pings: list, last_ping_ts: float | None, threshold: float) -> list:
    """Bodies cost one request each, so fetch only where uptime matters:
    a ping that follows a gap longer than the threshold."""
    want = []
    prev = last_ping_ts
    for p in pings:
        if prev is not None and p.ts - prev > threshold:
            want.append(p)
        prev = p.ts
    return want


class HealthchecksRelay:
    def __init__(self, http, api_base: str, api_key: str, check_uuid: str):
        self.http = http  # httpx.Client
        self.base = api_base.rstrip("/") + "/api/v3/checks/" + check_uuid
        self.headers = {"X-Api-Key": api_key}

    def _get(self, path=""):
        r = self.http.get(self.base + path, headers=self.headers, timeout=15)
        r.raise_for_status()
        return r

    def check(self) -> dict:
        return self._get().json()

    def pings(self) -> list:
        return self._get("/pings/").json().get("pings", [])

    def set_mode(self, mode: str, ts: int):
        """Write the dashboard command into the check description (full API key)."""
        desc = with_mode(self.check().get("desc", ""), mode, ts)
        r = self.http.post(self.base, headers=self.headers, json={"desc": desc}, timeout=15)
        r.raise_for_status()

    def body(self, n: int) -> dict:
        r = self.http.get(f"{self.base}/pings/{n}/body", headers=self.headers, timeout=15)
        if r.status_code == 404:
            return {}
        r.raise_for_status()
        return parse_body(r.text)

    def poll(self, cursor: int | None, last_ping_ts: float | None, threshold: float):
        """One poll cycle. Returns (pings, new_cursor, complete).
        complete=True means after this poll we have seen every ping up to now."""
        info = self.check()
        n_pings = int(info.get("n_pings") or 0)
        if cursor is not None and n_pings <= cursor:
            return [], cursor, True
        pings, new_cursor = new_success_pings(self.pings(), cursor)
        for p in needs_body(pings, last_ping_ts, threshold):
            b = self.body(p.seq)
            up = b.get("uptime_s")
            try:
                p.uptime_s = float(up) if up is not None else None
            except (TypeError, ValueError):
                p.uptime_s = None
            p.detail = {**(p.detail or {}), **b}
        return pings, new_cursor, (new_cursor or 0) >= n_pings
