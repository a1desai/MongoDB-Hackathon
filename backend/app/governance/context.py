"""GateContext: everything the deterministic rules need, gathered from Atlas and the World.

`build_gate_context` does all the I/O (identities, versioned policies, kill switch, latest
forecast, delivered relief, committed devices, SMS budget, evidence trust) so that
`gate.evaluate` stays pure. Every query degrades to a safe default instead of raising.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

from app import db as dbm
from app.governance.identities import load_identities, load_policies
from app.governance.rules.base import ResolvedTargets, params
from app.governance.thermal_guard import Predictor
from app.models import ActionRequest, Identity, Policy
from app.sim.world_state import World
from app.timeutil import at, parse_sim

log = logging.getLogger(__name__)

# Estimation constants for R07's "remaining lower-rung potential" (not policy thresholds).
EV_KW = 7.2  # ChargeFlow session draw (PLAN §5.2)
BATTERY_KW = 5.0  # PowerCell unit discharge (PLAN §5.2)
BATTERY_SOC_FLOOR = 0.2  # PowerCell stops at 20% state of charge (PLAN §7.7)
REALIZATION = {"thermostat_setback": 0.85, "ev_pause": 0.9, "battery_discharge": 1.0}
RELIEF_ACTIONS = ["precool", "thermostat_setback", "ev_pause", "battery_discharge", "business_curtail_request", "send_sms", "feeder_outage"]
PENDING_STATUSES = ["proposed", "needs_approval", "approved", "executing"]


@dataclass
class GateContext:
    world: World
    run: dict
    policies: dict[str, Policy]
    policy_version: str
    identities: dict[str, Identity]
    sim_now: datetime
    halted: bool
    halted_scope: str
    t_safe_c: float
    budget_left_cad: float
    lower_rung_potential_mw: float
    active_delivered_mw: float
    projected_shortfall_mw: float  # gross: net projected shortfall + relief already delivered
    # ---- optional extras (defaults keep hand-built contexts in tests short)
    margin_mw: float | None = None  # projected supply minus demand; None -> world.supply_mw - world.demand_mw
    outdoor_c: float | None = None
    untrusted_evidence_ids: frozenset[str] = frozenset()
    potential_by_rung: dict[str, float] = field(default_factory=dict)
    predictor: Predictor | None = None  # R05 override (tests); default thermal_guard.default_predict
    resolved: ResolvedTargets | None = None  # set by gate.evaluate for the request being decided
    notice_minute: np.ndarray | None = None  # R12: per home, sim-minute a cool-space notice was requested/sent (-1 none)

    @property
    def scale(self) -> float:
        return float(self.run.get("scale") or self.world.scale or 1.0)

    @property
    def net_projected_shortfall_mw(self) -> float:
        return self.projected_shortfall_mw - self.active_delivered_mw

    def margin(self) -> float:
        if self.margin_mw is not None:
            return float(self.margin_mw)
        return float(self.world.supply_mw - self.world.demand_mw)


# ---------------------------------------------------------------- pure estimates


def lower_rung_potential(world: World, policies: dict[str, Policy], committed: np.ndarray | None = None) -> dict[str, float]:
    """Real-scale MW still available *now* from lower rungs, excluding devices already active or
    targeted by a pending request. Conservative on purpose: SMS offers are acceptance-based and
    are not counted; businesses count only once they have accepted and are not yet dispatched."""
    p03 = params(policies, "R03")
    p04 = params(policies, "R04")
    exempt = set(p04.get("exempt_actions", []))
    free = world.power_on & ~world.overridden
    if committed is not None and committed.shape == free.shape:
        free = free & ~committed

    setback = world.has_thermostat & world.consent_thermostat & free & ~world.setback_active & ~world.precool_active
    setback &= world.setback_count_today < int(p03.get("setbacks_per_home_per_day", 1))
    if "thermostat_setback" in exempt:
        setback &= ~world.vulnerable
    safe_k_ac = np.where(world.k_ac > 0, world.k_ac, 1.0)
    per_home_kw = world.ac_kw * np.minimum(world.duty, float(p03.get("offset_max_c", 0.0)) * world.k_env / safe_k_ac)
    setback_kw = float(per_home_kw[setback].sum()) * REALIZATION["thermostat_setback"]

    ev = world.has_ev & world.consent_ev & world.ev_charging & ~world.ev_paused & free
    if "ev_pause" in exempt:
        ev &= ~world.vulnerable
    ev_kw = float(ev.sum()) * EV_KW * REALIZATION["ev_pause"]

    bat = world.has_battery & world.consent_battery & ~world.battery_discharging & (world.battery_soc > BATTERY_SOC_FLOOR) & free
    bat_kw = float(np.where(world.battery_kw > 0, world.battery_kw, BATTERY_KW)[bat].sum()) * REALIZATION["battery_discharge"]

    biz_mw = 0.0
    for b in (world.businesses or {}).values():
        if b.get("status") == "accepted" and not b.get("protected"):
            biz_mw += float(b.get("accepted_mw") or 0.0)

    k = world.scale / 1000.0
    return {
        "battery_discharge": bat_kw * k,
        "ev_pause": ev_kw * k,
        "business_curtail_request": biz_mw,
        "thermostat_setback": setback_kw * k,
    }


# ---------------------------------------------------------------- I/O


async def _safe(coro: Any, default: Any, what: str) -> Any:
    try:
        return await coro
    except Exception as exc:
        log.warning("gate context: %s failed (%s); using default", what, exc)
        return default


async def _control_doc(run_id: str) -> dict | None:
    return await dbm.db()[dbm.AGENT_CONTROL].find_one({"_id": run_id})


async def _latest_forecast(run_id: str) -> dict | None:
    return await dbm.db()[dbm.FORECASTS].find_one({"run_id": run_id}, sort=[("sim_ts", -1)])


async def _active_relief(run_id: str) -> list[dict]:
    """Verified/partial executions of relief actions, joined to their request ($lookup)."""
    pipeline = [
        {"$match": {"run_id": run_id, "status": {"$in": ["verified", "partial"]}}},
        {
            "$lookup": {
                "from": dbm.ACTION_REQUESTS,
                "localField": "request_id",
                "foreignField": "_id",
                "as": "req",
                "pipeline": [{"$project": {"action_type": 1, "window": 1, "status": 1}}],
            }
        },
        {"$unwind": "$req"},
        {"$match": {"req.action_type": {"$in": RELIEF_ACTIONS}, "req.status": {"$nin": ["rolled_back", "cancelled"]}}},
        {"$project": {"_id": 0, "delivered_mw": "$verification.delivered_mw", "window": "$req.window"}},
    ]
    cur = await dbm.db()[dbm.EXECUTIONS].aggregate(pipeline)
    return await cur.to_list(None)


async def _committed_households(run_id: str, exclude_id: str | None) -> list[str]:
    q: dict[str, Any] = {
        "run_id": run_id,
        "status": {"$in": PENDING_STATUSES},
        "action_type": {"$in": ["thermostat_setback", "ev_pause", "battery_discharge"]},
    }
    if exclude_id:
        q["_id"] = {"$ne": exclude_id}
    out: list[str] = []
    async for doc in dbm.db()[dbm.ACTION_REQUESTS].find(q, {"targets.household_ids": 1}):
        out.extend((doc.get("targets") or {}).get("household_ids") or [])
    return out


async def _budget_spent(run_id: str) -> float:
    pipeline = [
        {"$match": {"run_id": run_id, "direction": "out", "offer.amount_cad": {"$gt": 0}}},
        {"$group": {"_id": None, "spent": {"$sum": "$offer.amount_cad"}}},
    ]
    cur = await dbm.db()[dbm.MESSAGES].aggregate(pipeline)
    rows = await cur.to_list(1)
    return float(rows[0]["spent"]) if rows else 0.0


async def _untrusted_ids(run_id: str, ids: list[str]) -> set[str]:
    """Evidence ids that the messages collection marks untrusted (the proposer's label is not trusted)."""
    if not ids:
        return set()
    q = {"_id": {"$in": ids}, "$or": [{"trust": "untrusted"}, {"direction": "in"}]}
    return {d["_id"] async for d in dbm.db()[dbm.MESSAGES].find(q, {"_id": 1})}


NOTICE_KINDS = ["cool_space_notice"]
DEAD_STATUSES = ["denied", "cancelled", "failed", "rolled_back"]


async def _cool_space_notices(run_id: str) -> list[tuple[str, str]]:
    """(household id, sim_ts) for cool-space notices that were sent or requested and not denied."""
    out: list[tuple[str, str]] = []
    q = {
        "run_id": run_id,
        "action_type": "send_sms",
        "status": {"$nin": DEAD_STATUSES},
        "$or": [{"params.message_kind": {"$in": NOTICE_KINDS}}, {"params.offer_type": {"$in": NOTICE_KINDS}}],
    }
    async for d in dbm.db()[dbm.ACTION_REQUESTS].find(q, {"targets.recipient_ids": 1, "messages.to": 1, "created_sim_ts": 1}):
        ts = d.get("created_sim_ts") or ""
        recips = (d.get("targets") or {}).get("recipient_ids") or [m.get("to") for m in d.get("messages") or []]
        out.extend((r, ts) for r in recips if isinstance(r, str))
    mq = {"run_id": run_id, "direction": "out", "offer.type": {"$in": NOTICE_KINDS}}
    async for m in dbm.db()[dbm.MESSAGES].find(mq, {"to": 1, "sim_ts": 1}):
        if isinstance(m.get("to"), str):
            out.append((m["to"], m.get("sim_ts") or ""))
    return out


def notice_minutes(world: World, notices: list[tuple[str, str]]) -> np.ndarray:
    """Earliest notice minute (since sim_start) per home, -1 where none."""
    arr = np.full(world.n, -1, dtype=np.int64)
    for hh, ts in notices:
        try:
            i = world.hh_index(hh)
            m = int((parse_sim(ts) - world.sim_start).total_seconds() // 60)
        except Exception:
            continue
        if 0 <= i < world.n and (arr[i] < 0 or m < arr[i]):
            arr[i] = m
    return arr


def _outdoor_now(world: World) -> float | None:
    try:
        from app.sim.weather import outdoor_c

        return float(outdoor_c(world, world.sim_now))
    except Exception:
        return None


def _forecast_net_peak(forecast: dict | None, now: datetime) -> float | None:
    if not forecast:
        return None
    peaks: list[float] = []
    for w in forecast.get("windows") or []:
        try:
            if at(now, w["end"]) > now:
                peaks.append(float(w.get("peak_short_mw") or 0.0))
        except Exception:
            continue
    return max(peaks) if peaks else None


def _still_active(window: dict | None, now: datetime) -> bool:
    if not window or not window.get("end"):
        return True
    try:
        return at(now, window["end"]) > now
    except Exception:
        return True


async def build_gate_context(world: World, run: dict, request: ActionRequest | None = None) -> GateContext:
    """Gather the Gate's inputs. Pass `request` to resolve its evidence trust and exclude it from
    the committed-device set."""
    run_id = world.run_id
    ev_ids = [e.id for e in request.evidence] if request else []
    (identities, (policies, version), control, forecast, relief, committed_ids, spent, untrusted, notices) = await asyncio.gather(
        _safe(load_identities(), {}, "identities"),
        _safe(load_policies(), ({}, "unversioned"), "policies"),
        _safe(_control_doc(run_id), None, "agent_control"),
        _safe(_latest_forecast(run_id), None, "forecast"),
        _safe(_active_relief(run_id), [], "active relief"),
        _safe(_committed_households(run_id, request.id if request else None), [], "committed devices"),
        _safe(_budget_spent(run_id), 0.0, "sms budget"),
        _safe(_untrusted_ids(run_id, ev_ids), set(), "evidence trust"),
        _safe(_cool_space_notices(run_id), [], "cool-space notices"),
    )
    now = world.sim_now

    halted = bool(world.halted)
    scope = world.halted_scope or "all"
    if control and control.get("halted"):
        halted, scope = True, control.get("scope") or "all"

    p05 = params(policies, "R05")
    t_safe = float(world.scenario.get("t_safe_c") or p05.get("t_safe_c") or 29.0)

    budget = world.scenario.get("budget_cad", run.get("budget_cad"))
    budget_left = float("inf") if budget is None else float(budget) - spent

    committed = np.zeros(world.n, dtype=bool)
    for h in committed_ids:
        try:
            i = world.hh_index(h)
            if 0 <= i < world.n:
                committed[i] = True
        except Exception:
            continue
    potential = lower_rung_potential(world, policies, committed)

    active = sum(float(r.get("delivered_mw") or 0.0) for r in relief if _still_active(r.get("window"), now))
    net = float(world.shortfall_mw)
    fc_peak = _forecast_net_peak(forecast, now)
    if fc_peak is not None:
        net = max(net, fc_peak)

    return GateContext(
        world=world,
        run=run,
        policies=policies,
        policy_version=version,
        identities=identities,
        sim_now=now,
        halted=halted,
        halted_scope=scope,
        t_safe_c=t_safe,
        budget_left_cad=budget_left,
        lower_rung_potential_mw=float(sum(potential.values())),
        active_delivered_mw=active,
        projected_shortfall_mw=net + active,
        margin_mw=-net,
        outdoor_c=_outdoor_now(world),
        untrusted_evidence_ids=frozenset(untrusted),
        potential_by_rung=potential,
        notice_minute=notice_minutes(world, notices),
    )
