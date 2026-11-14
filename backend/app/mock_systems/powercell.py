"""PowerCell: fictional home-battery fleet (PLAN §7.7).

Quirk: a unit stops discharging on its own at 20% state of charge, whatever the dispatch says.
Commands apply on the next tick; a dispatch discharges between `start` and `end`.
"""

from typing import Any

import numpy as np
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from pymongo import UpdateOne

from ..db import MOCK_POWERCELL_UNITS
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
    device_id,
    ensure_state,
    get_command,
    idempotency_key,
    new_command,
    parse_device_ids,
    parse_when,
    world_event,
)

SYSTEM = "powercell"
MIN_SOC = 0.20
MAX_KW = 5.0

router = APIRouter(prefix="/mock/powercell/v1", tags=["mock:powercell"])
ctx_dep = Depends(context(SYSTEM))
key_dep = Depends(idempotency_key)


class DispatchIn(BaseModel):
    unit_ids: list[str]
    kw: float = MAX_KW
    start: str | None = None
    end: str | None = None
    request_id: str | None = None


class StopIn(BaseModel):
    unit_ids: list[str] = []
    dispatch_id: str | None = None
    request_id: str | None = None


def create_dispatch(world: World, body: DispatchIn) -> dict[str, Any]:
    idx, rejected = parse_device_ids(world, body.unit_ids, world.has_battery)
    if idx.size == 0:
        raise MockError(422, "no_valid_units", "none of the unit_ids are PowerCell units")
    if body.kw <= 0:
        raise MockError(422, "bad_kw", "kw must be positive")
    start = parse_when(world, body.start, world.sim_now)
    end = parse_when(world, body.end, world.sim_end)
    if end <= start:
        raise MockError(422, "bad_window", "end must be after start")
    kw = min(body.kw, MAX_KW)
    cmd = new_command(
        world, SYSTEM, "dispatch", idx, latency=1, params={"kw": kw}, request_id=body.request_id, start=start, end=end
    )
    cmd.extra["soc_stopped"] = np.zeros(idx.size, dtype=bool)
    return {
        "dispatch_id": cmd.id,
        "status": "accepted",
        "units": int(idx.size),
        "kw_per_unit": kw,
        "rejected": rejected[:50],
        "rejected_count": len(rejected),
        "start": sim_iso(start),
        "end": sim_iso(end),
    }


def create_stop(world: World, body: StopIn) -> dict[str, Any]:
    target = None
    if body.dispatch_id:
        target = get_command(world, body.dispatch_id)
        if target.system != SYSTEM or target.kind != "dispatch":
            raise MockError(404, "not_found", f"{body.dispatch_id} is not a PowerCell dispatch")
        idx = target.idx
    else:
        idx, _ = parse_device_ids(world, body.unit_ids, world.has_battery)
    if idx.size == 0:
        raise MockError(422, "no_valid_units", "nothing to stop")
    cmd = new_command(
        world, SYSTEM, "stop", idx, latency=1, params={"target": target.id if target else None}, request_id=body.request_id
    )
    return {"command_id": cmd.id, "status": "accepted", "units": int(idx.size), "stops": cmd.params["target"]}


def step(world: World, cmd: Command) -> list[dict[str, Any]]:
    m = ensure_state(world)
    pc = m[SYSTEM]
    owner = pc["owner"]
    dirty = m["dirty"]
    events: list[dict[str, Any]] = []
    due = np.flatnonzero((cmd.state == PENDING) & (cmd.apply_tick <= world.tick))
    if due.size:
        dirty["commands"].add(cmd.id)
        cmd.state[due] = APPLIED
        hh = cmd.idx[due]
        hh = hh[owner[hh] <= cmd.handle]
        if cmd.kind == "dispatch":
            owner[hh] = cmd.handle
        else:
            world.battery_discharging[hh] = False
            owner[hh] = -1
            if cmd.params.get("target"):
                tgt = m["commands"][cmd.params["target"]]
                tgt.phase, tgt.finished = "ended", True
                dirty["commands"].add(tgt.id)
        dirty[SYSTEM].update(hh.tolist())

    if cmd.kind == "dispatch" and not cmd.finished:
        now = world.sim_now
        slots = np.flatnonzero((cmd.state == APPLIED) & (owner[cmd.idx] == cmd.handle))
        mine = cmd.idx[slots]
        if now >= cmd.end:
            world.battery_discharging[mine] = False
            owner[mine] = -1
            cmd.phase, cmd.finished = "ended", True
            dirty["commands"].add(cmd.id)
            dirty[SYSTEM].update(mine.tolist())
        elif now >= cmd.start:
            low = world.battery_soc[mine] <= MIN_SOC
            stopped = cmd.extra["soc_stopped"]
            newly = slots[low & ~stopped[slots]]
            stopped[slots[low]] = True
            pc["stopped_soc"][mine[low]] = True
            world.battery_discharging[mine[low]] = False
            run = mine[~low]
            world.battery_discharging[run] = True
            world.battery_kw[run] = cmd.params["kw"]
            cmd.phase = "active"
            dirty["commands"].add(cmd.id)
            dirty[SYSTEM].update(mine.tolist())
            if newly.size:
                events.append(
                    world_event(
                        f"PowerCell: {newly.size} unit(s) stopped at {MIN_SOC:.0%} state of charge (dispatch {cmd.id})",
                        system=SYSTEM,
                        command_id=cmd.id,
                        request_id=cmd.request_id,
                        units=int(newly.size),
                    )
                )
    elif cmd.kind == "stop" and not (cmd.state == PENDING).any():
        cmd.finished = True
    return events


def status(world: World, cmd: Command) -> dict[str, Any]:
    c = counts(cmd)
    c["overridden"] = 0
    if cmd.kind == "dispatch":
        owned = world.mock[SYSTEM]["owner"][cmd.idx] == cmd.handle
        c["discharging"] = int(((cmd.state == APPLIED) & owned & world.battery_discharging[cmd.idx]).sum())
        c["stopped_soc"] = int(cmd.extra["soc_stopped"].sum())
    return {
        "dispatch_id" if cmd.kind == "dispatch" else "command_id": cmd.id,
        "kind": cmd.kind,
        "phase": cmd.phase,
        "finished": cmd.finished,
        "kw_per_unit": cmd.params.get("kw"),
        "start": sim_iso(cmd.start) if cmd.start else None,
        "end": sim_iso(cmd.end) if cmd.end else None,
        "request_id": cmd.request_id,
        **c,
    }


def unit_state(world: World, i: int) -> dict[str, Any]:
    m = ensure_state(world)
    owner = int(m[SYSTEM]["owner"][i])
    return {
        "unit_id": device_id(SYSTEM, i),
        "household_id": world.hh_ids[i],
        "soc": round(float(world.battery_soc[i]), 3),
        "discharging": bool(world.battery_discharging[i]),
        "kw": float(world.battery_kw[i]) if world.battery_discharging[i] else 0.0,
        "stopped_at_min_soc": bool(m[SYSTEM]["stopped_soc"][i]),
        "dispatch_id": m["handles"][owner] if owner >= 0 else None,
    }


def snapshot(world: World, idx: np.ndarray) -> dict[str, Any]:
    return {
        "units": int(idx.size),
        "discharging": int(world.battery_discharging[idx].sum()),
        "mean_soc": round(float(world.battery_soc[idx].mean()), 3) if idx.size else None,
    }


def persist_ops(world: World) -> dict[str, list[UpdateOne]]:
    m = world.mock
    ts = sim_iso(world.sim_now)
    ops = [
        UpdateOne(
            {"_id": f"{world.run_id}:{device_id(SYSTEM, i)}"},
            {"$set": {"run_id": world.run_id, "sim_ts": ts, **unit_state(world, i)}},
            upsert=True,
        )
        for i in sorted(m["dirty"][SYSTEM])
    ]
    m["dirty"][SYSTEM].clear()
    return {MOCK_POWERCELL_UNITS: ops}


@router.post("/dispatch")
async def post_dispatch(body: DispatchIn, ctx: MockCtx = ctx_dep, key: str = key_dep):
    return await ctx.once(key, lambda: create_dispatch(ctx.world, body))


@router.post("/stop")
async def post_stop(body: StopIn, ctx: MockCtx = ctx_dep, key: str = key_dep):
    return await ctx.once(key, lambda: create_stop(ctx.world, body))


@router.get("/dispatches/{dispatch_id}")
async def get_dispatch(dispatch_id: str, ctx: MockCtx = ctx_dep):
    def read() -> dict[str, Any]:
        cmd = get_command(ctx.world, dispatch_id)
        if cmd.system != SYSTEM:
            raise MockError(404, "not_found", dispatch_id)
        return status(ctx.world, cmd)

    return await ctx.read(read)


@router.get("/units/{unit_id}")
async def get_unit(unit_id: str, ctx: MockCtx = ctx_dep):
    def read() -> dict[str, Any]:
        idx, _ = parse_device_ids(ctx.world, [unit_id], ctx.world.has_battery)
        if idx.size == 0:
            raise MockError(404, "not_found", unit_id)
        return unit_state(ctx.world, int(idx[0]))

    return await ctx.read(read)
