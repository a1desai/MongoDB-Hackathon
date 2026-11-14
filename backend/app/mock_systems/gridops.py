"""Lakeshore Grid Ops: fictional feeder-breaker control (PLAN §7.7).

`open` cuts a feeder section; `close` restores it. Defense in depth: `open` is rejected unless it
carries an approval_id whose approvals doc is "approved" for this run (and for this section, when
the linked action_request names one). Quirks: switching takes 1 tick, and 5% of first attempts
(per Idempotency-Key) fail with 503; a retry with the same key goes through.
"""

from datetime import datetime, timedelta
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from pymongo import UpdateOne

from ..config import get_settings
from ..db import ACTION_REQUESTS, APPROVALS, MOCK_GRIDOPS_SECTIONS, db
from ..sim.world_state import World
from ..timeutil import sim_iso
from .common import (
    APPLIED,
    PENDING,
    Command,
    MockCtx,
    MockError,
    context,
    counts,
    ensure_state,
    get_command,
    idempotency_key,
    new_command,
    world_event,
)

SYSTEM = "gridops"
FIRST_ATTEMPT_503_RATE = 0.05
MAX_DURATION_MIN = 240

router = APIRouter(prefix="/mock/gridops/v1", tags=["mock:gridops"])
ctx_dep = Depends(context(SYSTEM))
key_dep = Depends(idempotency_key)


class OpenIn(BaseModel):
    approval_id: str | None = None
    duration_min: int = 60
    request_id: str | None = None


class CloseIn(BaseModel):
    request_id: str | None = None


def set_section_power(world: World, sidx: int, on: bool, until: datetime | None) -> None:
    """Use sim/grid.py when it is available; otherwise switch the arrays directly."""
    try:
        from ..sim.grid import set_section_power as grid_set
    except ImportError:
        grid_set = None
    if grid_set is not None:
        grid_set(world, sidx, on, until)
        return
    mask = world.section_idx == sidx
    world.power_on[mask] = on
    if not on and not world.section_cut[sidx]:
        world.section_cut_count_today[sidx] += 1
    world.section_cut[sidx] = not on
    world.section_cut_until[sidx] = until


def _section(world: World, section_id: str) -> int:
    try:
        return world.section_index(section_id)
    except ValueError:
        raise MockError(404, "unknown_section", section_id) from None


def _flaky_first_attempt(world: World, key: str) -> None:
    go = ensure_state(world)[SYSTEM]
    if key in go["attempted"]:
        return
    go["attempted"].add(key)
    if world.mock["rng"][SYSTEM].random() < FIRST_ATTEMPT_503_RATE:
        raise MockError(503, "switching_unavailable", "SCADA link busy, retry with the same Idempotency-Key")


def _state(world: World, section_id: str) -> dict[str, Any]:
    return ensure_state(world)[SYSTEM]["sections"].setdefault(section_id, {"breaker": "closed"})


def _pending(world: World, sidx: int) -> Command | None:
    for cid in reversed(world.mock["handles"]):
        cmd = world.mock["commands"][cid]
        if cmd.system == SYSTEM and int(cmd.idx[0]) == sidx and not cmd.finished:
            return cmd
    return None


async def open_section(world: World, section_id: str, body: OpenIn, key: str) -> dict[str, Any]:
    sidx = _section(world, section_id)
    _flaky_first_attempt(world, key)
    if not body.approval_id:
        raise MockError(409, "approval_required", "opening a feeder breaker requires an approved approval_id")
    approval = await db()[APPROVALS].find_one({"_id": body.approval_id})
    if not approval or approval.get("run_id") != world.run_id or approval.get("status") != "approved":
        raise MockError(409, "approval_required", f"approval {body.approval_id} is not approved for this run")
    req_id = approval.get("request_id")
    if req_id:
        req = await db()[ACTION_REQUESTS].find_one({"_id": req_id}, {"targets": 1})
        target = ((req or {}).get("targets") or {}).get("section_id")
        if target and target != section_id:
            raise MockError(409, "approval_mismatch", f"approval {body.approval_id} is for {target}, not {section_id}")
    if not 0 < body.duration_min <= MAX_DURATION_MIN:
        raise MockError(422, "bad_duration", f"duration_min must be 1..{MAX_DURATION_MIN}")
    if world.section_cut[sidx] or _state(world, section_id)["breaker"] == "open":
        raise MockError(409, "already_open", f"{section_id} is already open")
    pend = _pending(world, sidx)
    if pend is not None:
        raise MockError(409, "switching", f"{section_id} has a switching operation in progress ({pend.id})")
    planned_close = world.sim_now + timedelta(minutes=get_settings().tick_sim_minutes + body.duration_min)
    cmd = new_command(
        world,
        SYSTEM,
        "open",
        np.asarray([sidx]),
        latency=1,
        params={"section_id": section_id, "approval_id": body.approval_id, "duration_min": body.duration_min},
        request_id=body.request_id,
    )
    return {
        "switch_id": cmd.id,
        "section_id": section_id,
        "status": "switching",
        "action": "open",
        "approval_id": body.approval_id,
        "duration_min": body.duration_min,
        "planned_close_sim_ts": sim_iso(planned_close),
    }


async def close_section(world: World, section_id: str, body: CloseIn, key: str) -> dict[str, Any]:
    sidx = _section(world, section_id)
    _flaky_first_attempt(world, key)
    cmd = new_command(
        world, SYSTEM, "close", np.asarray([sidx]), latency=1, params={"section_id": section_id}, request_id=body.request_id
    )
    return {"switch_id": cmd.id, "section_id": section_id, "status": "switching", "action": "close"}


def step(world: World, cmd: Command) -> list[dict[str, Any]]:
    if not ((cmd.state == PENDING) & (cmd.apply_tick <= world.tick)).any():
        return []
    m = ensure_state(world)
    sid = cmd.params["section_id"]
    sidx = int(cmd.idx[0])
    st = _state(world, sid)
    ts = sim_iso(world.sim_now)
    cmd.state[:] = APPLIED
    cmd.phase, cmd.finished = "done", True
    m["dirty"]["commands"].add(cmd.id)
    m["dirty"][SYSTEM].add(sid)
    if cmd.kind == "open":
        set_section_power(world, sidx, False, None)  # agent cuts are restored by the Restorer via close
        planned = world.sim_now + timedelta(minutes=cmd.params["duration_min"])
        st.update(
            breaker="open",
            opened_sim_ts=ts,
            planned_close_sim_ts=sim_iso(planned),
            approval_id=cmd.params["approval_id"],
            request_id=cmd.request_id,
            switch_id=cmd.id,
        )
        cmd.extra["planned_close"] = planned
        return [
            world_event(
                f"Lakeshore Grid Ops opened breaker {sid} (approval {cmd.params['approval_id']})",
                system=SYSTEM,
                section_id=sid,
                command_id=cmd.id,
                request_id=cmd.request_id,
                planned_close_sim_ts=st["planned_close_sim_ts"],
            )
        ]
    was_open = st.get("breaker") == "open" or bool(world.section_cut[sidx])
    set_section_power(world, sidx, True, None)
    st.update(breaker="closed", closed_sim_ts=ts, switch_id=cmd.id, planned_close_sim_ts=None)
    if not was_open:
        return []
    return [
        world_event(
            f"Lakeshore Grid Ops closed breaker {sid}; section re-energised",
            system=SYSTEM,
            section_id=sid,
            command_id=cmd.id,
            request_id=cmd.request_id,
        )
    ]


def section_state(world: World, section_id: str) -> dict[str, Any]:
    sidx = _section(world, section_id)
    st = _state(world, section_id)
    pend = _pending(world, sidx)
    return {
        "section_id": section_id,
        "breaker": "open" if world.section_cut[sidx] else "closed",
        "energized": not bool(world.section_cut[sidx]),
        "switching": pend.kind if pend else None,
        "protected": bool(world.section_protected[sidx]),
        "households": int(world.section_households[sidx]),
        "demand_mw": round(float(world.section_demand_mw[sidx]), 3),
        "cut_minutes_today": int(world.section_cut_minutes_today[sidx]),
        "opened_sim_ts": st.get("opened_sim_ts"),
        "planned_close_sim_ts": st.get("planned_close_sim_ts"),
        "approval_id": st.get("approval_id"),
    }


def status(world: World, cmd: Command) -> dict[str, Any]:
    c = counts(cmd)
    c["overridden"] = 0
    return {
        "switch_id": cmd.id,
        "kind": cmd.kind,
        "section_id": cmd.params["section_id"],
        "finished": cmd.finished,
        "request_id": cmd.request_id,
        **c,
    }


def snapshot(world: World, section_id: str) -> dict[str, Any]:
    return section_state(world, section_id)


def persist_ops(world: World) -> dict[str, list[UpdateOne]]:
    m = world.mock
    ts = sim_iso(world.sim_now)
    ops = [
        UpdateOne(
            {"_id": f"{world.run_id}:{sid}"},
            {"$set": {"run_id": world.run_id, "sim_ts": ts, **section_state(world, sid)}},
            upsert=True,
        )
        for sid in sorted(m["dirty"][SYSTEM])
    ]
    m["dirty"][SYSTEM].clear()
    return {MOCK_GRIDOPS_SECTIONS: ops}


@router.post("/sections/{section_id}/open")
async def post_open(section_id: str, body: OpenIn, ctx: MockCtx = ctx_dep, key: str = key_dep):
    return await ctx.once(key, lambda: open_section(ctx.world, section_id, body, key))


@router.post("/sections/{section_id}/close")
async def post_close(section_id: str, body: CloseIn | None = None, ctx: MockCtx = ctx_dep, key: str = key_dep):
    return await ctx.once(key, lambda: close_section(ctx.world, section_id, body or CloseIn(), key))


@router.get("/sections/{section_id}")
async def get_section(section_id: str, ctx: MockCtx = ctx_dep):
    return await ctx.read(lambda: section_state(ctx.world, section_id))


@router.get("/switches/{switch_id}")
async def get_switch(switch_id: str, ctx: MockCtx = ctx_dep):
    def read() -> dict[str, Any]:
        cmd = get_command(ctx.world, switch_id)
        if cmd.system != SYSTEM:
            raise MockError(404, "not_found", switch_id)
        return status(ctx.world, cmd)

    return await ctx.read(read)
