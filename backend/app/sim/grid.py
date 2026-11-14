"""Zone and section demand, supply schedule, contingencies and the uncontrolled trip (PLAN §7.6).

Zone demand (real MW) = scale * residential_sim_mw + commercial_background_mw(t) + named businesses.
Commercial background is shared across sections by household count; cutting a section drops its
homes, its share of the commercial background and any named business located in it.

`world.section_demand_mw[s]` is the section's load if energised: the served load for a live
section, and the load it would draw on reconnection for a cut one (warm homes run their AC flat
out, so this is the rebound the Restorer and R16 must plan for). `aggregate()["sections"]` is the
served load (0 for a cut section), which is what feeder_load records.
"""

from datetime import datetime, timedelta
from typing import Any

import numpy as np

from ..timeutil import at, hhmm, parse_sim, sim_iso
from . import clock, profiles, thermal
from .aux_state import get_aux
from .weather import outdoor_c
from .world_state import World

TRIP_THRESHOLD = 0.03  # shortfall above 3% of supply ...
TRIP_TICKS = 3  # ... for 3 consecutive ticks trips a random section
TRIP_MINUTES = 90


def _event(summary: str, payload: dict[str, Any], type_: str = "world_event") -> dict[str, Any]:
    return {"type": type_, "summary": summary, "payload": payload}


def real(world: World, count: int | float) -> int:
    return int(round(float(count) * world.scale))


# ---------------------------------------------------------------- supply and background

def commercial_background_mw(world: World, t: datetime | None = None) -> float:
    t = t or world.sim_now
    return profiles.value_at(profiles.commercial_profile(world.scenario), clock.hour_of_day(t))


def planned_supply_mw(world: World, t: datetime | None = None) -> float:
    """Scheduled supply before contingencies: the latest supply_schedule_mw step <= t, else supply_mw."""
    t = t or world.sim_now
    flat = float(world.scenario.get("supply_mw", 0.0) or 0.0)
    sched = profiles.supply_profile(world.scenario)
    if not sched:
        return flat
    h = clock.day_hours(world, t)
    val = sched[0][1]
    for sh, mw in sched:
        if sh <= h + 1e-9:
            val = mw
        else:
            break
    return float(val)


def supply_mw(world: World, t: datetime) -> float:
    """Scenario supply minus contingencies that have already fired (the Forecaster's view too)."""
    return max(planned_supply_mw(world, t) - float(world.contingency_lost_mw), 0.0)


def businesses_mw(world: World) -> tuple[float, np.ndarray, np.ndarray]:
    """(total served MW, per-section served MW, per-section MW if energised) for named businesses."""
    s = len(world.section_ids)
    served = np.zeros(s)
    potential = np.zeros(s)
    total = 0.0
    for b in world.businesses.values():
        load = float(b.get("load_mw", b.get("base_load_mw", 0.0)) or 0.0)
        sid = b.get("section_id")
        sidx = world.section_ids.index(sid) if sid in world.section_ids else -1
        if sidx >= 0:
            potential[sidx] += load
            if world.section_cut[sidx]:
                continue
            served[sidx] += load
        total += load
    return total, served, potential


def businesses_base_mw(world: World) -> float:
    return float(sum(float(b.get("base_load_mw", b.get("load_mw", 0.0)) or 0.0) for b in world.businesses.values()))


# ---------------------------------------------------------------- aggregation

def _rebound_kw(world: World) -> np.ndarray:
    """kW each home would draw if its power came back now (AC duty from its current T_in)."""
    t_out = outdoor_c(world, world.sim_now)
    s = thermal.effective_setpoint(world)
    duty = thermal._duty(world.t_in, s, world.k_env, world.k_ac, t_out)
    duty = np.where(world.ac_type > 0, duty, 0.0)
    return world.base_kw_scale * thermal.base_profile_kw(clock.hour_of_day(world.sim_now), world.scenario) + world.ac_kw * duty


def aggregate(world: World) -> dict:
    """Section and zone demand vs supply for the current tick; updates the world's grid fields."""
    s = len(world.section_ids)
    scale = float(world.scale)
    res_kw = np.bincount(world.section_idx, weights=world.p_kw, minlength=s)[:s]
    res_mw = res_kw * scale / 1000.0
    hh = np.asarray(world.section_households, dtype=float)
    share = hh / hh.sum() if hh.sum() > 0 else np.full(s, 1.0 / max(s, 1))
    comm = commercial_background_mw(world) * share
    cut = np.asarray(world.section_cut, dtype=bool)
    biz_total, biz_served, biz_potential = businesses_mw(world)
    served = np.where(cut, 0.0, res_mw + comm) + biz_served
    potential = served.copy()
    if cut.any():
        off = ~world.power_on
        reb_kw = np.bincount(world.section_idx[off], weights=_rebound_kw(world)[off], minlength=s)[:s]
        potential = np.where(cut, reb_kw * scale / 1000.0 + comm + biz_potential, served)
    # businesses outside any known section still count toward the zone
    unsited = biz_total - float(biz_served.sum())
    demand = float(served.sum() + max(unsited, 0.0))
    supply = supply_mw(world, world.sim_now)
    world.section_demand_mw = potential
    world.demand_mw = demand
    world.supply_mw = supply
    world.shortfall_mw = max(demand - supply, 0.0)
    return {
        "demand_mw": round(demand, 3),
        "supply_mw": round(supply, 3),
        "shortfall_mw": round(world.shortfall_mw, 3),
        "margin_mw": round(supply - demand, 3),
        "residential_mw": round(float(np.where(cut, 0.0, res_mw).sum()), 3),
        "commercial_mw": round(float(np.where(cut, 0.0, comm).sum()), 3),
        "businesses_mw": round(float(biz_served.sum() + max(unsited, 0.0)), 3),
        "sections": {sid: round(float(served[i]), 3) for i, sid in enumerate(world.section_ids)},
        "sections_if_energised": {sid: round(float(potential[i]), 3) for i, sid in enumerate(world.section_ids)},
    }


# ---------------------------------------------------------------- contingencies

def _contingency_time(world: World, c: dict[str, Any]) -> datetime | None:
    a = c.get("at")
    if a is None:
        return None
    if isinstance(a, datetime):
        return a
    s = str(a)
    if len(s) <= 5 and ":" in s:
        return at(world.sim_start, s)
    try:
        return parse_sim(s)
    except ValueError:
        return None


def fire_contingency(world: World, mw: float, label: str, kind: str = "generator_trip",
                     duration_min: float | None = None, scheduled: bool = True) -> dict[str, Any]:
    """Drop supply by `mw` now and return the contingency event."""
    aux = get_aux(world)
    mw = float(mw)
    world.contingency_lost_mw += mw
    if duration_min:
        aux.contingency_restores.append((world.sim_now + timedelta(minutes=float(duration_min)), mw, label))
    world.metrics["contingencies"] = int(world.metrics.get("contingencies", 0)) + 1
    supply = supply_mw(world, world.sim_now)
    return _event(
        f"{label}: {mw:,.0f} MW of supply lost at {hhmm(world.sim_now)}; supply now {supply:,.0f} MW",
        {"kind": kind, "mw": mw, "label": label, "scheduled": scheduled, "supply_mw": round(supply, 1),
         "contingency_lost_mw": round(world.contingency_lost_mw, 1), "at": sim_iso(world.sim_now),
         **({"duration_min": duration_min} if duration_min else {})},
        "contingency",
    )


def inject_contingency(world: World, mw: float | None = None, label: str | None = None) -> dict[str, Any]:
    """Operator-injected generator trip (POST /runs/{id}/inject), fired immediately."""
    sc = (world.scenario.get("contingencies") or [{}])[0] or {}
    return fire_contingency(world, float(mw or sc.get("mw", 350.0)),
                            label or "Injected generator trip (fictional)", "generator_trip", scheduled=False)


def check_contingencies(world: World) -> list[dict]:
    """Fire scheduled contingencies whose time has come; restore timed ones that have ended."""
    aux = get_aux(world)
    events: list[dict] = []
    for i, c in enumerate(world.scenario.get("contingencies") or []):
        if i in aux.fired_contingencies or not isinstance(c, dict):
            continue
        when = _contingency_time(world, c)
        if when is None or world.sim_now < when:
            continue
        aux.fired_contingencies.add(i)
        events.append(fire_contingency(world, float(c.get("mw", 0.0) or 0.0), str(c.get("label") or c.get("type") or "Contingency"),
                                       str(c.get("type") or "generator_trip"), c.get("duration_min")))
    keep = []
    for until, mw, label in aux.contingency_restores:
        if world.sim_now >= until:
            world.contingency_lost_mw = max(world.contingency_lost_mw - mw, 0.0)
            events.append(_event(f"{label}: {mw:,.0f} MW of supply restored at {hhmm(world.sim_now)}",
                                 {"kind": "contingency_restored", "mw": mw, "label": label}, "contingency"))
        else:
            keep.append((until, mw, label))
    aux.contingency_restores = keep
    return events


# ---------------------------------------------------------------- section switching

def set_section_power(world: World, section_idx: int, on: bool, until: datetime | None,
                      source: str | None = None) -> None:
    """Energise or cut one section. `until` set means a timed cut released by its owner later.

    `source` tags who cut it: "world" (uncontrolled trip), "baseline" or "agent" (GridOps). If not
    given it is "world" for a timed cut and "agent" otherwise (GridOps passes until=None).
    """
    s = int(section_idx)
    aux = get_aux(world)
    mask = world.section_idx == s
    world.power_on[mask] = on
    if on:
        world.section_cut[s] = False
        world.section_cut_until[s] = None
        aux.cut_source[s] = None
        return
    if not world.section_cut[s]:
        world.section_cut_count_today[s] += 1
    world.section_cut[s] = True
    world.section_cut_until[s] = until
    aux.cut_source[s] = source or ("world" if until is not None else "agent")
    # devices lose power: chargers and batteries stop
    world.ev_charging[mask] = False
    world.battery_discharging[mask] = False


def cut_source(world: World, section_idx: int) -> str | None:
    return get_aux(world).cut_source[int(section_idx)]


def release_expired_cuts(world: World) -> list[str]:
    """Re-energise world-imposed (uncontrolled trip) cuts whose time is up; returns section ids."""
    aux = get_aux(world)
    out: list[str] = []
    for s, sid in enumerate(world.section_ids):
        until = world.section_cut_until[s]
        if world.section_cut[s] and aux.cut_source[s] == "world" and until is not None and world.sim_now >= until:
            set_section_power(world, s, True, None)
            out.append(sid)
    return out


def check_uncontrolled_trip(world: World) -> dict | None:
    """PLAN §7.6: shortfall > 3% of supply for 3 consecutive ticks trips a random live section
    for 90 minutes, ignoring protection. Call once per tick after aggregate()."""
    if world.supply_mw > 0 and world.shortfall_mw > TRIP_THRESHOLD * world.supply_mw:
        world.over_threshold_ticks += 1
    else:
        world.over_threshold_ticks = 0
        return None
    if world.over_threshold_ticks < TRIP_TICKS:
        return None
    live = np.flatnonzero(~np.asarray(world.section_cut, dtype=bool))
    world.over_threshold_ticks = 0
    if live.size == 0:
        return None
    s = int(world.rng.choice(live))
    shortfall, supply = world.shortfall_mw, world.supply_mw
    mw = float(world.section_demand_mw[s]) if len(world.section_demand_mw) > s else 0.0
    until = world.sim_now + timedelta(minutes=TRIP_MINUTES)
    set_section_power(world, s, False, until, source="world")
    mask = world.section_idx == s
    homes = int(mask.sum())
    vul = int((mask & world.vulnerable).sum())
    world.metrics["uncontrolled_trips"] = int(world.metrics.get("uncontrolled_trips", 0)) + 1
    trips = world.metrics.setdefault("trip_log", [])
    trips.append({"section_id": world.section_ids[s], "at": sim_iso(world.sim_now), "until": sim_iso(until)})
    sid = world.section_ids[s]
    prot = " (a protected section)" if bool(world.section_protected[s]) else ""
    return _event(
        f"Uncontrolled trip: {sid}{prot} lost power for {TRIP_MINUTES} min after {TRIP_TICKS} ticks with the "
        f"shortfall above 3% of supply ({shortfall:,.0f} MW short of {supply:,.0f} MW); "
        f"{real(world, homes):,} homes dark, {real(world, vul):,} vulnerable",
        {"kind": "uncontrolled_trip", "section_id": sid, "protected": bool(world.section_protected[s]),
         "until": sim_iso(until), "duration_min": TRIP_MINUTES, "shortfall_mw": round(shortfall, 1),
         "supply_mw": round(supply, 1), "section_mw": round(mw, 1), "homes_real": real(world, homes),
         "vulnerable_real": real(world, vul)},
    )


def section_states(world: World) -> list[dict[str, Any]]:
    """For the WS "sections" message."""
    aux = get_aux(world)
    return [
        {"id": sid, "cut": bool(world.section_cut[i]), "cut_minutes_today": int(world.section_cut_minutes_today[i]),
         "protected": bool(world.section_protected[i]), "source": aux.cut_source[i],
         "until": sim_iso(world.section_cut_until[i]) if world.section_cut_until[i] else None}
        for i, sid in enumerate(world.section_ids)
    ]
