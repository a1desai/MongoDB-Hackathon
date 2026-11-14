"""Outdoor temperature. Weather docs from Atlas are cached on world.scenario["_weather"]
as BSON-safe lists `{"minutes": [...], "temps": [...]}` (minutes since midnight of the sim day).

Outside the cached range (warm-up before 12:00, forecasts past 21:30) the synthetic diurnal
curve is used, shifted so it joins the data continuously at the nearest edge.
"""

import math
from datetime import datetime
from typing import Any

import numpy as np

from ..timeutil import parse_sim
from . import clock, profiles
from .world_state import World

T_MIN_C = 24.0  # synthetic heat-wave curve: min at 05:00
T_MAX_C = 36.0  # max at 16:00
H_MIN = 5.0
H_MAX = 16.0


def synthetic_c(hour: float, delta_c: float = 0.0) -> float:
    """PLAN §5.1 fallback curve: 24 °C at 05:00 rising to 36 °C at 16:00 (cosine segments)."""
    h = hour % 24.0
    mid = (T_MIN_C + T_MAX_C) / 2.0
    amp = (T_MAX_C - T_MIN_C) / 2.0
    if H_MIN <= h < H_MAX:
        val = mid - amp * math.cos(math.pi * (h - H_MIN) / (H_MAX - H_MIN))
    else:
        span = 24.0 - (H_MAX - H_MIN)
        val = mid + amp * math.cos(math.pi * ((h - H_MAX) % 24.0) / span)
    return val + delta_c


def delta_c(scenario: dict[str, Any]) -> float:
    w = scenario.get("weather") or {}
    try:
        return float(w.get("delta_c", 0.0))
    except (TypeError, ValueError):
        return 0.0


def cache_from_docs(docs: list[dict[str, Any]], *, offset_c: float | None = None) -> dict[str, list[float]] | None:
    """Turn weather docs ({ts, temp_c}) into the cached minute/temperature lists."""
    pts: list[tuple[datetime, float]] = []
    for d in docs:
        ts, temp = d.get("ts"), d.get("temp_c")
        if ts is None or temp is None:
            continue
        t = parse_sim(ts) if isinstance(ts, str) else parse_sim(ts.isoformat())
        value = float(temp)
        # Processed rows store the offset used at build time, e.g. eccc_2025-06-24+3.0.
        source = d.get("source", "")
        if offset_c is not None and source and "+" in source:
            try:
                value += offset_c - float(source.rsplit("+", 1)[1])
            except ValueError:
                pass
        pts.append((t, value))
    if len(pts) < 2:
        return None
    pts.sort(key=lambda p: p[0])
    first = pts[0][0]
    midnight = first.replace(hour=0, minute=0, second=0, microsecond=0)
    minutes = [(t - midnight).total_seconds() / 60.0 for t, _ in pts]
    return {"minutes": minutes, "temps": [v for _, v in pts]}


def synthetic_cache(scenario: dict[str, Any], start_h: float = 12.0, end_h: float = 21.5) -> dict[str, list[float]]:
    profile = profiles.parse_profile((scenario.get("weather") or {}).get("profile_c"))
    d = delta_c(scenario)
    minutes = list(np.arange(start_h * 60, end_h * 60 + 0.1, 5.0))
    temps = [round(profiles.value_at(profile, m / 60.0) if profile else synthetic_c(m / 60.0, d), 2) for m in minutes]
    return {"minutes": [float(m) for m in minutes], "temps": temps}


def _cache(world: World) -> dict[str, list[float]]:
    w = world.scenario.get("_weather")
    if not w or len(w.get("minutes", [])) < 2:
        w = synthetic_cache(world.scenario)
        world.scenario["_weather"] = w
    return w


def outdoor_c(world: World, t: datetime) -> float:
    """Outdoor °C at sim time t (linear interpolation of the 5-minute weather series)."""
    w = _cache(world)
    mins, temps = w["minutes"], w["temps"]
    m = clock.day_minutes(world, t)
    if mins[0] <= m <= mins[-1]:
        return float(np.interp(m, mins, temps))
    d = delta_c(world.scenario)
    edge_m, edge_t = (mins[0], temps[0]) if m < mins[0] else (mins[-1], temps[-1])
    return float(synthetic_c(m / 60.0, d) + (edge_t - synthetic_c(edge_m / 60.0, d)))
