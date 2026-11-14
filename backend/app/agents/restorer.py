"""Restorer (PLAN §8.2, code).

- Thermostats: 30 minutes before a setback window ends, or as soon as the shortfall clears,
  proposes thermostat_restore in 4 staggered groups 10 minutes apart (hottest homes first) so
  the release does not cause a rebound peak.
- Feeders: when an agent-imposed outage reaches its duration, proposes feeder_restore.
Rule R16 (RESTORE_SAFETY) judges both. Proposals are stored and logged as request_proposed;
the engine (or Governance.planning_cycle) sends each through the request graph.
"""

from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

import numpy as np

from ..db import ACTION_REQUESTS, DECISIONS, db
from ..ids import new_id
from ..models import ACTION_SYSTEM, ActionRequest, Evidence, Targets, Window
from ..sim.world_state import World
from ..timeutil import hhmm, parse_sim, sim_iso
from .common import ACTING_FOR, MAX_BATCH, ceil_tick, kw_per_c, outdoor_at, plain, r1, to_mw

log = logging.getLogger("cooldown.agents.restorer")

AGENT_ID = "agent:restorer"
STAGGER_GROUPS = 4  # R16 params
STAGGER_MIN = 10
LEAD = timedelta(minutes=STAGGER_GROUPS * STAGGER_MIN - STAGGER_MIN)  # last group lands at window end
CLEAR_MARGIN = 0.97  # "shortfall cleared": demand below 97% of supply
MIN_ACTIVE = timedelta(minutes=30)

_done: dict[str, set[str]] = {}  # run_id -> request ids already handled


def _request(world: World, action: str, *, summary: str, targets: Targets, params: dict[str, Any],
             window: Window, evidence: list[Evidence], mw_added: float) -> ActionRequest:
    rid = new_id("ar_", world.run_id)
    now = sim_iso(world.sim_now)
    return ActionRequest(
        id=rid, run_id=world.run_id, created_sim_ts=now, sim_ts=now, action_type=action,  # type: ignore[arg-type]
        system=ACTION_SYSTEM[action], rung=None, proposed_by=AGENT_ID, acting_for=ACTING_FOR,  # type: ignore[arg-type]
        summary=summary[:300], targets=targets, params=plain({**params, "mw_added": r1(mw_added)}), window=window,
        evidence=evidence, expected_mw=r1(mw_added), status="proposed", idempotency_key=rid,
    )


async def _thermostat_restores(world: World, done: set[str]) -> list[ActionRequest]:
    cur = db()[ACTION_REQUESTS].find({
        "run_id": world.run_id, "action_type": "thermostat_setback",
        "status": {"$in": ["executing", "verified", "partial"]},
    })
    setbacks = [r async for r in cur if r["_id"] not in done]
    if not setbacks:
        return []
    cleared = world.supply_mw > 0 and world.demand_mw < CLEAR_MARGIN * world.supply_mw
    active = world.setback_active.astype(bool)
    t_out = outdoor_at(world, world.sim_now)
    out: list[ActionRequest] = []
    for req in setbacks:
        params = req.get("params") or {}
        try:
            start, end = parse_sim(params["start"]), parse_sim(params["end"])
        except (KeyError, ValueError):
            continue
        near_end = world.sim_now >= end - LEAD - timedelta(minutes=5)
        early = cleared and world.sim_now - start >= MIN_ACTIVE
        if not (near_end or early):
            continue
        done.add(req["_id"])
        allowed = None
        if req.get("decision_id"):
            dec = await db()[DECISIONS].find_one({"_id": req["decision_id"]}, {"allowed_household_ids": 1})
            allowed = (dec or {}).get("allowed_household_ids") or None
        hh = allowed or (req.get("targets") or {}).get("household_ids", [])
        idx = np.array([world.hh_index(h) for h in hh if 0 <= world.hh_index(h) < world.n], dtype=int)
        idx = idx[active[idx]] if idx.size else idx
        if idx.size == 0:
            continue
        idx = idx[np.argsort(-world.t_in[idx], kind="stable")]  # hottest homes come back first
        groups = [g for g in np.array_split(idx, STAGGER_GROUPS) if g.size]
        first = ceil_tick(world, world.sim_now)
        why = "shortfall cleared" if early and not near_end else f"setback ends {hhmm(end)}"
        ev = [Evidence(id=req["_id"], kind="request", trust="system")]
        for g_i, g in enumerate(groups):
            at_t = first + timedelta(minutes=STAGGER_MIN * g_i)
            mw = to_mw(kw_per_c(world, t_out)[g] * float(params.get("offset_c", 2.0)), world.scale)
            for b in range(0, g.size, MAX_BATCH):
                part = g[b:b + MAX_BATCH]
                out.append(_request(
                    world, "thermostat_restore",
                    summary=f"Restore {int(part.size * world.scale):,} thermostats at {hhmm(at_t)} "
                            f"(group {g_i + 1}/{len(groups)}, {why}), +{mw:.0f} MW",
                    targets=Targets(household_ids=[world.hh_ids[int(k)] for k in part]),
                    params={"start": sim_iso(at_t), "offset_c": 0.0, "stagger_group": g_i + 1,
                            "stagger_groups": len(groups), "stagger_min": STAGGER_MIN,
                            "restores_request_id": req["_id"]},
                    window=Window(start=hhmm(at_t), end=hhmm(at_t + timedelta(minutes=STAGGER_MIN))),
                    evidence=ev, mw_added=mw,
                ))
    return out


async def _feeder_restores(world: World, done: set[str]) -> list[ActionRequest]:
    cur = db()[ACTION_REQUESTS].find({
        "run_id": world.run_id, "action_type": "feeder_outage",
        "status": {"$in": ["executing", "verified", "partial"]},
    })
    out: list[ActionRequest] = []
    async for req in cur:
        if req["_id"] in done:
            continue
        s_id = (req.get("targets") or {}).get("section_id")
        if s_id not in world.section_ids:
            continue
        i = world.section_index(s_id)
        params = req.get("params") or {}
        until = world.section_cut_until[i] if i < len(world.section_cut_until) else None
        if until is None and params.get("start"):
            until = parse_sim(params["start"]) + timedelta(minutes=int(params.get("duration_min", 60)))
        if until is None or world.sim_now < until - timedelta(minutes=5):
            continue
        done.add(req["_id"])
        if not bool(world.section_cut[i]):
            continue  # already back (e.g. rolled back)
        mw = float(world.section_demand_mw[i]) if len(world.section_demand_mw) > i else float(req.get("expected_mw", 0))
        if mw <= 0:
            mw = float(req.get("expected_mw", 0))
        at_t = ceil_tick(world, world.sim_now)
        out.append(_request(
            world, "feeder_restore",
            summary=f"Close breaker {s_id}: restore {int(world.section_households[i] * world.scale):,} homes "
                    f"after {params.get('duration_min', 60)} min, +{mw:.0f} MW",
            targets=Targets(section_id=s_id),
            params={"start": sim_iso(at_t), "restores_request_id": req["_id"],
                    "approval_id": req.get("approval_id")},
            window=Window(start=hhmm(at_t), end=hhmm(at_t + timedelta(minutes=5))),
            evidence=[Evidence(id=req["_id"], kind="request", trust="system")], mw_added=mw,
        ))
    return out


async def propose(world: World) -> list[ActionRequest]:
    """Staggered thermostat restores and feeder restores due now; stored + request_proposed."""
    from ..governance.ledger import append

    done = _done.setdefault(world.run_id, set())
    proposals = await _thermostat_restores(world, done) + await _feeder_restores(world, done)
    for req in proposals:
        await db()[ACTION_REQUESTS].insert_one(req.to_mongo())
        await append(
            world.run_id, "request_proposed", AGENT_ID, req.summary, sim_ts=req.created_sim_ts,
            acting_for=ACTING_FOR, refs={"request_id": req.id},
            payload=plain({"action_type": req.action_type, "system": req.system, "params": req.params,
                           "targets_count": len(req.targets.household_ids) or 1,
                           "section_id": req.targets.section_id, "expected_mw": req.expected_mw,
                           "evidence": [e.model_dump() for e in req.evidence]}),
        )
    return proposals
