"""Indoor-temperature prediction for rule R05 (SAFE_INDOOR).

Thermostat offsets use `sim.thermal.predict_max_tin` (the same model the Targeter ranks with)
when it is importable. Outages (power off) and the no-sim fallback use a small forward copy of
the PLAN §7.3 physics. Pure numpy, no I/O, never mutates the World.
"""

import logging
from collections.abc import Callable
from datetime import datetime, timedelta

import numpy as np

from app.sim.world_state import World

log = logging.getLogger(__name__)

STEP_MIN = 5
DEFAULT_T_OUT_C = 33.0
# Signature: (world, idx, offset_c, start, end, power_off, t_out_c) -> max T_in per idx home.
Predictor = Callable[[World, np.ndarray, float, datetime, datetime, bool, float | None], np.ndarray]


def _outdoor(world: World, t: datetime, fallback: float | None) -> float:
    try:
        from app.sim.weather import outdoor_c

        return float(outdoor_c(world, t))
    except Exception:
        return float(fallback if fallback is not None else DEFAULT_T_OUT_C)


def local_predict(
    world: World,
    idx: np.ndarray,
    offset_c: float,
    start: datetime,
    end: datetime,
    power_off: bool = False,
    t_out_c: float | None = None,
) -> np.ndarray:
    """Forward-simulate homes `idx` from sim_now to `end`; return max T_in inside [start, end]."""
    idx = np.asarray(idx, dtype=np.int64)
    if idx.size == 0:
        return np.zeros(0)
    now = world.sim_now
    start = max(start, now)
    if end <= start:
        end = start + timedelta(minutes=STEP_MIN)
    dt_h = STEP_MIN / 60.0

    t_in = world.t_in[idx].astype(float).copy()
    k_env = world.k_env[idx].astype(float)
    k_ac = world.k_ac[idx].astype(float)
    has_ac = (world.ac_type[idx] > 0) & (k_ac > 0)
    solar = world.solar_gain[idx].astype(float)
    base_sp = world.setpoint_base[idx].astype(float)
    cur_off = world.offset_c[idx].astype(float)
    power = world.power_on[idx].astype(bool)
    duty = world.duty[idx].astype(float).copy()
    safe_k_ac = np.where(k_ac > 0, k_ac, 1.0)

    max_t = np.full(idx.size, -np.inf)
    t = now
    while t < end:
        in_window = t >= start
        off = offset_c if in_window else cur_off
        sp = base_sp + off
        on = power & ~(power_off and in_window)
        t_out = _outdoor(world, t, t_out_c)
        hold = np.clip(k_env * (t_out - sp) / safe_k_ac, 0.0, 1.0)
        duty = np.where(t_in > sp + 0.3, 1.0, np.where(t_in < sp - 0.3, 0.0, hold))
        duty = np.where(has_ac & on, duty, 0.0)
        sun = solar if 11 <= t.hour < 18 else 0.0
        t_in = t_in + dt_h * (k_env * (t_out - t_in) + sun - k_ac * duty)
        t = t + timedelta(minutes=STEP_MIN)
        if t > start:
            max_t = np.maximum(max_t, t_in)
    return np.where(np.isfinite(max_t), max_t, t_in)


def default_predict(
    world: World,
    idx: np.ndarray,
    offset_c: float,
    start: datetime,
    end: datetime,
    power_off: bool = False,
    t_out_c: float | None = None,
) -> np.ndarray:
    """Use sim.thermal.predict_max_tin (offsets and outages) when available, else the local model."""
    idx = np.asarray(idx, dtype=np.int64)
    if idx.size:
        try:
            from app.sim import thermal

            if power_off:
                res = np.asarray(thermal.predict_max_tin(world, idx, float(offset_c), start, end, power_off=True), dtype=float)
            else:
                res = np.asarray(thermal.predict_max_tin(world, idx, float(offset_c), start, end), dtype=float)
            if res.shape[0] == idx.size:
                return res
        except Exception as exc:  # module missing or shape mismatch: fall back quietly
            log.debug("sim.thermal.predict_max_tin unavailable (%s); using local model", exc)
    return local_predict(world, idx, offset_c, start, end, power_off, t_out_c)
