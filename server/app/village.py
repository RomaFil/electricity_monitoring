"""Village light: state machine for a heartbeat source (ESP8266).

Pure logic without I/O, so it can be unit-tested. The poller feeds it
pings (on_ping) and clock ticks (on_tick) and stores the events it returns.
All times are unix seconds (UTC).

Idea in plain words:
  * ESP is alive only while there is light, and pings once a minute.
  * No pings for longer than `threshold` -> light is (probably) gone.
  * When pings come back, the gap between the last ping before and the first
    ping after is the outage. If the ESP reports its uptime, the real moment
    the light returned is "ping time - uptime" (the router boots 1-2 minutes
    after the light returns, so the first ping is late).
  * If the ESP did NOT reboot during the gap (uptime is longer than the gap),
    the light was on and only the internet/router was down.
  * If we could have missed pings (server down, too many pings to fetch),
    the gap is "unknown", never an outage.
"""
from dataclasses import dataclass, asdict

NEVER, ONLINE, OFFLINE, UNKNOWN = "never", "online", "offline", "unknown"
SOURCE = "village"


@dataclass
class Ping:
    ts: float
    seq: int | None = None          # Healthchecks ping number "n" (None in direct mode)
    uptime_s: float | None = None   # ESP uptime from the ping body, if known
    contiguous: bool = True         # True = no ping could have been missed since the previous one
    detail: dict | None = None


@dataclass
class State:
    status: str = NEVER
    since: float | None = None
    last_ping_ts: float | None = None
    last_seq: int | None = None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        return cls(**{k: d.get(k) for k in ("status", "since", "last_ping_ts", "last_seq")}) if d else cls()


def _event(type_, ts, start=None, end=None, **detail):
    return {"source": SOURCE, "type": type_, "ts": ts, "start": start, "end": end, "detail": detail}


def on_ping(st: State, p: Ping, threshold: float):
    """Process one received ping. Returns (new_state, events)."""
    if st.last_ping_ts is not None:
        old_ts = p.ts <= st.last_ping_ts
        old_seq = p.seq is not None and st.last_seq is not None and p.seq <= st.last_seq
        if old_ts or old_seq:
            return st, []  # duplicate or out of order

    if st.last_ping_ts is None:
        new = State(ONLINE, p.ts, p.ts, p.seq)
        return new, [_event("first_ping", p.ts)]

    events = []
    gap = p.ts - st.last_ping_ts
    rebooted = p.uptime_s is not None and 0 <= p.uptime_s < gap
    since = st.since if st.status == ONLINE else p.ts

    if gap > threshold:
        start = st.last_ping_ts
        if not p.contiguous:
            events.append(_event("gap_unknown", p.ts, start, p.ts, reason="pings may be missing"))
        elif p.uptime_s is not None and not rebooted:
            # ESP kept running the whole time: light was on, only the link was down.
            events.append(_event("link_restored", p.ts, start, p.ts, duration=p.ts - start))
        else:
            end = p.ts - p.uptime_s if rebooted else p.ts
            end = max(end, start)
            events.append(_event("power_restored", end, start, end,
                                 duration=end - start, precise_end=rebooted))
        since = events[-1]["end"]
    elif rebooted:
        # Short blink (shorter than the threshold) or an ESP crash: informational only.
        events.append(_event("esp_reboot", p.ts, uptime_s=p.uptime_s))

    return State(ONLINE, since, p.ts, p.seq if p.seq is not None else st.last_seq), events


def on_tick(st: State, now: float, complete: bool, threshold: float):
    """Clock tick. `complete` = we are sure no ping arrived since the last one
    (relay: API answered and n_pings did not grow; direct: server was up the whole time).
    Returns (new_state, events)."""
    if st.last_ping_ts is None or now - st.last_ping_ts <= threshold:
        return st, []
    if complete and st.status in (ONLINE, UNKNOWN):
        late = st.status == UNKNOWN
        return (State(OFFLINE, st.last_ping_ts, st.last_ping_ts, st.last_seq),
                [_event("power_lost", st.last_ping_ts, late=late)])
    if not complete and st.status == ONLINE:
        return (State(UNKNOWN, st.last_ping_ts, st.last_ping_ts, st.last_seq),
                [_event("no_data", st.last_ping_ts)])
    return st, []
