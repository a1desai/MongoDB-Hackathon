"""Daily profiles from the scenario ("HH:MM" -> value maps), linearly interpolated and periodic over 24 h.

Parsed profiles are cached on the scenario dict under a "_" key as BSON-safe lists, so they
travel with world copies and are stripped by `scenario.public_scenario`.
"""

from typing import Any

import numpy as np

# Typical home non-AC load (kW at base_kw_scale = 1) when the scenario has no calibration.base_kw_profile.
DEFAULT_BASE_KW: dict[str, float] = {
    "00:00": 0.6, "06:00": 0.6, "09:00": 0.8, "12:00": 0.9, "15:00": 1.0, "17:00": 1.3,
    "18:00": 1.5, "19:00": 1.6, "20:00": 1.5, "21:00": 1.3, "23:00": 0.9,
}

# Normalised summer zone-demand shape (1.0 at 17:00) used when no commercial profile is given.
DEFAULT_SHAPE: dict[str, float] = {
    "00:00": 0.63, "04:00": 0.56, "06:00": 0.60, "08:00": 0.83, "10:00": 0.97, "12:00": 0.99,
    "14:00": 1.00, "16:00": 1.02, "17:00": 1.00, "18:00": 0.97, "19:00": 0.93, "20:00": 0.87,
    "21:00": 0.83, "22:00": 0.74, "23:00": 0.68,
}


def _key_hours(k: Any) -> float | None:
    s = str(k).strip()
    try:
        if ":" in s:
            h, m = s.split(":")[:2]
            return int(h) + int(m) / 60.0
        return float(s) / 60.0 if float(s) >= 24 else float(s)
    except (TypeError, ValueError):
        return None


def parse_profile(mapping: dict[str, Any] | None) -> dict[str, list[float]] | None:
    """{"HH:MM": v} -> {"h": [...], "v": [...]} sorted, closed over 24 h for periodic interpolation."""
    if not mapping or not isinstance(mapping, dict):
        return None
    pts: dict[float, float] = {}
    for k, v in mapping.items():
        h = _key_hours(k)
        if h is None or v is None:
            continue
        try:
            pts[h % 24.0 if h < 24.0 else 24.0] = float(v)
        except (TypeError, ValueError):
            continue
    if not pts:
        return None
    if 24.0 in pts and 0.0 not in pts:
        pts[0.0] = pts[24.0]
    pts.pop(24.0, None)
    hs = sorted(pts)
    vs = [pts[h] for h in hs]
    # wrap: one point before 0 and one after 24 so np.interp is periodic
    hs_w = [hs[-1] - 24.0, *hs, hs[0] + 24.0]
    vs_w = [vs[-1], *vs, vs[0]]
    return {"h": [float(x) for x in hs_w], "v": [float(x) for x in vs_w]}


def cached_profile(scenario: dict[str, Any], cache_key: str, mapping: dict[str, Any] | None,
                   default: dict[str, float]) -> dict[str, list[float]]:
    prof = scenario.get(cache_key)
    if not prof:
        prof = parse_profile(mapping) or parse_profile(default)
        scenario[cache_key] = prof
    return prof  # type: ignore[return-value]


def value_at(prof: dict[str, list[float]], hour: float) -> float:
    return float(np.interp(hour % 24.0, prof["h"], prof["v"]))


def calibration(scenario: dict[str, Any]) -> dict[str, Any]:
    c = scenario.get("calibration")
    return c if isinstance(c, dict) else {}


def base_kw_profile(scenario: dict[str, Any]) -> dict[str, list[float]]:
    return cached_profile(scenario, "_base_kw", calibration(scenario).get("base_kw_profile"), DEFAULT_BASE_KW)


def commercial_profile(scenario: dict[str, Any]) -> dict[str, list[float]]:
    """Commercial background MW by hour; defaults to 45% of target_peak_mw times a summer shape."""
    prof = scenario.get("_commercial")
    if prof:
        return prof
    cal = calibration(scenario)
    prof = parse_profile(cal.get("commercial_background_mw"))
    if prof is None:
        share = float(cal.get("commercial_background_share", 0.45) or 0.45)
        target = float(scenario.get("target_peak_mw", 4000.0) or 4000.0)
        shape = parse_profile(cal.get("ieso_demand_shape")) or parse_profile(DEFAULT_SHAPE)
        assert shape is not None
        peak_h = _key_hours(cal.get("peak_time", "17:00")) or 17.0
        norm = value_at(shape, peak_h) or 1.0
        prof = {"h": shape["h"], "v": [share * target * v / norm for v in shape["v"]]}
    scenario["_commercial"] = prof
    return prof


def supply_profile(scenario: dict[str, Any]) -> list[tuple[float, float]] | None:
    """Planned supply schedule as sorted [(hour, mw)] step points, or None for a flat supply_mw."""
    sched = scenario.get("supply_schedule_mw")
    if not isinstance(sched, dict) or not sched:
        return None
    pts: list[tuple[float, float]] = []
    for k, v in sched.items():
        h = _key_hours(k)
        try:
            if h is not None:
                pts.append((h, float(v)))
        except (TypeError, ValueError):
            continue
    return sorted(pts) or None
