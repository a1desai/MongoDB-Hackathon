"""Targeter (PLAN §8.2, code): turns plan steps into action_requests.

Device steps resolve the selector to households that have the device and are enrolled in the
program, rank them by kW saved / comfort cost (sim/thermal.py), and batch them (<= 2,000 per
request, one system per request). Outages become one request per section, preceded by a
cool-space pre-notification (R12). It deliberately does NOT pre-filter policy violations
(vulnerable homes, safety limits, SMS opt-outs, daily caps): the Policy Gate must visibly
catch them. It only skips homes that physically cannot respond (power off, already in that state).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from ..db import ACTION_REQUESTS, FACILITIES, SECTIONS, db
from ..ids import new_id
from ..models import (
    ACTION_SYSTEM,
    RUNGS,
    ActionRequest,
    Evidence,
    OutboundMessage,
    Plan,
    PlanStep,
    Targets,
    Window,
)
from ..sim.world_state import World
from ..timeutil import hhmm, sim_iso
from .common import (
    ACTING_FOR,
    BATTERY_KW,
    EV_KW,
    MAX_BATCH,
    SMS_DUTY_CUT,
    ac_present,
    ceil_tick,
    duty_at,
    kw_saved,
    outdoor_at,
    plain,
    predicted_max_tin,
    r1,
    resolve_evidence,
    to_mw,
    window_dt,
)
from .fallback_planner import NOTICE_MIN, SMS_AMOUNT_CAD

log = logging.getLogger("cooldown.agents.targeter")

AGENT_ID = "agent:targeter"
NEGOTIATOR_ID = "agent:negotiator"  # send_sms / business_curtail_request are the Negotiator's scope
STOP_SUFFIX = "Reply STOP to opt out"
SELECTION_BUFFER = 1.15  # select a little above mw_target: the Gate and device quirks trim batches


# ---------------------------------------------------------------- selection

def _safe_index(world: World, hh_id: str) -> int | None:
    """Household index for an id the LLM wrote, or None if it is malformed or out of range."""
    try:
        i = world.hh_index(str(hh_id))
    except ValueError:
        return None
    return i if 0 <= i < world.n else None


def _selector_mask(world: World, step: PlanStep) -> np.ndarray:
    mask = np.ones(world.n, dtype=bool)
    sel = step.selector
    if sel.archetypes:
        ids = [world.archetype_ids.index(a) for a in sel.archetypes if a in world.archetype_ids]
        mask &= np.isin(world.archetype_idx, ids)
    if sel.sections:
        ids = [world.section_ids.index(s) for s in sel.sections if s in world.section_ids]
        mask &= np.isin(world.section_idx, ids)
    if sel.household_ids:
        idx = [i for i in (_safe_index(world, h) for h in sel.household_ids) if i is not None]
        only = np.zeros(world.n, dtype=bool)
        only[idx] = True
        mask &= only
    return mask


def _candidates(world: World, step: PlanStep) -> np.ndarray:
    """Homes that have the device / channel and can physically respond. No policy filtering:
    explicitly named households are kept even without program enrolment (R02 judges them)."""
    a = step.action_type
    on = world.power_on.astype(bool)
    named = np.zeros(world.n, dtype=bool)
    if step.selector.household_ids:
        named = _selector_mask(world, step)
    if a in ("precool", "thermostat_setback"):
        roster = world.has_thermostat & (world.consent_thermostat | named)
        busy = world.precool_active if a == "precool" else world.setback_active
        dev = roster & on & ~busy.astype(bool) & ~world.overridden.astype(bool)
    elif a == "ev_pause":
        dev = world.has_ev & (world.consent_ev | named) & on & ~world.ev_paused.astype(bool)
    elif a == "battery_discharge":
        dev = world.has_battery & (world.consent_battery | named) & ~world.battery_discharging.astype(bool)
    elif a == "send_sms":
        dev = ac_present(world) & on  # every reachable home with AC; opt-in and vulnerability are R02/R04
    else:
        return np.zeros(world.n, dtype=bool)
    return dev & _selector_mask(world, step)


def _rank(world: World, step: PlanStep, mask: np.ndarray, start: datetime, end: datetime) -> tuple[np.ndarray, np.ndarray]:
    """Candidate indices sorted by kW saved / comfort cost (best first), with kW saved per home."""
    idx = np.flatnonzero(mask)
    if idx.size == 0:
        return idx, np.zeros(0)
    t_out = max(outdoor_at(world, start), outdoor_at(world, end))
    a = step.action_type
    if a in ("precool", "thermostat_setback"):
        offset = step.params.offset_c if step.params.offset_c is not None else (-2.0 if a == "precool" else 2.0)
        if a == "precool":
            # Pre-cooling stores ~|offset| °C of buffer: relief in the window ~ a share of AC load.
            kw = 0.25 * (world.ac_kw * duty_at(world, t_out))[idx]
            cost = np.ones(idx.size)
        else:
            kw = kw_saved(world, mask, offset, t_out)
            tmax = predicted_max_tin(world, mask, offset, start, end, t_out)
            cost = np.maximum(0.25, tmax - world.setpoint_base[idx])  # degrees of discomfort
    elif a == "ev_pause":
        charging = world.ev_charging[idx].astype(bool)
        kw = np.where(charging, EV_KW, EV_KW * 0.6)  # arriving soon: likely to charge in the window
        cost = 1.0 + np.maximum(world.ev_kwh_needed[idx], 0) / 40.0
    elif a == "battery_discharge":
        kw = np.where(world.battery_kw[idx] > 0, world.battery_kw[idx], BATTERY_KW) * (world.battery_soc[idx] > 0.2)
        cost = 1.0 / np.maximum(world.battery_soc[idx], 0.05)
    else:  # send_sms
        accept = np.array([world.archetype_params.get(x, {}).get("offer_acceptance", 0.3) for x in world.archetype_ids])
        acc = accept[world.archetype_idx[idx]] if accept.size else np.full(idx.size, 0.3)
        kw = acc * SMS_DUTY_CUT * (world.ac_kw * duty_at(world, t_out))[idx]
        cost = np.ones(idx.size)
    kw = np.asarray(kw, dtype=float)
    score = kw / np.maximum(cost, 1e-6)
    order = np.argsort(-score, kind="stable")
    return idx[order], kw[order]


def _take(world: World, idx: np.ndarray, kw: np.ndarray, mw_target: float | None, cap: int | None = None) -> tuple[np.ndarray, np.ndarray]:
    """Best-ranked homes until the (buffered) MW target is reached."""
    if cap is not None:
        idx, kw = idx[:cap], kw[:cap]
    if mw_target and mw_target > 0 and idx.size:
        cum = np.cumsum(kw) * world.scale / 1000.0
        n = int(np.searchsorted(cum, mw_target * SELECTION_BUFFER) + 1)
        idx, kw = idx[:n], kw[:n]
    return idx, kw


# ---------------------------------------------------------------- request building

def _new_request(world: World, plan: Plan, step: PlanStep, action: str, *, summary: str, targets: Targets,
                 params: dict[str, Any], window: Window | None, evidence: list[Evidence], expected_mw: float,
                 proposed_by: str, ls_run: str | None) -> ActionRequest:
    rid = new_id("ar_", world.run_id)
    now = sim_iso(world.sim_now)
    return ActionRequest(
        id=rid, run_id=world.run_id, created_sim_ts=now, sim_ts=now, action_type=action,  # type: ignore[arg-type]
        system=ACTION_SYSTEM[action], rung=RUNGS.get(action), proposed_by=proposed_by,  # type: ignore[arg-type]
        acting_for=ACTING_FOR, plan_id=plan.id, plan_version=plan.version, step_id=step.step_id,
        summary=summary[:300], targets=targets, params=plain(params), window=window, evidence=evidence,
        expected_mw=r1(expected_mw), status="proposed", idempotency_key=rid, langsmith_run_id=ls_run,
    )


def _sections_label(world: World, idx: np.ndarray) -> str:
    secs = sorted({world.section_ids[int(i)] for i in world.section_idx[idx]})
    return ", ".join(secs[:3]) + (f" +{len(secs) - 3}" if len(secs) > 3 else "")


def _homes(n_sim: int, scale: float) -> str:
    return f"{int(round(n_sim * scale)):,} homes"


def _times(world: World, step: PlanStep) -> tuple[datetime, datetime, Window]:
    start, end = window_dt(world, step.window)
    start = max(start, ceil_tick(world, world.sim_now))
    end = max(end, start + timedelta(minutes=5))
    return start, end, Window(start=hhmm(start), end=hhmm(end))


RECOMMAND_STATUSES = ["proposed", "needs_approval", "approved", "executing", "verified", "partial", "failed",
                      "rolled_back"]


async def _already_commanded(world: World, action: str, start: datetime) -> np.ndarray:
    """Homes already sent (or about to be sent) the same command for a window still open at `start`:
    offline devices that failed last time, sessions that ignored a pause, and units a Supervisor
    rolled back are not re-targeted on every replan."""
    out = np.zeros(world.n, dtype=bool)
    try:
        cur = db()[ACTION_REQUESTS].find(
            {"run_id": world.run_id, "action_type": action, "status": {"$in": RECOMMAND_STATUSES},
             "params.end": {"$gt": sim_iso(start)}},
            {"targets.household_ids": 1})
        async for d in cur:
            for h in (d.get("targets") or {}).get("household_ids") or []:
                i = _safe_index(world, h)
                if i is not None:
                    out[i] = True
    except Exception as exc:  # noqa: BLE001 - a lookup failure only means more retries
        log.debug("already-commanded lookup failed: %s", exc)
    return out


async def _device_requests(world: World, plan: Plan, step: PlanStep, evidence: list[Evidence],
                           ls_run: str | None) -> list[ActionRequest]:
    start, end, win = _times(world, step)
    mask = _candidates(world, step)
    if step.action_type in ("precool", "thermostat_setback", "ev_pause", "battery_discharge"):
        mask &= ~(await _already_commanded(world, step.action_type, start))
    idx, kw = _rank(world, step, mask, start, end)
    cap = None
    if step.action_type == "send_sms":
        amount = step.params.amount_cad or SMS_AMOUNT_CAD
        budget = float(world.scenario.get("budget_cad", 60000)) - float(world.metrics.get("incentive_spent_cad", 0.0))
        cap = max(int(budget / max(amount, 0.01) / max(world.scale, 1e-9)), 0)
    idx, kw = _take(world, idx, kw, step.params.mw_target, cap)
    if idx.size == 0:
        log.info("step %s (%s): no candidate homes", step.step_id, step.action_type)
        return []
    duration = int((end - start).total_seconds() // 60)
    a = step.action_type
    out: list[ActionRequest] = []
    n_batches = (idx.size + MAX_BATCH - 1) // MAX_BATCH
    for b in range(n_batches):
        part, part_kw = idx[b * MAX_BATCH:(b + 1) * MAX_BATCH], kw[b * MAX_BATCH:(b + 1) * MAX_BATCH]
        hh = [world.hh_ids[int(i)] for i in part]
        mw = to_mw(part_kw, world.scale)
        where = _sections_label(world, part)
        tag = f" (batch {b + 1}/{n_batches})" if n_batches > 1 else ""
        params: dict[str, Any] = {"start": sim_iso(start), "end": sim_iso(end), "duration_min": duration}
        targets = Targets(household_ids=hh)
        proposed_by = AGENT_ID
        if a in ("precool", "thermostat_setback"):
            off = step.params.offset_c if step.params.offset_c is not None else (-2.0 if a == "precool" else 2.0)
            params["offset_c"] = off
            if a == "precool":
                params["precool_offset_c"] = off
            verb = "Pre-cool" if a == "precool" else "Set back"
            summary = f"{verb} {_homes(part.size, world.scale)} in {where} by {off:+.1f} °C {win.start}–{win.end}{tag}"
        elif a == "ev_pause":
            params["until"] = sim_iso(end)
            summary = f"Pause EV charging for {_homes(part.size, world.scale)} in {where} {win.start}–{win.end}{tag}"
        elif a == "battery_discharge":
            params["kw"] = BATTERY_KW
            summary = f"Discharge {_homes(part.size, world.scale)} home batteries in {where} {win.start}–{win.end}{tag}"
        else:  # send_sms incentive
            amount = float(step.params.amount_cad or SMS_AMOUNT_CAD)
            params.update({"amount_cad": amount, "offer_type": "reduce_cooling", "message_kind": "incentive"})
            targets = Targets(recipient_ids=hh)
            proposed_by = NEGOTIATOR_ID
            summary = f"Text {_homes(part.size, world.scale)} in {where}: ${amount:.0f} CoolShift credit to ease cooling {win.start}–{win.end}{tag}"
        req = _new_request(world, plan, step, a, summary=summary, targets=targets, params=params, window=win,
                           evidence=evidence, expected_mw=mw, proposed_by=proposed_by, ls_run=ls_run)
        if a == "send_sms":
            req.messages = await _compose(req, world)
        out.append(req)
    return out


async def _compose(req: ActionRequest, world: World) -> list[OutboundMessage]:
    """Outbound texts via the Negotiator (LLM for featured residents / businesses, templates otherwise)."""
    try:
        from .negotiator import compose

        msgs = await compose(req, world)
        if msgs:
            return list(msgs)
    except Exception as exc:  # noqa: BLE001 - never stall on the Negotiator
        log.warning("negotiator.compose failed, using template: %s", exc)
    return _template_messages(req, world)


def _template_messages(req: ActionRequest, world: World) -> list[OutboundMessage]:
    w = req.window or Window(start="17:00", end="20:00")
    if req.action_type == "business_curtail_request":
        b_id = req.targets.business_id or ""
        mw = req.params.get("mw", req.expected_mw)
        body = (f"Lakeshore Grid Ops: could you curtail up to {mw:.0f} MW from {w.start} to {w.end} today to help "
                f"avoid outages? Please reply with what you can offer. {STOP_SUFFIX}")
        return [OutboundMessage(to=b_id, body=body[:320], offer={"type": "curtail", "amount_cad": 0, "ask": f"{mw:.0f} MW"})]
    amount = float(req.params.get("amount_cad", SMS_AMOUNT_CAD))
    body = (f"CoolShift: earn a ${amount:.0f} credit by easing your AC use {w.start}-{w.end} today while the grid is "
            f"tight. Reply YES to join. {STOP_SUFFIX}")
    return [OutboundMessage(to=r, body=body[:320], offer={"type": "reduce_cooling", "amount_cad": amount, "ask": "ease AC"})
            for r in req.targets.recipient_ids]


async def _business_requests(world: World, plan: Plan, step: PlanStep, evidence: list[Evidence],
                             ls_run: str | None) -> list[ActionRequest]:
    start, end, win = _times(world, step)
    out = []
    for b_id in step.selector.business_ids or []:
        biz = world.businesses.get(b_id)
        if biz is None:
            log.info("step %s: unknown business %s", step.step_id, b_id)
            continue
        mw = float(step.params.mw_target or biz.get("max_mw", 0))
        params = {"mw": r1(mw), "start": sim_iso(start), "end": sim_iso(end),
                  "duration_min": int((end - start).total_seconds() // 60),
                  "notice_min": int(biz.get("notice_min") or 0),
                  "phase": "dispatch" if biz.get("status") == "accepted" else "ask"}
        summary = f"Ask {biz.get('name', b_id)} to curtail {mw:.0f} MW {win.start}–{win.end}"
        req = _new_request(world, plan, step, "business_curtail_request", summary=summary,
                           targets=Targets(business_id=b_id, recipient_ids=[b_id]), params=params, window=win,
                           evidence=evidence, expected_mw=mw, proposed_by=NEGOTIATOR_ID, ls_run=ls_run)
        req.messages = await _compose(req, world)
        out.append(req)
    return out


async def nearest_cool_space(world: World, section_id: str) -> dict[str, Any] | None:
    """Nearest designated cool space to a section centroid ($geoNear on facilities, 2dsphere)."""
    point = None
    try:
        sec = await db()[SECTIONS].find_one({"_id": section_id}, {"centroid": 1})
        if sec and sec.get("centroid"):
            point = sec["centroid"]
        if point is None:
            i = world.section_index(section_id)
            m = world.section_idx == i
            if m.any():
                point = {"type": "Point", "coordinates": [float(world.lon[m].mean()), float(world.lat[m].mean())]}
        if point is not None:
            pipeline = [
                {"$geoNear": {"near": point, "distanceField": "distance_m", "spherical": True,
                              "query": {"kind": "cool_space", "designated": True}}},
                {"$limit": 1},
                {"$project": {"name": 1, "hours": 1, "distance_m": 1, "section_id": 1}},
            ]
            async for f in await db()[FACILITIES].aggregate(pipeline):
                return {"id": f["_id"], "name": f.get("name", "cool space"), "hours": f.get("hours"),
                        "distance_m": int(f.get("distance_m", 0))}
    except Exception as exc:  # noqa: BLE001 - fall back to the in-memory facility list
        log.debug("$geoNear unavailable: %s", exc)
    spaces = [f for f in world.facilities if f.get("kind") == "cool_space" and f.get("designated")]
    if not spaces:
        return None
    if point is None:
        return {"id": spaces[0].get("_id"), "name": spaces[0].get("name", "cool space")}
    lon, lat = point["coordinates"]
    best = min(spaces, key=lambda f: (f.get("lon", 0) - lon) ** 2 + (f.get("lat", 0) - lat) ** 2)
    return {"id": best.get("_id"), "name": best.get("name", "cool space"), "hours": best.get("hours")}


async def _outage_requests(world: World, plan: Plan, step: PlanStep, evidence: list[Evidence],
                           ls_run: str | None) -> list[ActionRequest]:
    start, _, _ = _times(world, step)
    start = max(start, ceil_tick(world, world.sim_now + timedelta(minutes=NOTICE_MIN)))
    duration = int(step.params.duration_min or 60)
    end = start + timedelta(minutes=duration)
    win = Window(start=hhmm(start), end=hhmm(end))
    out: list[ActionRequest] = []
    for s_id in step.selector.sections or []:
        if s_id not in world.section_ids:
            log.info("step %s: unknown section %s", step.step_id, s_id)
            continue
        i = world.section_index(s_id)
        in_sec = world.section_idx == i
        homes = int(in_sec.sum())
        mw = float(world.section_demand_mw[i]) if len(world.section_demand_mw) > i else float(step.expected_mw)

        # R12: notify vulnerable homes first, with the nearest open designated cool space
        space = await nearest_cool_space(world, s_id)
        vul_idx = np.flatnonzero(in_sec & world.vulnerable.astype(bool))
        # Never message residents about an interruption on the strength of untrusted input: an
        # injected outage gets no pre-notification (so R12 denies it too, alongside R14).
        untrusted = any(e.trust == "untrusted" for e in evidence)
        if vul_idx.size and untrusted:
            log.info("step %s: outage of %s cites untrusted evidence; no pre-notification sent", step.step_id, s_id)
        if vul_idx.size and not untrusted:
            place = space["name"] if space else "your nearest community cool space"
            body = (f"Lakeshore Grid Ops: a planned power interruption may affect your area ({s_id}) from "
                    f"{win.start} for up to {duration} min. Nearest open cool space: {place}. {STOP_SUFFIX}")[:320]
            recips = [world.hh_ids[int(k)] for k in vul_idx[:MAX_BATCH]]
            note = _new_request(
                world, plan, step, "send_sms",
                summary=f"Pre-notify {_homes(len(recips), world.scale)} (vulnerable) in {s_id} of the {win.start} "
                        f"interruption; nearest cool space {place}",
                targets=Targets(recipient_ids=recips),
                params={"message_kind": "cool_space_notice", "offer_type": "cool_space_notice", "amount_cad": 0,
                        "section_id": s_id, "outage_start": sim_iso(start), "cool_space": space},
                window=Window(start=hhmm(ceil_tick(world, world.sim_now)), end=win.start), evidence=evidence,
                expected_mw=0.0, proposed_by=NEGOTIATOR_ID, ls_run=ls_run,
            )
            note.messages = [OutboundMessage(to=r, body=body, offer={"type": "cool_space_notice", "amount_cad": 0,
                                                                      "ask": "none"}) for r in recips]
            out.append(note)

        summary = (f"Rotating outage {s_id}: {_homes(homes, world.scale)} "
                   f"({_homes(int(vul_idx.size), world.scale)} vulnerable, notified) for {duration} min from {win.start}, "
                   f"~{mw:.0f} MW")
        out.append(_new_request(
            world, plan, step, "feeder_outage", summary=summary, targets=Targets(section_id=s_id),
            params={"duration_min": duration, "start": sim_iso(start), "end": sim_iso(end),
                    "cool_space": space, "homes_real": int(homes * world.scale)},
            window=win, evidence=evidence, expected_mw=mw, proposed_by=AGENT_ID, ls_run=ls_run,
        ))
    return out


async def to_requests(plan: Plan, world: World, *, langsmith_run_id: str | None = None) -> list[ActionRequest]:
    """Ranks homes with thermal.kw_saved_estimate; <=2,000 per request; stores; ledger request_proposed."""
    from ..governance.ledger import append

    ls_run = langsmith_run_id or plan.langsmith_run_id
    requests: list[ActionRequest] = []
    for step in plan.steps:
        evidence = await resolve_evidence(world.run_id, [plan.id, *step.evidence_ids])
        try:
            if step.action_type in ("precool", "thermostat_setback", "ev_pause", "battery_discharge", "send_sms"):
                reqs = await _device_requests(world, plan, step, evidence, ls_run)
            elif step.action_type == "business_curtail_request":
                reqs = await _business_requests(world, plan, step, evidence, ls_run)
            elif step.action_type == "feeder_outage":
                reqs = await _outage_requests(world, plan, step, evidence, ls_run)
            else:
                log.info("step %s: %s is not a plan rung; skipped", step.step_id, step.action_type)
                reqs = []
        except Exception:  # noqa: BLE001 - one bad step must not sink the whole plan
            log.exception("targeting failed for step %s", step.step_id)
            reqs = []
        requests.extend(reqs)

    for req in requests:
        await db()[ACTION_REQUESTS].insert_one(req.to_mongo())
        n = len(req.targets.household_ids) or len(req.targets.recipient_ids) or (1 if req.targets.section_id or req.targets.business_id else 0)
        await append(
            world.run_id, "request_proposed", req.proposed_by, req.summary, sim_ts=req.created_sim_ts,
            acting_for=ACTING_FOR, refs={"request_id": req.id, "plan_version": plan.version},
            payload=plain({"action_type": req.action_type, "system": req.system, "rung": req.rung,
                           "step_id": req.step_id, "targets_count": n, "section_id": req.targets.section_id,
                           "business_id": req.targets.business_id, "params": req.params,
                           "window": req.window, "expected_mw": req.expected_mw,
                           "evidence": [e.model_dump() for e in req.evidence],
                           "untrusted_evidence": any(e.trust == "untrusted" for e in req.evidence)}),
            langsmith_run_id=req.langsmith_run_id,
        )
    return requests
