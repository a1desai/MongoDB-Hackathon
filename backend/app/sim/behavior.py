"""Rule-based follower behaviour (PLAN §7.4) and named-business load (§7.5).

Archetype parameters come from world.archetype_params, which the archetype world agents update
(§11.1) and which start at the archetype defaults. All randomness uses world.rng, except the
static per-home EV-compliance draw (see aux_state).
"""

import math
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from ..timeutil import at, hhmm, parse_sim, sim_iso
from . import clock
from .aux_state import get_aux
from .thermal import BATTERY_KWH, BATTERY_MIN_SOC, EV_KW
from .world_state import World

DEFAULT_PARAMS = {"override_rate_per_hour": 0.15, "offer_acceptance": 0.30, "ev_pause_acceptance": 0.75}
OVERRIDE_TEMP_COEF = 0.8  # hazard multiplier exp(0.8*(T_in - comfort_max))
QUINTILE_ACCEPT_STEP = 0.05  # Q1 +0.10 ... Q5 -0.10
SMS_OFFER_WINDOW_MIN = 180  # an accepted offer reduces cooling for this long unless told otherwise
EVENT_EVERY_MIN = 30  # aggregate noisy follower events into one world_event per 30 sim minutes
BUSINESS_ACTIVE = {"dispatched", "active"}


def world_event(summary: str, payload: dict[str, Any] | None = None, type_: str = "world_event") -> dict[str, Any]:
    return {"type": type_, "summary": summary, "payload": payload or {}}


def real(world: World, count: int | float) -> int:
    """Simulated home count -> real-scale homes."""
    return int(round(float(count) * world.scale))


def archetype_vectors(world: World) -> dict[str, np.ndarray]:
    """Per-archetype parameter arrays aligned with world.archetype_ids."""
    out: dict[str, np.ndarray] = {}
    for key, default in DEFAULT_PARAMS.items():
        vals = []
        for aid in world.archetype_ids:
            p = world.archetype_params.get(aid) or {}
            try:
                vals.append(float(p.get(key, default)))
            except (TypeError, ValueError):
                vals.append(default)
        out[key] = np.asarray(vals if vals else [default], dtype=float)
    return out


def _per_home(world: World, vec: np.ndarray, idx: np.ndarray) -> np.ndarray:
    arch = np.clip(world.archetype_idx[idx], 0, vec.size - 1)
    return vec[arch]


def acceptance_prob(world: World, idx: np.ndarray, vecs: dict[str, np.ndarray] | None = None) -> np.ndarray:
    """p = offer_acceptance_arch ± 0.1 by income quintile (lower income responds more to incentives)."""
    vecs = vecs or archetype_vectors(world)
    base = _per_home(world, vecs["offer_acceptance"], idx)
    adj = QUINTILE_ACCEPT_STEP * (3 - world.income_quintile[idx].astype(float))
    return np.clip(base + adj, 0.0, 1.0)


def apply_sms_offer(
    world: World,
    idx: np.ndarray,
    until: datetime | None = None,
    *,
    accept_scale: float = 1.0,
    include_vulnerable: bool = False,
    include_featured: bool = False,
    count_message: bool = False,
) -> int:
    """Roll SMS acceptance for the given homes now; accepted homes cut AC duty by 30% until `until`.

    Used for direct delivery (baseline broadcast). Governed deliveries are picked up by
    step_behavior from sms_count_today increments. Returns the number of simulated homes accepting.
    """
    aux = get_aux(world)
    idx = np.asarray(idx, dtype=np.int64)
    if count_message and idx.size:
        world.sms_count_today[idx] += 1
    aux.sms_seen[idx] = world.sms_count_today[idx]
    ok = ~world.sms_stopped[idx] & ~world.sms_accepted[idx] & (world.ac_type[idx] > 0)
    if not include_vulnerable:
        ok &= ~world.vulnerable[idx]
    if not include_featured:
        ok &= ~aux.featured_mask[idx]
    cand = idx[ok]
    if cand.size == 0:
        return 0
    p = np.clip(acceptance_prob(world, cand) * accept_scale, 0.0, 1.0)
    hit = cand[world.rng.random(cand.size) < p]
    world.sms_accepted[hit] = True
    end_min = clock.sim_minute(world, until) if until else clock.sim_minute(world) + SMS_OFFER_WINDOW_MIN
    aux.sms_accept_until[hit] = end_min
    return int(hit.size)


def _step_overrides(world: World, dt_h: float, vecs: dict[str, np.ndarray]) -> list[dict]:
    aux = get_aux(world)
    at_risk = world.setback_active & ~world.overridden & (world.ac_type > 0) & world.power_on
    if at_risk.any():
        idx = np.flatnonzero(at_risk)
        base = _per_home(world, vecs["override_rate_per_hour"], idx)
        expo = np.clip(OVERRIDE_TEMP_COEF * (world.t_in[idx] - world.comfort_max[idx]), -20.0, 20.0)
        hazard = base * np.exp(expo)
        p = 1.0 - np.exp(-hazard * dt_h)
        hit = idx[world.rng.random(idx.size) < p]
        world.overridden[hit] = True
        aux.override_new += int(hit.size)
    now_min = clock.sim_minute(world)
    if aux.override_new and now_min - aux.override_last_emit_min >= EVENT_EVERY_MIN:
        n_new = aux.override_new
        aux.override_new, aux.override_last_emit_min = 0, now_min
        total = int(world.overridden.sum())
        return [
            world_event(
                f"{real(world, n_new):,} homes overrode their CoolShift setback as indoor heat rose "
                f"({real(world, total):,} overridden in total)",
                {"kind": "setback_override", "homes_sim": n_new, "homes_real": real(world, n_new),
                 "total_real": real(world, total)},
            )
        ]
    return []


def _step_sms(world: World) -> None:
    aux = get_aux(world)
    now_min = clock.sim_minute(world)
    expired = world.sms_accepted & (aux.sms_accept_until >= 0) & (aux.sms_accept_until <= now_min)
    if expired.any():
        world.sms_accepted[expired] = False
        aux.sms_accept_until[expired] = -1
    new = world.sms_count_today > aux.sms_seen
    if new.any():
        apply_sms_offer(world, np.flatnonzero(new))


def _step_ev(world: World, dt_h: float, vecs: dict[str, np.ndarray]) -> list[dict]:
    aux = get_aux(world)
    paused = world.ev_paused & world.has_ev
    if paused.any():
        idx = np.flatnonzero(paused)
        comply = aux.ev_comply_u[idx] < _per_home(world, vecs["ev_pause_acceptance"], idx)
        overrides = idx[~comply]
        if overrides.size:
            world.ev_paused[overrides] = False
            aux.ev_override_new += int(overrides.size)
    hours = clock.day_hours(world, world.sim_now)
    with np.errstate(invalid="ignore"):
        arrived = world.has_ev & (hours >= np.nan_to_num(world.ev_arrival_h, nan=np.inf))
    charging = arrived & (world.ev_kwh_needed > 0) & ~world.ev_paused & world.power_on
    world.ev_kwh_needed[charging] = np.maximum(world.ev_kwh_needed[charging] - EV_KW * dt_h, 0.0)
    world.ev_charging[:] = charging
    now_min = clock.sim_minute(world)
    if aux.ev_override_new and now_min - aux.ev_override_last_emit_min >= EVENT_EVERY_MIN:
        n_new = aux.ev_override_new
        aux.ev_override_new, aux.ev_override_last_emit_min = 0, now_min
        return [
            world_event(
                f"{real(world, n_new):,} EV owners resumed charging despite a ChargeFlow pause",
                {"kind": "ev_pause_override", "homes_sim": n_new, "homes_real": real(world, n_new)},
            )
        ]
    return []


def _step_batteries(world: World, dt_h: float) -> list[dict]:
    dis = world.battery_discharging & world.has_battery & world.power_on
    if not dis.any():
        return []
    idx = np.flatnonzero(dis)
    world.battery_soc[idx] = world.battery_soc[idx] - world.battery_kw[idx] * dt_h / BATTERY_KWH
    empty = idx[world.battery_soc[idx] <= BATTERY_MIN_SOC]
    if empty.size == 0:
        return []
    world.battery_soc[empty] = BATTERY_MIN_SOC
    world.battery_discharging[empty] = False
    return [
        world_event(
            f"{real(world, empty.size):,} PowerCell batteries stopped discharging at 20% state of charge",
            {"kind": "battery_floor", "homes_sim": int(empty.size), "homes_real": real(world, empty.size)},
        )
    ]


def as_datetime(world: World, v: Any) -> datetime | None:
    """Accept a datetime, an ISO string or "HH:MM" (on the sim day)."""
    if v is None or v == "":
        return None
    if isinstance(v, datetime):
        return v
    s = str(v)
    if len(s) <= 5 and ":" in s:
        return at(world.sim_now, s)
    return parse_sim(s)


def step_businesses(world: World) -> list[dict]:
    """Named businesses only shed load after accepting and being dispatched, respecting notice."""
    now = world.sim_now
    events: list[dict] = []
    for bid, b in world.businesses.items():
        base = float(b.get("base_load_mw", b.get("load_mw", 0.0)) or 0.0)
        b["base_load_mw"] = base
        status = b.get("status") or "idle"
        accepted = float(b.get("accepted_mw") or 0.0)
        curtailed = 0.0
        if status in BUSINESS_ACTIVE and accepted > 0:
            if not b.get("dispatched_at"):
                b["dispatched_at"] = b.get("dispatched_sim_ts") or sim_iso(now)
            disp = as_datetime(world, b["dispatched_at"]) or now
            start = disp + timedelta(minutes=float(b.get("notice_min") or 0))
            af = as_datetime(world, b.get("active_from"))
            if af and af > start:
                start = af
            until = as_datetime(world, b.get("active_until"))
            if until is not None and now >= until:
                if status == "active":
                    events.append(world_event(f"{b.get('name', bid)} ended its curtailment at {hhmm(now)}",
                                              {"kind": "business_curtail_end", "business_id": bid}))
                status = "completed"
            elif now >= start:
                cap = float(b.get("max_mw") or 0.0)
                curtailed = max(0.0, min(accepted, cap, base))
                if status != "active":
                    events.append(world_event(
                        f"{b.get('name', bid)} began shedding {curtailed:.1f} MW at {hhmm(now)}",
                        {"kind": "business_curtail_start", "business_id": bid, "mw": round(curtailed, 2)}))
                    status = "active"
        b["status"] = status
        b["curtailed_mw"] = curtailed
        b["load_mw"] = base - curtailed
    return events


def step_behavior(world: World, dt_h: float) -> list[dict]:
    """Follower overrides, SMS acceptance effects, EV arrivals/charging/pauses, batteries, businesses."""
    vecs = archetype_vectors(world)
    events: list[dict] = []
    events += _step_overrides(world, dt_h, vecs)
    _step_sms(world)
    events += _step_ev(world, dt_h, vecs)
    events += _step_batteries(world, dt_h)
    events += step_businesses(world)
    return events


def override_hazard(base_rate: float, t_in: float, comfort_max: float) -> float:
    """Scalar helper (tests/explanations): hourly override hazard for one home."""
    return base_rate * math.exp(OVERRIDE_TEMP_COEF * (t_in - comfort_max))
