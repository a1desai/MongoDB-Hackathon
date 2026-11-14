"""TextLine: fictional SMS gateway (PLAN §7.7).

Messages are delivered on the next tick. A recipient who replied STOP is opted out at the gateway
level (sends to them are rejected), on top of the Policy Gate's own R02/R06 checks.

Deliveries to featured residents and businesses are queued on `world.mock["textline"]["delivered"]`
(list of {to, body, offer, request_id, batch_id, sim_ts, resident_id?}) for the world agents to
drain. `world.mock["textline"]["last_delivered_idx"]` holds the household indices delivered this
tick, for sim/behavior.py.
"""

import re
from typing import Any

import numpy as np
from fastapi import APIRouter, Depends
from pydantic import BaseModel
from pymongo import UpdateOne

from ..db import MOCK_TEXTLINE_OUTBOX
from ..sim.world_state import World
from ..timeutil import sim_iso
from .common import (
    APPLIED,
    PENDING,
    REJECTED,
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

SYSTEM = "textline"
MAX_CHARS = 320
STOP_RE = re.compile(r"^\s*(stop|stopall|unsubscribe|cancel|end|quit)\b", re.IGNORECASE)

router = APIRouter(prefix="/mock/textline/v1", tags=["mock:textline"])
ctx_dep = Depends(context(SYSTEM))
key_dep = Depends(idempotency_key)


class SmsIn(BaseModel):
    to: str  # household id (hh_00042) or business id (biz_...)
    body: str
    offer: dict[str, Any] | None = None


class MessagesIn(BaseModel):
    messages: list[SmsIn]
    request_id: str | None = None


def _resolve(world: World, to: str) -> int | None:
    """Household index, -1 for a business, None if unknown."""
    if to in world.businesses:
        return -1
    if to.startswith("hh_"):
        try:
            i = world.hh_index(to)
        except ValueError:
            return None
        return i if 0 <= i < world.n else None
    return None


def create_batch(world: World, body: MessagesIn) -> dict[str, Any]:
    if not body.messages:
        raise MockError(422, "empty", "messages must not be empty")
    idx: list[int] = []
    reasons: list[str | None] = []
    for msg in body.messages:
        i = _resolve(world, msg.to)
        reason = None
        if i is None:
            reason = "unknown_recipient"
        elif len(msg.body) > MAX_CHARS:
            reason = "too_long"
        elif i >= 0 and world.sms_stopped[i]:
            reason = "opted_out"
        idx.append(-2 if i is None else i)
        reasons.append(reason)
    cmd = new_command(
        world,
        SYSTEM,
        "send",
        np.asarray(idx, dtype=np.int64),
        latency=1,
        request_id=body.request_id,
        extra={"messages": [m.model_dump(exclude_none=True) for m in body.messages], "reasons": reasons},
    )
    rej = np.asarray([r is not None for r in reasons])
    cmd.state[rej] = REJECTED
    rejected = [{"to": m.to, "reason": r} for m, r in zip(body.messages, reasons, strict=True) if r]
    return {
        "batch_id": cmd.id,
        "status": "queued",
        "queued": int((~rej).sum()),
        "rejected": rejected[:50],
        "rejected_count": len(rejected),
    }


def record_reply(world: World, from_id: str, body: str) -> bool:
    """Inbound SMS from a resident (carrier webhook). A STOP reply opts them out; returns True then."""
    ensure_state(world)
    i = _resolve(world, from_id)
    if i is None or i < 0 or not STOP_RE.match(body or ""):
        return False
    world.sms_stopped[i] = True
    return True


def process_inbound(world: World) -> list[dict[str, Any]]:
    """Drain world.mock["textline"]["inbound"] ({from, body} dicts appended by world agents)."""
    tx = ensure_state(world)[SYSTEM]
    queue, tx["inbound"] = tx["inbound"], []
    stopped = [m["from"] for m in queue if record_reply(world, m.get("from", ""), m.get("body", ""))]
    if not stopped:
        return []
    return [world_event(f"TextLine: {len(stopped)} STOP repl(ies) opted out", system=SYSTEM, opted_out=stopped[:50])]


def step(world: World, cmd: Command) -> list[dict[str, Any]]:
    m = ensure_state(world)
    tx = m[SYSTEM]
    due = np.flatnonzero((cmd.state == PENDING) & (cmd.apply_tick <= world.tick))
    if not due.size:
        cmd.finished = not (cmd.state == PENDING).any()
        return []
    # A STOP received after queueing but before delivery still wins.
    hh_slots = due[cmd.idx[due] >= 0]
    late_stop = hh_slots[world.sms_stopped[cmd.idx[hh_slots]]]
    cmd.state[late_stop] = REJECTED
    for s in late_stop:
        cmd.extra["reasons"][s] = "opted_out"
    due = due[cmd.state[due] == PENDING]
    cmd.state[due] = APPLIED
    cmd.finished = True
    m["dirty"]["commands"].add(cmd.id)

    hh = cmd.idx[due]
    hh = hh[hh >= 0]
    np.add.at(world.sms_count_today, hh, 1)
    tx["last_delivered_idx"] = np.concatenate([tx["last_delivered_idx"], hh])

    featured_by_idx = {v: k for k, v in world.featured.items()}
    ts = sim_iso(world.sim_now)
    notable = []
    for s in due:
        msg = cmd.extra["messages"][s]
        i = int(cmd.idx[s])
        if i == -1:
            biz = world.businesses[msg["to"]]
            biz["last_request_delivered_sim_ts"] = ts
            biz["last_request_id"] = cmd.request_id
            if biz.get("status") == "accepted":
                biz["status"] = "dispatched"
                biz["dispatched_sim_ts"] = ts
        elif i not in featured_by_idx:
            continue
        item = {**msg, "request_id": cmd.request_id, "batch_id": cmd.id, "sim_ts": ts}
        if i >= 0:
            item["resident_id"] = featured_by_idx[i]
        notable.append(item)
    tx["delivered"].extend(notable)
    return [
        world_event(
            f"TextLine delivered {due.size:,} message(s) (batch {cmd.id})",
            system=SYSTEM,
            command_id=cmd.id,
            request_id=cmd.request_id,
            delivered=int(due.size),
            notable=[{"to": n["to"], "resident_id": n.get("resident_id")} for n in notable[:25]],
        )
    ]


def status(world: World, cmd: Command) -> dict[str, Any]:
    c = counts(cmd)
    c["overridden"] = 0
    c["delivered"] = c["applied"]
    return {"batch_id": cmd.id, "kind": cmd.kind, "finished": cmd.finished, "request_id": cmd.request_id, **c}


def snapshot(world: World, to: list[str]) -> dict[str, Any]:
    idx = np.asarray([i for t in to if (i := _resolve(world, t)) is not None and i >= 0], dtype=np.int64)
    return {
        "recipients": len(to),
        "households": int(idx.size),
        "stopped": int(world.sms_stopped[idx].sum()) if idx.size else 0,
        "already_messaged_today": int((world.sms_count_today[idx] > 0).sum()) if idx.size else 0,
    }


def persist_ops(world: World) -> dict[str, list[UpdateOne]]:
    m = world.mock
    ts = sim_iso(world.sim_now)
    ops = []
    for cid in [c for c in m["dirty"]["commands"] if c.startswith("txb_")]:
        cmd = m["commands"][cid]
        names = {APPLIED: "delivered", PENDING: "queued", REJECTED: "rejected"}
        msgs = [
            {**msg, "state": names.get(int(st), "failed"), "reason": r}
            for msg, st, r in zip(cmd.extra["messages"], cmd.state, cmd.extra["reasons"], strict=True)
        ]
        doc = {"run_id": world.run_id, "sim_ts": ts, "created_sim_ts": cmd.created_sim_ts, **status(world, cmd)}
        doc.pop("batch_id")
        doc["messages"] = msgs
        ops.append(UpdateOne({"_id": cid}, {"$set": doc}, upsert=True))
    return {MOCK_TEXTLINE_OUTBOX: ops}


@router.post("/messages")
async def post_messages(body: MessagesIn, ctx: MockCtx = ctx_dep, key: str = key_dep):
    return await ctx.once(key, lambda: create_batch(ctx.world, body))


@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str, ctx: MockCtx = ctx_dep):
    def read() -> dict[str, Any]:
        cmd = get_command(ctx.world, batch_id)
        if cmd.system != SYSTEM:
            raise MockError(404, "not_found", batch_id)
        return status(ctx.world, cmd)

    return await ctx.read(read)
