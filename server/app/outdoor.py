"""Outdoor temperature at the village from Open-Meteo (no key, outgoing request only).

It is model data for a ~2 km grid cell, not a weather station, but good enough to see
how much warmer the room is than the street.
"""
URL = "https://api.open-meteo.com/v1/forecast"


def params(lat: float, lon: float, past_days: int) -> dict:
    return {"latitude": lat, "longitude": lon, "hourly": "temperature_2m",
            "past_days": past_days, "forecast_days": 1, "timeformat": "unixtime"}


def parse(data: dict, now: float) -> list:
    """Hourly (ts, t) pairs that are already in the past; the forecast part is skipped."""
    hourly = (data or {}).get("hourly") or {}
    out = []
    for ts, t in zip(hourly.get("time") or [], hourly.get("temperature_2m") or []):
        if t is not None and ts <= now:
            out.append((float(ts), float(t)))
    return out
