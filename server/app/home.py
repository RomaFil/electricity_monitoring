"""Home light: reports from power-guard (the Proxmox laptop at home).

power-guard runs its own state machine on the laptop and sends ready-made
events with laptop timestamps. Here we only map them to our event types and
derive the current status for the dashboard. Pure logic, no I/O.
"""
SOURCE = "home"

# power-guard event -> our event type
EVENT_MAP = {
    "ac_lost": "power_lost",
    "ac_restored": "power_restored",
    "sleep_start": "sleep_start",
    "sleep_end": "sleep_end",
    "forced_poweroff": "forced_poweroff",
    "unknown_gap": "gap_unknown",
    "rtcwake_error": "rtcwake_error",
    "guard_start": "guard_start",
}


def map_event(e: dict) -> dict | None:
    """Convert one power-guard event into a row for the events table."""
    type_ = EVENT_MAP.get(e.get("type"))
    if not type_ or not e.get("id") or e.get("ts") is None:
        return None
    detail = dict(e.get("detail") or {})
    for k in ("battery", "ac", "status"):
        if k in e:
            detail[k] = e[k]
    return {
        "source": SOURCE, "type": type_, "ts": float(e["ts"]),
        "start": detail.pop("start", None), "end": detail.pop("end", None),
        "detail": detail, "ext_id": f"pg:{e['id']}",
    }


def status(last_sample: dict | None, last_ctrl: dict | None, now: float,
           stale_s: float, wake_interval: float) -> dict:
    """Current home status.
    last_sample: newest power sample {"ts","ac","battery","status","state"}.
    last_ctrl:   newest of sleep_start / sleep_end / forced_poweroff / guard_start events.
    """
    if not last_sample and not last_ctrl:
        return {"status": "never"}
    s = last_sample or {}
    out = {"ac": s.get("ac"), "battery": s.get("battery"), "bat_status": s.get("status"),
           "last_report": s.get("ts"), "guard_state": s.get("state")}
    ctrl_after_sample = last_ctrl and (not s or last_ctrl["ts"] >= s["ts"])

    if ctrl_after_sample and last_ctrl["type"] == "forced_poweroff":
        out.update(status="off", since=last_ctrl["ts"],
                   banner="Домашній сервер вимкнувся через розряд батареї. "
                          "Потрібен ручний запуск кнопкою живлення.")
        return out
    if ctrl_after_sample and last_ctrl["type"] == "sleep_start":
        late = now - last_ctrl["ts"] > wake_interval + stale_s
        out.update(status="no_data" if late else "sleeping", since=last_ctrl["ts"])
        if late:
            out["banner"] = "Домашній сервер заснув і не прокинувся вчасно."
        return out
    if not s or now - s["ts"] > stale_s:
        out.update(status="no_data", since=s.get("ts"))
        return out
    out["status"] = "online" if s.get("ac") else "on_battery"
    return out
