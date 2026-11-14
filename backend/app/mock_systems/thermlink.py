"""ThermLink: fictional smart-thermostat vendor (PLAN §7.7).

Quirks (seeded): 6% of thermostats are offline for the whole run, each device applies a command
1-2 ticks after it is sent, and 3% of device commands fail. An event sets the thermostat offset
(negative = pre-cool, positive = setback) between `start` and `end`; an optional
`precool_offset_c` runs for the 60 minutes before `start`. offset_c = 0 is a restore.
"""

from datetime import timedelta
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from pymongo import UpdateOne

from ..db import MOCK_THERMLINK_BATCHES, MOCK_THERMLINK_DEVICES
from ..sim.world_state import World
from ..timeutil import sim_iso
from .common import (
    APPLIED,
    FAILED,
    OFFLINE,
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

SYSTEM = "thermlink"
OFFSET_LIMIT_C = 5.0  # vendor hardware limit; the Policy Gate (R03) enforces the program's ±2 °C
FAIL_RATE = 0.03
PRECOOL_LEAD_MIN = 60

router = APIRouter(prefix="/mock/thermlink/v1", tags=["mock:thermlink"])
ctx_dep = Depends(context(SYSTEM))
key_dep = Depends(idempotency_key)


class EventIn(BaseModel):
    device_ids: list[str]
    offset_c: float
    precool_offset_c: float | None = None
    start: str | None = None  # "HH:MM" or ISO; default now
    end: str | None = None  # default end of the simulated day
    request_id: str | None = None


class EndIn(BaseModel):
    request_id: str | None = None


# ---------------------------------------------------------------- state changes

def _reset(world: World, hh: np.ndarray) -> None:
    world.offset_c[hh] = 0.0
    world.precool_active[hh] = False
    world.setback_active[hh] = False


def create_event(world: World, body: EventIn) -> dict[str, Any]:
    m = ensure_state(world)
    if abs(body.offset_c) > OFFSET_LIMIT_C or (body.precool_offset_c and abs(body.precool_offset_c) > OFFSET_LIMIT_C):
        raise MockError(422, "offset_out_of_range", f"ThermLink accepts offsets within ±{OFFSET_LIMIT_C} °C")
    idx, rejected = parse_device_ids(world, body.device_ids, world.has_thermostat)
    if idx.size == 0:
        raise MockError(422, "no_valid_devices", "none of the device_ids are ThermLink thermostats")
    start = parse_when(world, body.start, world.sim_now)
    end = parse_when(world, body.end, world.sim_end)
    if end <= start:
        raise MockError(422, "bad_window", "end must be after start")
    restore = body.offset_c == 0 and not body.precool_offset_c
    rng = m["rng"][SYSTEM]
    cmd = new_command(
        world,
        SYSTEM,
        "restore" if restore else "event",
        idx,
        latency=rng.integers(1, 3, size=idx.size),  # 1-2 ticks
        params={"offset_c": body.offset_c, "precool_offset_c": body.precool_offset_c},
        request_id=body.request_id,
        start=start,
        end=end,
    )
    cmd.state[m[SYSTEM]["offline"][idx]] = OFFLINE
    cmd.extra["counted"] = np.zeros(idx.size, dtype=bool)  # setback counted toward setback_count_today
    return {
        "batch_id": cmd.id,
        "status": "accepted",
        "kind": cmd.kind,
        "devices": int(idx.size),
        "rejected": rejected[:50],
        "rejected_count": len(rejected),
        "start": sim_iso(start),
        "end": sim_iso(end),
    }


def end_event(world: World, batch_id: str, body: EndIn) -> dict[str, Any]:
    target = get_command(world, batch_id)
    if target.system != SYSTEM or target.kind != "event":
        raise MockError(404, "not_found", f"{batch_id} is not a ThermLink event batch")
    cmd = new_command(
        world, SYSTEM, "end", target.idx, latency=1, params={"target": batch_id}, request_id=body.request_id
    )
    return {"batch_id": cmd.id, "ends": batch_id, "status": "accepted", "devices": cmd.total}


def step(world: World, cmd: Command) -> list[dict[str, Any]]:
    m = ensure_state(world)
    owner = m[SYSTEM]["owner"]
    dirty = m["dirty"]
    due = np.flatnonzero((cmd.state == PENDING) & (cmd.apply_tick <= world.tick))
    if due.size:
        dirty["commands"].add(cmd.id)
        if cmd.kind == "end":
            cmd.state[due] = APPLIED
            target = m["commands"][cmd.params["target"]]
            mine = target.idx[owner[target.idx] == target.handle]
            _reset(world, mine)
            owner[mine] = -1
            target.phase, target.finished = "ended", True
            dirty["commands"].add(target.id)
            dirty[SYSTEM].update(mine.tolist())
        else:
            fail = m["rng"][SYSTEM].random(due.size) < FAIL_RATE
            cmd.state[due[fail]] = FAILED
            ok = due[~fail]
            cmd.state[ok] = APPLIED
            hh = cmd.idx[ok]
            hh = hh[owner[hh] <= cmd.handle]  # a newer command on the same device wins
            if cmd.kind == "restore":
                _reset(world, hh)
                owner[hh] = -1
            else:
                owner[hh] = cmd.handle
            dirty[SYSTEM].update(hh.tolist())

    if cmd.kind == "event" and not cmd.finished:
        _advance_window(world, cmd, owner)
    elif cmd.kind in ("restore", "end") and not (cmd.state == PENDING).any():
        cmd.finished = True
    return []


def _advance_window(world: World, cmd: Command, owner: np.ndarray) -> None:
    m = world.mock
    now = world.sim_now
    slots = np.flatnonzero((cmd.state == APPLIED) & (owner[cmd.idx] == cmd.handle))
    mine = cmd.idx[slots]
    offset = float(cmd.params["offset_c"])
    pre = cmd.params.get("precool_offset_c")
    if now >= cmd.end:
        _reset(world, mine)
        owner[mine] = -1
        cmd.phase, cmd.finished = "ended", True
    elif now >= cmd.start:
        world.offset_c[mine] = offset
        world.precool_active[mine] = offset < 0
        world.setback_active[mine] = offset > 0
        if offset > 0:
            counted = cmd.extra["counted"]
            new = slots[~counted[slots]]
            world.setback_count_today[cmd.idx[new]] += 1
            counted[new] = True
        cmd.phase = "active"
    elif pre and now >= cmd.start - timedelta(minutes=PRECOOL_LEAD_MIN):
        world.offset_c[mine] = float(pre)
        world.precool_active[mine] = True
        world.setback_active[mine] = False
        cmd.phase = "precool"
    else:
        return
    m["dirty"]["commands"].add(cmd.id)
    m["dirty"][SYSTEM].update(mine.tolist())


# ---------------------------------------------------------------- reads

def status(world: World, cmd: Command) -> dict[str, Any]:
    c = counts(cmd)
    applied = cmd.state == APPLIED
    c["overridden"] = int((applied & world.overridden[cmd.idx]).sum()) if cmd.kind == "event" else 0
    c["active"] = int((applied & (world.mock[SYSTEM]["owner"][cmd.idx] == cmd.handle)).sum()) if cmd.kind == "event" else 0
    return {
        "batch_id": cmd.id,
        "kind": cmd.kind,
        "phase": cmd.phase,
        "finished": cmd.finished,
        "offset_c": cmd.params.get("offset_c"),
        "start": sim_iso(cmd.start) if cmd.start else None,
        "end": sim_iso(cmd.end) if cmd.end else None,
        "request_id": cmd.request_id,
        **c,
    }


def device_state(world: World, i: int) -> dict[str, Any]:
    m = ensure_state(world)
    owner = int(m[SYSTEM]["owner"][i])
    mode = "precool" if world.precool_active[i] else "setback" if world.setback_active[i] else "idle"
    return {
        "device_id": device_id(SYSTEM, i),
        "household_id": world.hh_ids[i],
        "online": not bool(m[SYSTEM]["offline"][i]),
        "offset_c": float(world.offset_c[i]),
        "mode": mode,
        "user_override": bool(world.overridden[i]),
        "batch_id": m["handles"][owner] if owner >= 0 else None,
        "indoor_c": round(float(world.t_in[i]), 2),
    }


def snapshot(world: World, idx: np.ndarray) -> dict[str, Any]:
    """Pre-state of a set of thermostats (stored on the execution before a command)."""
    m = ensure_state(world)
    vals, cnt = np.unique(np.round(world.offset_c[idx], 1), return_counts=True)
    return {
        "devices": int(idx.size),
        "online": int((~m[SYSTEM]["offline"][idx]).sum()),
        "offsets": [{"offset_c": float(v), "devices": int(c)} for v, c in zip(vals, cnt, strict=True)],
        "precool_active": int(world.precool_active[idx].sum()),
        "setback_active": int(world.setback_active[idx].sum()),
        "overridden": int(world.overridden[idx].sum()),
    }


# ---------------------------------------------------------------- persistence

def persist_ops(world: World) -> dict[str, list[UpdateOne]]:
    m = world.mock
    ts = sim_iso(world.sim_now)
    devs = [
        UpdateOne(
            {"_id": f"{world.run_id}:{device_id(SYSTEM, i)}"},
            {"$set": {"run_id": world.run_id, "sim_ts": ts, **device_state(world, i)}},
            upsert=True,
        )
        for i in sorted(m["dirty"][SYSTEM])
    ]
    m["dirty"][SYSTEM].clear()
    batches = []
    for cid in [c for c in m["dirty"]["commands"] if c.startswith("tlb_")]:
        cmd = m["commands"][cid]
        doc = {"run_id": world.run_id, "sim_ts": ts, "created_sim_ts": cmd.created_sim_ts, **status(world, cmd)}
        doc.pop("batch_id")
        doc["device_ids"] = [device_id(SYSTEM, int(i)) for i in cmd.idx]
        doc["target"] = cmd.params.get("target")
        batches.append(UpdateOne({"_id": cid}, {"$set": doc}, upsert=True))
    return {MOCK_THERMLINK_DEVICES: devs, MOCK_THERMLINK_BATCHES: batches}


# ---------------------------------------------------------------- routes

@router.post("/events")
async def post_events(body: EventIn, ctx: MockCtx = ctx_dep, key: str = key_dep):
    return await ctx.once(key, lambda: create_event(ctx.world, body))


@router.post("/events/{batch_id}/end")
async def post_end(batch_id: str, body: EndIn | None = None, ctx: MockCtx = ctx_dep, key: str = key_dep):
    return await ctx.once(key, lambda: end_event(ctx.world, batch_id, body or EndIn()))


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str, ctx: MockCtx = ctx_dep):
    def read() -> dict[str, Any]:
        cmd = get_command(ctx.world, batch_id)
        if cmd.system != SYSTEM:
            raise MockError(404, "not_found", batch_id)
        return status(ctx.world, cmd)

    return await ctx.read(read)


@router.get("/devices/{dev_id}")
async def get_device(dev_id: str, ctx: MockCtx = ctx_dep):
    def read() -> dict[str, Any]:
        idx, _ = parse_device_ids(ctx.world, [dev_id], ctx.world.has_thermostat)
        if idx.size == 0:
            raise MockError(404, "not_found", dev_id)
        return device_state(ctx.world, int(idx[0]))

    return await ctx.read(read)
