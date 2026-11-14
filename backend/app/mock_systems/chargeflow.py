"""ChargeFlow: fictional EV charging network (PLAN §7.7).

Quirk (seeded): 4% of sessions ignore a pause and keep charging, although the API accepted it.
Commands apply on the next tick. A pause lasts until `until`, then charging resumes.
"""

from datetime import timedelta
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from pymongo import UpdateOne

from ..db import MOCK_CHARGEFLOW_SESSIONS
from ..sim.world_state import World
from ..timeutil import sim_iso
from .common import (
    APPLIED,
    IGNORED,
    PENDING,
    Command,
    MockCtx,
    MockError,
    context,
    counts,
    device_id,
    ensure_state,
    get_command,
    idempotency_key,
    new_command,
    parse_device_ids,
    parse_when,
)

SYSTEM = "chargeflow"
IGNORE_RATE = 0.04
EV_KW = 7.2
DEFAULT_PAUSE_MIN = 60

router = APIRouter(prefix="/mock/chargeflow/v1", tags=["mock:chargeflow"])
ctx_dep = Depends(context(SYSTEM))
key_dep = Depends(idempotency_key)


class PauseIn(BaseModel):
    session_ids: list[str]
    until: str | None = None  # "HH:MM" or ISO; default now + 60 min
    request_id: str | None = None


class ResumeIn(BaseModel):
    session_ids: list[str]
    request_id: str | None = None


def create_pause(world: World, body: PauseIn) -> dict[str, Any]:
    idx, rejected = parse_device_ids(world, body.session_ids, world.has_ev)
    if idx.size == 0:
        raise MockError(422, "no_valid_sessions", "none of the session_ids are ChargeFlow sessions")
    until = parse_when(world, body.until, world.sim_now + timedelta(minutes=DEFAULT_PAUSE_MIN))
    if until <= world.sim_now:
        raise MockError(422, "bad_window", "until must be in the future")
    cmd = new_command(world, SYSTEM, "pause", idx, latency=1, request_id=body.request_id, start=world.sim_now, end=until)
    return {
        "command_id": cmd.id,
        "status": "accepted",
        "sessions": int(idx.size),
        "rejected": rejected[:50],
        "rejected_count": len(rejected),
        "until": sim_iso(until),
    }


def create_resume(world: World, body: ResumeIn) -> dict[str, Any]:
    idx, rejected = parse_device_ids(world, body.session_ids, world.has_ev)
    if idx.size == 0:
        raise MockError(422, "no_valid_sessions", "none of the session_ids are ChargeFlow sessions")
    cmd = new_command(world, SYSTEM, "resume", idx, latency=1, request_id=body.request_id)
    return {"command_id": cmd.id, "status": "accepted", "sessions": int(idx.size), "rejected_count": len(rejected)}


def step(world: World, cmd: Command) -> list[dict[str, Any]]:
    m = ensure_state(world)
    owner = m[SYSTEM]["owner"]
    dirty = m["dirty"]
    due = np.flatnonzero((cmd.state == PENDING) & (cmd.apply_tick <= world.tick))
    if due.size:
        dirty["commands"].add(cmd.id)
        if cmd.kind == "pause":
            ignore = m["rng"][SYSTEM].random(due.size) < IGNORE_RATE
            cmd.state[due[ignore]] = IGNORED  # accepted by the API, but the charger keeps going
            ok = due[~ignore]
            cmd.state[ok] = APPLIED
            hh = cmd.idx[ok]
            hh = hh[owner[hh] <= cmd.handle]
            world.ev_paused[hh] = True
            owner[hh] = cmd.handle
            cmd.phase = "active"
        else:
            cmd.state[due] = APPLIED
            hh = cmd.idx[due]
            hh = hh[owner[hh] <= cmd.handle]
            world.ev_paused[hh] = False
            owner[hh] = -1
        dirty[SYSTEM].update(hh.tolist())

    if cmd.kind == "pause" and not cmd.finished and world.sim_now >= cmd.end:
        mine = cmd.idx[owner[cmd.idx] == cmd.handle]
        world.ev_paused[mine] = False
        owner[mine] = -1
        cmd.phase, cmd.finished = "ended", True
        dirty["commands"].add(cmd.id)
        dirty[SYSTEM].update(mine.tolist())
    elif cmd.kind == "resume" and not (cmd.state == PENDING).any():
        cmd.finished = True
    return []


def status(world: World, cmd: Command) -> dict[str, Any]:
    c = counts(cmd)
    if cmd.kind == "pause":
        owned = world.mock[SYSTEM]["owner"][cmd.idx] == cmd.handle
        # Paused by us, but the owner un-paused it (PLAN §7.4 owner override).
        c["overridden"] = int(((cmd.state == APPLIED) & owned & ~world.ev_paused[cmd.idx]).sum())
        c["paused_now"] = int(((cmd.state == APPLIED) & owned & world.ev_paused[cmd.idx]).sum())
    else:
        c["overridden"] = 0
    return {
        "command_id": cmd.id,
        "kind": cmd.kind,
        "phase": cmd.phase,
        "finished": cmd.finished,
        "until": sim_iso(cmd.end) if cmd.end else None,
        "request_id": cmd.request_id,
        **c,
    }


def session_state(world: World, i: int) -> dict[str, Any]:
    m = ensure_state(world)
    owner = int(m[SYSTEM]["owner"][i])
    return {
        "session_id": device_id(SYSTEM, i),
        "household_id": world.hh_ids[i],
        "charging": bool(world.ev_charging[i]),
        "paused": bool(world.ev_paused[i]),
        "kwh_needed": round(float(world.ev_kwh_needed[i]), 2),
        "command_id": m["handles"][owner] if owner >= 0 else None,
    }


def snapshot(world: World, idx: np.ndarray) -> dict[str, Any]:
    return {
        "sessions": int(idx.size),
        "charging": int(world.ev_charging[idx].sum()),
        "paused": int(world.ev_paused[idx].sum()),
    }


def persist_ops(world: World) -> dict[str, list[UpdateOne]]:
    m = world.mock
    ts = sim_iso(world.sim_now)
    ops = [
        UpdateOne(
            {"_id": f"{world.run_id}:{device_id(SYSTEM, i)}"},
            {"$set": {"run_id": world.run_id, "sim_ts": ts, **session_state(world, i)}},
            upsert=True,
        )
        for i in sorted(m["dirty"][SYSTEM])
    ]
    m["dirty"][SYSTEM].clear()
    return {MOCK_CHARGEFLOW_SESSIONS: ops}


@router.post("/pause")
async def post_pause(body: PauseIn, ctx: MockCtx = ctx_dep, key: str = key_dep):
    return await ctx.once(key, lambda: create_pause(ctx.world, body))


@router.post("/resume")
async def post_resume(body: ResumeIn, ctx: MockCtx = ctx_dep, key: str = key_dep):
    return await ctx.once(key, lambda: create_resume(ctx.world, body))


@router.get("/commands/{command_id}")
async def get_cmd(command_id: str, ctx: MockCtx = ctx_dep):
    def read() -> dict[str, Any]:
        cmd = get_command(ctx.world, command_id)
        if cmd.system != SYSTEM:
            raise MockError(404, "not_found", command_id)
        return status(ctx.world, cmd)

    return await ctx.read(read)


@router.get("/sessions/{session_id}")
async def get_session(session_id: str, ctx: MockCtx = ctx_dep):
    def read() -> dict[str, Any]:
        idx, _ = parse_device_ids(ctx.world, [session_id], ctx.world.has_ev)
        if idx.size == 0:
            raise MockError(404, "not_found", session_id)
        return session_state(ctx.world, int(idx[0]))

    return await ctx.read(read)
