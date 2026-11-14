"""Watcher (PLAN §8.2, code, every tick).

Compares verified delivered MW with what the active plan steps expected, watches for new
shortfall and contingencies, flags uncontrolled-trip risk, expires approvals, and logs
replan_triggered. It returns replan reasons; the engine passes the first one to
Governance.planning_cycle(trigger), which rate-limits replans (once per 15 sim minutes,
except right after a contingency).

`state` (optional, engine-owned) may carry "plan" (Plan | dict) and "forecast" (Forecast | dict);
anything missing is read from Atlas. The Watcher's own memory is kept per run in this module.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from ..db import ACTION_REQUESTS, DECISIONS, EXECUTIONS, FORECASTS, PLANS, db
from ..sim.world_state import World
from ..timeutil import hhmm, parse_sim, sim_iso
from .common import ACTING_FOR, plain, r1

log = logging.getLogger("cooldown.agents.watcher")

AGENT_ID = "agent:watcher"
DELIVERY_RATIO = 0.85
MIN_DEFICIT_MW = 10.0  # ignore under-delivery smaller than this (a few offline devices are not worth a replan)
NEW_SHORTFALL_MW = 50.0
TRIP_FRACTION = 0.03  # PLAN §7.6: shortfall above 3% of supply for 3 ticks trips a section
HOURLY = timedelta(minutes=60)
REPEAT_COOLDOWN = timedelta(minutes=15)
VERIFY_GRACE = timedelta(minutes=15)  # 3 ticks for the Verifier to settle before judging delivery

_memo: dict[str, dict[str, Any]] = {}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    if obj is None:
        return default
    if isinstance(obj, dict):
        return obj.get(key, obj.get("_id") if key == "id" else default)
    return getattr(obj, key, default)


async def _latest(coll: str, run_id: str, sort_field: str) -> dict | None:
    return await db()[coll].find_one({"run_id": run_id}, sort=[(sort_field, -1)])


async def delivery(world: World, plan_id: str) -> tuple[float, float]:
    """(expected, delivered) MW for executed requests of a plan whose window covers now.

    Expected is scaled by the Gate's allowed share, so partial_allow batches are judged on what
    was actually allowed. One aggregation: action_requests $lookup decisions + executions.
    """
    now = sim_iso(world.sim_now)
    grace = sim_iso(world.sim_now - VERIFY_GRACE)
    pipeline = [
        {"$match": {"run_id": world.run_id, "plan_id": plan_id,
                    "status": {"$in": ["executing", "verified", "partial", "failed"]},
                    "action_type": {"$nin": ["send_sms", "feeder_restore", "thermostat_restore", "undo"]}}},
        {"$lookup": {"from": EXECUTIONS, "localField": "execution_id", "foreignField": "_id", "as": "exe"}},
        {"$lookup": {"from": DECISIONS, "localField": "decision_id", "foreignField": "_id", "as": "dec"}},
        {"$unwind": "$exe"},
        {"$match": {"exe.sent_sim_ts": {"$lte": grace}, "params.start": {"$lte": now}, "params.end": {"$gt": now}}},
        {"$unwind": {"path": "$dec", "preserveNullAndEmptyArrays": True}},
        {"$project": {
            "delivered": {"$ifNull": ["$exe.verification.delivered_mw", 0]},
            "expected": {"$multiply": ["$expected_mw", {"$cond": [
                {"$gt": [{"$add": [{"$ifNull": ["$dec.allowed_count", 0]}, {"$ifNull": ["$dec.denied_count", 0]}]}, 0]},
                {"$divide": [{"$ifNull": ["$dec.allowed_count", 0]},
                             {"$add": [{"$ifNull": ["$dec.allowed_count", 0]}, {"$ifNull": ["$dec.denied_count", 0]}]}]},
                1]}]},
        }},
        {"$group": {"_id": None, "expected": {"$sum": "$expected"}, "delivered": {"$sum": "$delivered"}}},
    ]
    async for row in await db()[ACTION_REQUESTS].aggregate(pipeline):
        return float(row["expected"]), float(row["delivered"])
    return 0.0, 0.0


ROTATION_LEAD = timedelta(minutes=25)  # R12 notice (15) + switching (5) + one tick of slack


async def _outages_ending(world: World) -> list[tuple[str, datetime]]:
    """Agent outages in effect whose planned end is within ROTATION_LEAD."""
    out: list[tuple[str, datetime]] = []
    try:
        now, horizon = sim_iso(world.sim_now), sim_iso(world.sim_now + ROTATION_LEAD)
        cur = db()[ACTION_REQUESTS].find(
            {"run_id": world.run_id, "action_type": "feeder_outage", "status": {"$in": ["executing", "verified", "partial"]},
             "params.end": {"$gt": now, "$lte": horizon}},
            {"targets.section_id": 1, "params.end": 1}).sort("params.end", 1)
        async for d in cur:
            out.append((str((d.get("targets") or {}).get("section_id")), parse_sim(d["params"]["end"])))
    except Exception as exc:  # noqa: BLE001
        log.debug("ending outages lookup failed: %s", exc)
    return out


async def check(world: World, state: dict) -> list[str]:
    """Replan reasons (possibly empty); expires approvals; logs replan_triggered."""
    from ..governance.ledger import append

    state = state if isinstance(state, dict) else {}
    memo = _memo.setdefault(world.run_id, {"contingency_mw": 0.0, "last": {}})
    reasons: list[tuple[str, str]] = []  # (code, human detail)

    # 1. approvals past their 10-minute window are auto-denied (the change stream resumes their graphs)
    try:
        from ..governance.approvals import expire_due

        expired = await expire_due(world.run_id, world.sim_now)
        if expired:
            log.info("expired %d approvals", len(expired))
    except Exception as exc:  # noqa: BLE001
        log.debug("expire_due unavailable: %s", exc)

    plan = state.get("plan") or await _latest(PLANS, world.run_id, "version")
    forecast = state.get("forecast") or await _latest(FORECASTS, world.run_id, "sim_ts")
    windows = _get(forecast, "windows", []) or []
    fc_peak = max((float(_get(w, "peak_short_mw", 0)) for w in windows), default=0.0)

    # 2. contingency (supply lost since we last looked)
    if world.contingency_lost_mw > memo["contingency_mw"] + 1e-6:
        lost = world.contingency_lost_mw - memo["contingency_mw"]
        reasons.append(("contingency", f"contingency: {lost:.0f} MW of supply lost"))
    memo["contingency_mw"] = world.contingency_lost_mw

    # 3. uncontrolled-trip risk: shortfall above 3% of supply right now
    if world.supply_mw > 0 and world.shortfall_mw > TRIP_FRACTION * world.supply_mw:
        reasons.append(("trip_risk", f"trip risk: shortfall {world.shortfall_mw:.0f} MW is above 3% of supply "
                                     f"({world.over_threshold_ticks} consecutive ticks)"))

    # 7. rotation: an agent outage ends soon while the forecast still shows a shortfall, so the next
    #    section must be notified now (R12) to take over when this one is restored
    ending = await _outages_ending(world)
    if ending and fc_peak > 0:
        sid, end = ending[0]
        reasons.append(("rotation_due", f"outage in {sid} ends {hhmm(end)} with a shortfall still forecast; "
                                        f"line up the next rotation"))

    if plan is None:
        if fc_peak > 0:
            reasons.append(("first_shortfall", f"first forecast shortfall: {fc_peak:.0f} MW"))
    else:
        plan_ts = parse_sim(_get(plan, "sim_ts"))
        planned_for = float(_get(plan, "expected_shortfall_mw", 0) or 0)
        # 4. a new shortfall beyond what the plan was built for
        current = max(fc_peak, world.shortfall_mw)
        if current - planned_for > NEW_SHORTFALL_MW:
            reasons.append(("new_shortfall", f"new shortfall {current:.0f} MW vs {planned_for:.0f} MW planned"))
        # 5. delivered below 85% of expected
        expected, delivered = await delivery(world, _get(plan, "id") or _get(plan, "_id"))
        if expected > 1.0 and delivered < DELIVERY_RATIO * expected and expected - delivered >= MIN_DEFICIT_MW:
            reasons.append(("under_delivery", f"delivered {delivered:.0f} MW is below 85% of {expected:.0f} MW expected"))
        # 6. hourly replan while a shortfall persists
        if (fc_peak > 0 or world.shortfall_mw > 0) and world.sim_now - plan_ts >= HOURLY:
            reasons.append(("hourly", f"shortfall persists {hhmm(plan_ts)}-{hhmm(world.sim_now)}"))

    # de-duplicate: each reason at most once per 15 sim minutes (contingency always passes)
    fresh: list[tuple[str, str]] = []
    for code, detail in reasons:
        last: datetime | None = memo["last"].get(code)
        if code != "contingency" and last is not None and world.sim_now - last < REPEAT_COOLDOWN:
            continue
        memo["last"][code] = world.sim_now
        fresh.append((code, detail))
    if not fresh:
        return []

    order = ["contingency", "trip_risk", "rotation_due", "first_shortfall", "new_shortfall", "under_delivery", "hourly"]
    fresh.sort(key=lambda r: order.index(r[0]))
    await append(
        world.run_id, "replan_triggered", AGENT_ID, "Replan: " + "; ".join(d for _, d in fresh),
        sim_ts=sim_iso(world.sim_now), acting_for=ACTING_FOR,
        refs={"plan_version": _get(plan, "version")} if plan is not None else None,
        payload=plain({"reasons": [c for c, _ in fresh], "details": [d for _, d in fresh],
                       "demand_mw": r1(world.demand_mw), "supply_mw": r1(world.supply_mw),
                       "shortfall_mw": r1(world.shortfall_mw), "forecast_peak_short_mw": r1(fc_peak),
                       "over_threshold_ticks": world.over_threshold_ticks}),
    )
    return [c for c, _ in fresh]
