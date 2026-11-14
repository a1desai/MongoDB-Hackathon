"""Five mock control systems (PLAN §7.7), each a FastAPI router with its own key and collection.

Only the Executor calls them over HTTP. The engine calls `apply_pending(world)` once per tick
(after physics) and `await persist(world)` to mirror changed state into the mock_* collections.
The verifier reads command state with `command_status`.
"""

import logging
from typing import Any

import numpy as np
from fastapi import APIRouter
from pymongo.errors import PyMongoError

from ..db import db
from ..sim.world_state import World
from . import chargeflow, gridops, powercell, textline, thermlink
from .common import Command, MockError, ensure_state
from .keys import EXECUTOR_KEY, SYSTEM_KEYS, key_for

log = logging.getLogger(__name__)

MODULES = {
    "thermlink": thermlink,
    "chargeflow": chargeflow,
    "powercell": powercell,
    "textline": textline,
    "gridops": gridops,
}

routers: list[APIRouter] = [m.router for m in MODULES.values()]

__all__ = [
    "EXECUTOR_KEY",
    "SYSTEM_KEYS",
    "Command",
    "MockError",
    "apply_pending",
    "command_status",
    "get_command",
    "key_for",
    "persist",
    "routers",
]


def apply_pending(world: World) -> list[dict[str, Any]]:
    """Per tick: apply due commands (latency/failure quirks) and window transitions to the world arrays.

    Returns world-event dicts ({type, summary, payload}) for the engine to log as sim:world.
    """
    m = ensure_state(world)
    m["textline"]["last_delivered_idx"] = np.zeros(0, dtype=np.int64)
    events: list[dict[str, Any]] = []
    events += textline.process_inbound(world)
    for cid in list(m["handles"]):
        cmd: Command = m["commands"][cid]
        if cmd.finished:
            continue
        try:
            events += MODULES[cmd.system].step(world, cmd)
        except Exception:  # a mock bug must never stall the sim
            log.exception("mock %s command %s failed to apply", cmd.system, cid)
            cmd.finished = True
    return events


async def persist(world: World) -> None:
    """bulk_write changed mock state into the mock_* collections (never raises)."""
    m = ensure_state(world)
    ops: dict[str, list] = {}
    for mod in MODULES.values():
        for coll, items in mod.persist_ops(world).items():
            ops.setdefault(coll, []).extend(items)
    m["dirty"]["commands"].clear()
    for coll, items in ops.items():
        if not items:
            continue
        try:
            await db()[coll].bulk_write(items, ordered=False)
        except PyMongoError as e:
            log.warning("persist %s failed: %s", coll, e)


def get_command(world: World, command_id: str) -> Command | None:
    return ensure_state(world)["commands"].get(command_id)


def command_status(world: World, command_id: str) -> dict[str, Any] | None:
    """Read-back of a command: counts {total, pending, applied, offline, failed, ignored, rejected, overridden}."""
    cmd = get_command(world, command_id)
    if cmd is None:
        return None
    return MODULES[cmd.system].status(world, cmd)
