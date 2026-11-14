"""Vectorized single-node thermal model per home (PLAN §7.3).

For each home with power on and AC present:
    S         = setpoint_base + offset          (offset ignored once the user has overridden)
    duty_hold = clip(k_env*(T_out - S)/k_ac, 0, 1)
    duty      = 1 if T_in > S+0.3 else 0 if T_in < S-0.3 else duty_hold     (x0.7 if an SMS offer was accepted)
    T_in     += dt_h*(k_env*(T_out - T_in) + solar(t) - k_ac*duty)
    P_kw      = base_kw(t) + ac_kw*duty + ev_kw*charging - battery_kw*discharging
Power off: duty = 0, P = 0 and T_in drifts toward T_out (plus solar gain).
"""

from datetime import datetime, timedelta

import numpy as np

from . import clock, profiles
from .weather import outdoor_c
from .world_state import World

DEADBAND_C = 0.3
SMS_DUTY_FACTOR = 0.7  # accepted incentive: AC duty reduced by 30%
EV_KW = 7.2
BATTERY_KWH = 13.5
BATTERY_KW = 5.0
BATTERY_MIN_SOC = 0.2
SOLAR_START_H = 11.0
SOLAR_END_H = 18.0


def base_profile_kw(hour: float, scenario: dict | None = None) -> float:
    """Non-AC household load (kW at base_kw_scale = 1) at an hour of day, from the scenario's
    calibration.base_kw_profile (PLAN §5.2 item 8) or the built-in default."""
    prof = profiles.base_kw_profile(scenario if scenario is not None else {})
    return profiles.value_at(prof, hour)


def solar_on(t: datetime) -> bool:
    h = clock.hour_of_day(t)
    return SOLAR_START_H <= h < SOLAR_END_H


def effective_offset(world: World) -> np.ndarray:
    """The applied ThermLink offset, or 0 where the resident has overridden."""
    return np.where(world.overridden, 0.0, world.offset_c)


def effective_setpoint(world: World) -> np.ndarray:
    return world.setpoint_base + effective_offset(world)


def _duty(t_in: np.ndarray, s: np.ndarray, k_env: np.ndarray, k_ac: np.ndarray, t_out: float) -> np.ndarray:
    safe_k = np.where(k_ac > 0, k_ac, 1.0)
    hold = np.clip(np.where(k_ac > 0, k_env * (t_out - s) / safe_k, 0.0), 0.0, 1.0)
    return np.where(t_in > s + DEADBAND_C, 1.0, np.where(t_in < s - DEADBAND_C, 0.0, hold))


def household_kw(world: World, t: datetime | None = None) -> np.ndarray:
    """Per-home load (kW) from the current duty and device flags; 0 where power is off."""
    t = t or world.sim_now
    on = world.power_on
    ev = world.ev_charging & ~world.ev_paused & on
    bat = world.battery_discharging & on
    p = (
        world.base_kw_scale * base_profile_kw(clock.hour_of_day(t), world.scenario)
        + world.ac_kw * world.duty
        + EV_KW * ev
        - world.battery_kw * bat
    )
    return np.where(on, p, 0.0)


def step_thermal(world: World, dt_h: float, t_out: float) -> None:
    """Advance indoor temperatures one tick in place and refresh duty and p_kw."""
    solar = world.solar_gain if solar_on(world.sim_now) else 0.0
    s = effective_setpoint(world)
    duty = _duty(world.t_in, s, world.k_env, world.k_ac, t_out)
    duty = np.where(world.sms_accepted, duty * SMS_DUTY_FACTOR, duty)
    duty = np.where((world.ac_type > 0) & world.power_on, duty, 0.0)
    world.t_in += dt_h * (world.k_env * (t_out - world.t_in) + solar - world.k_ac * duty)
    world.duty[:] = duty
    world.p_kw[:] = household_kw(world, world.sim_now)


def _indices(world: World, mask: np.ndarray) -> np.ndarray:
    mask = np.asarray(mask)
    if mask.dtype == bool:
        return np.flatnonzero(mask)
    return mask.astype(np.int64, copy=False)


def predict_max_tin(
    world: World,
    mask: np.ndarray,
    offset_c: float,
    start: datetime,
    end: datetime,
    *,
    power_off: bool = False,
    dt_h: float | None = None,
) -> np.ndarray:
    """Simulate the masked homes forward from now and return each one's max T_in in [start, end].

    `offset_c` is applied during the window (replacing the current offset, overrides ignored,
    which is the conservative case for R05). Before the window the current effective offset is
    used. `power_off=True` models a feeder outage during the window. Pure: world is not mutated.
    `mask` may be a boolean mask (length n) or an index array; output follows its order.
    """
    idx = _indices(world, mask)
    if idx.size == 0:
        return np.empty(0, dtype=float)
    now = world.sim_now
    t_in = world.t_in[idx].astype(float, copy=True)
    if end <= now:
        return t_in
    dt = dt_h or clock.dt_hours()
    step = timedelta(hours=dt)
    start = max(start, now)
    k_env, k_ac = world.k_env[idx], world.k_ac[idx]
    s_base = world.setpoint_base[idx]
    cur_off = effective_offset(world)[idx]
    solar_gain = world.solar_gain[idx]
    has_ac = world.ac_type[idx] > 0
    on_now = world.power_on[idx]
    sms = world.sms_accepted[idx]
    max_t = t_in.copy() if start <= now else np.full(idx.size, -np.inf)
    t = now
    while t < end:
        in_win = start <= t < end
        s = s_base + (offset_c if in_win else cur_off)
        on = on_now & (not (power_off and in_win))
        t_out = outdoor_c(world, t)
        duty = _duty(t_in, s, k_env, k_ac, t_out)
        duty = np.where(sms, duty * SMS_DUTY_FACTOR, duty)
        duty = np.where(has_ac & on, duty, 0.0)
        solar = solar_gain if solar_on(t) else 0.0
        t_in = t_in + dt * (k_env * (t_out - t_in) + solar - k_ac * duty)
        t = t + step
        if t >= start:
            max_t = np.maximum(max_t, t_in)
    return np.where(np.isfinite(max_t), max_t, t_in)


def steady_duty(world: World, idx: np.ndarray, setpoint: np.ndarray, t_out: float, solar_on_now: bool) -> np.ndarray:
    """Duty needed to hold `setpoint` (including solar gain), clipped to [0, 1]."""
    k_env, k_ac = world.k_env[idx], world.k_ac[idx]
    solar = world.solar_gain[idx] if solar_on_now else 0.0
    safe_k = np.where(k_ac > 0, k_ac, 1.0)
    return np.clip(np.where(k_ac > 0, (k_env * (t_out - setpoint) + solar) / safe_k, 0.0), 0.0, 1.0)


def kw_saved_estimate(world: World, mask: np.ndarray, offset_c: float) -> np.ndarray:
    """Estimated kW relief per masked home if `offset_c` were applied now.

    Setback (offset > 0): drop in steady-state AC kW (0 for homes whose AC is saturated either way).
    Pre-cool (offset < 0): average AC kW avoided over the first hour after the pre-cool ends,
    from coasting down the stored |offset| °C. Homes without a thermostat, AC or power get 0.
    """
    idx = _indices(world, mask)
    if idx.size == 0:
        return np.empty(0, dtype=float)
    t_out = outdoor_c(world, world.sim_now)
    sol = solar_on(world.sim_now)
    s0 = effective_setpoint(world)[idx]
    d0 = steady_duty(world, idx, s0, t_out, sol)
    ac_kw = world.ac_kw[idx]
    if offset_c >= 0:
        s1 = world.setpoint_base[idx] + offset_c
        saved = ac_kw * (d0 - steady_duty(world, idx, s1, t_out, sol))
    else:
        solar = world.solar_gain[idx] if sol else 0.0
        drift = np.maximum(world.k_env[idx] * (t_out - s0) + solar, 0.1)  # °C/h warming with AC off
        saved = ac_kw * d0 * np.minimum(1.0, abs(offset_c) / drift)
    eligible = world.has_thermostat[idx] & (world.ac_type[idx] > 0) & world.power_on[idx]
    return np.where(eligible, np.maximum(saved, 0.0), 0.0)
