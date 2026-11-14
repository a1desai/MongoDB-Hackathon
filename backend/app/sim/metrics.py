"""Run metrics (PLAN §7.9), always reported at real scale (simulated count x world.scale),
and the compact map encoding sent every 3 ticks.

Per-home minute counters (outage_minutes, over_t_safe_minutes) and section cut minutes are
accumulated by physics.world_step; `update` tracks grid-level peaks. `summary` is pure (World
only); `summary_with_db` adds gate denials, approvals and delivered MW from Atlas.
"""

import base64
import logging
from typing import Any

import numpy as np

from ..models import RUNGS
from ..timeutil import at, hhmm, sim_iso
from .world_state import World

log = logging.getLogger("cooldown.sim.metrics")

REBOUND_FROM = "20:00"  # baseline's simultaneous restore; rebound_peak_mw is the max demand from here on

FLAG_POWER_OFF = 1
FLAG_OVERRIDDEN = 2
FLAG_PRECOOL = 4
FLAG_SETBACK = 8
FLAG_VULNERABLE = 16


def _m(world: World) -> dict[str, Any]:
    return world.metrics


def record_voluntary(world: World, action_type: str, mw: float, key: str | None = None) -> None:
    """Record delivered MW for a voluntary rung. With `key` (e.g. an execution id) the latest value
    for that key replaces the previous one; without it the MW is added to the rung total."""
    m = _m(world)
    if key:
        m.setdefault("voluntary_by_key", {})[key] = [action_type, float(mw)]
    else:
        tot = m.setdefault("voluntary_mw_totals", {})
        tot[action_type] = float(tot.get(action_type, 0.0)) + float(mw)


def record_gate_decision(world: World, verdict: str, denied_reasons: dict[str, int] | None = None) -> None:
    m = _m(world)
    v = m.setdefault("verdicts", {})
    v[verdict] = int(v.get(verdict, 0)) + 1
    d = m.setdefault("gate_denials", {})
    for rule, count in (denied_reasons or {}).items():
        d[rule] = int(d.get(rule, 0)) + int(count)


def record_approval(world: World, status: str) -> None:
    a = _m(world).setdefault("approvals", {})
    a[status] = int(a.get(status, 0)) + 1


def update(world: World, dt_h: float) -> None:
    """Track grid-level peaks for this tick; call after grid.aggregate()."""
    m = _m(world)
    now = world.sim_now
    m["ticks"] = int(m.get("ticks", 0)) + 1
    if world.shortfall_mw > float(m.get("peak_shortfall_mw", 0.0)):
        m["peak_shortfall_mw"] = float(world.shortfall_mw)
        m["peak_shortfall_at"] = hhmm(now)
    if world.demand_mw > float(m.get("peak_demand_mw", 0.0)):
        m["peak_demand_mw"] = float(world.demand_mw)
        m["peak_demand_at"] = hhmm(now)
    m["shortfall_mwh"] = float(m.get("shortfall_mwh", 0.0)) + world.shortfall_mw * dt_h
    margin = world.supply_mw - world.demand_mw
    m["min_margin_mw"] = min(float(m.get("min_margin_mw", margin)), margin)
    if now >= at(now, REBOUND_FROM) and world.demand_mw > float(m.get("rebound_peak_mw", 0.0)):
        m["rebound_peak_mw"] = float(world.demand_mw)
        m["rebound_peak_at"] = hhmm(now)
        m["rebound_over_supply_mw"] = max(world.demand_mw - world.supply_mw, 0.0)
    if world.vulnerable.any():
        mx = float(world.t_in[world.vulnerable].max())
        m["max_t_in_vulnerable_c"] = max(float(m.get("max_t_in_vulnerable_c", mx)), mx)
    m["max_sections_cut"] = max(int(m.get("max_sections_cut", 0)), int(np.count_nonzero(world.section_cut)))
    m["last_sim_ts"] = sim_iso(now)


def _quintile_minutes(world: World) -> tuple[dict[str, float], dict[str, int]]:
    """(mean outage minutes per home by quintile, section outage minutes by dominant quintile)."""
    per_home: dict[str, float] = {}
    per_section: dict[str, int] = {}
    for q in range(1, 6):
        m = world.income_quintile == q
        if m.any():
            per_home[f"Q{q}"] = round(float(world.outage_minutes[m].mean()), 1)
        sm = world.section_quintile == q
        if sm.any():
            per_section[f"Q{q}"] = int(world.section_cut_minutes_today[sm].sum())
    return per_home, per_section


def voluntary_by_rung(world: World) -> dict[str, float]:
    m = _m(world)
    out: dict[str, float] = {k: float(v) for k, v in (m.get("voluntary_mw_totals") or {}).items()}
    for action, mw in (m.get("voluntary_by_key") or {}).values():
        out[action] = out.get(action, 0.0) + float(mw)
    return {k: round(v, 1) for k, v in sorted(out.items(), key=lambda kv: RUNGS.get(kv[0], 99))
            if k != "feeder_outage"}


def summary(world: World) -> dict:
    """PLAN §7.9 metrics at real scale."""
    m = _m(world)
    s = float(world.scale)
    vul = world.vulnerable
    per_home_q, per_section_q = _quintile_minutes(world)
    gap = (max(per_home_q.values()) - min(per_home_q.values())) if per_home_q else 0.0
    sec_gap = (max(per_section_q.values()) - min(per_section_q.values())) if per_section_q else 0
    vol = voluntary_by_rung(world)
    return {
        "run_id": world.run_id,
        "mode": world.mode,
        "sim_ts": sim_iso(world.sim_now),
        "scale": round(s, 3),
        "homes_real": int(round(world.n * s)),
        "uncontrolled_trips": int(m.get("uncontrolled_trips", 0)),
        "outage_home_minutes": int(round(float(world.outage_minutes.sum()) * s)),
        "outage_homes_real": int(round(float((world.outage_minutes > 0).sum()) * s)),
        "outage_home_minutes_vulnerable": int(round(float(world.outage_minutes[vul].sum()) * s)),
        "home_minutes_over_t_safe": int(round(float(world.over_t_safe_minutes.sum()) * s)),
        "home_minutes_over_t_safe_vulnerable": int(round(float(world.over_t_safe_minutes[vul].sum()) * s)),
        "outage_minutes_by_quintile": per_home_q,  # mean minutes without power per home
        "quintile_outage_gap_min": round(gap, 1),
        "section_outage_minutes_by_quintile": per_section_q,  # as R11 counts them
        "section_quintile_gap_min": int(sec_gap),
        "sections_cut_today": int(np.count_nonzero(world.section_cut_count_today)),
        "section_cut_minutes_total": int(world.section_cut_minutes_today.sum()),
        "peak_shortfall_mw": round(float(m.get("peak_shortfall_mw", 0.0)), 1),
        "peak_shortfall_at": m.get("peak_shortfall_at"),
        "peak_demand_mw": round(float(m.get("peak_demand_mw", 0.0)), 1),
        "shortfall_mwh": round(float(m.get("shortfall_mwh", 0.0)), 1),
        "rebound_peak_mw": round(float(m.get("rebound_peak_mw", 0.0)), 1),
        "rebound_peak_at": m.get("rebound_peak_at"),
        "rebound_over_supply_mw": round(float(m.get("rebound_over_supply_mw", 0.0)), 1),
        "voluntary_mw_delivered": vol,
        "voluntary_mw_total": round(sum(vol.values()), 1),
        "gate_denials": dict(m.get("gate_denials") or {}),
        "gate_verdicts": dict(m.get("verdicts") or {}),
        "approvals": dict(m.get("approvals") or {}),
        "overridden_homes_real": int(round(float(world.overridden.sum()) * s)),
        "max_t_in_vulnerable_c": round(float(m.get("max_t_in_vulnerable_c", 0.0)), 2),
        "contingencies": int(m.get("contingencies", 0)),
    }


async def summary_with_db(world: World) -> dict:
    """summary() plus gate denials, verdicts, approvals and delivered MW by rung read from Atlas."""
    out = summary(world)
    try:
        from ..db import ACTION_REQUESTS, APPROVALS, DECISIONS, EXECUTIONS, db

        d = db()
        rid = world.run_id
        denials: dict[str, int] = {}
        verdicts: dict[str, int] = {}
        async for dec in d[DECISIONS].find({"run_id": rid}, {"verdict": 1, "denied_reasons": 1, "rule_results": 1}):
            v = str(dec.get("verdict"))
            verdicts[v] = verdicts.get(v, 0) + 1
            reasons = dec.get("denied_reasons") or {}
            if not reasons and v == "deny":
                reasons = {r["rule_id"]: 1 for r in dec.get("rule_results", []) if r.get("applies") and not r.get("passed")
                           and r.get("effect") == "deny"}
            for rule, n in reasons.items():
                denials[rule] = denials.get(rule, 0) + int(n)
        approvals: dict[str, int] = {}
        async for a in d[APPROVALS].find({"run_id": rid}, {"status": 1}):
            approvals[str(a.get("status"))] = approvals.get(str(a.get("status")), 0) + 1
        req_type = {r["_id"]: r.get("action_type") async for r in d[ACTION_REQUESTS].find({"run_id": rid}, {"action_type": 1})}
        delivered: dict[str, float] = {}
        async for e in d[EXECUTIONS].find({"run_id": rid, "status": {"$in": ["verified", "partial"]}},
                                          {"request_id": 1, "verification": 1}):
            at_ = req_type.get(e.get("request_id"))
            if not at_ or at_ in ("feeder_outage", "feeder_restore", "thermostat_restore", "undo"):
                continue
            delivered[at_] = delivered.get(at_, 0.0) + float((e.get("verification") or {}).get("delivered_mw") or 0.0)
        if verdicts:
            out["gate_denials"], out["gate_verdicts"] = denials, verdicts
        if approvals:
            out["approvals"] = approvals
        if delivered:
            vol = {k: round(v, 1) for k, v in sorted(delivered.items(), key=lambda kv: RUNGS.get(kv[0], 99))}
            out["voluntary_mw_delivered"] = vol
            out["voluntary_mw_total"] = round(sum(vol.values()), 1)
    except Exception as exc:  # noqa: BLE001 - metrics must never fail the end of a run
        log.warning("summary_with_db: using in-memory metrics only (%s)", exc)
    return out


def encode_map(world: World) -> dict:
    """{"temps": b64 uint8 round(T_in*4), "flags": b64 uint8 bitfield}, one byte per home in
    household order. Bits: 0 power_off, 1 overridden, 2 precool, 3 setback, 4 vulnerable."""
    temps = np.clip(np.rint(world.t_in * 4.0), 0, 255).astype(np.uint8)
    flags = (
        (~world.power_on).astype(np.uint8) * FLAG_POWER_OFF
        | world.overridden.astype(np.uint8) * FLAG_OVERRIDDEN
        | world.precool_active.astype(np.uint8) * FLAG_PRECOOL
        | world.setback_active.astype(np.uint8) * FLAG_SETBACK
        | world.vulnerable.astype(np.uint8) * FLAG_VULNERABLE
    ).astype(np.uint8)
    return {
        "sim_ts": sim_iso(world.sim_now),
        "temps": base64.b64encode(temps.tobytes()).decode("ascii"),
        "flags": base64.b64encode(flags.tobytes()).decode("ascii"),
    }
