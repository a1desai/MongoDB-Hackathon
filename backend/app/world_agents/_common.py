"""Shared helpers for world agents and the Negotiator/Auditor: static lookups, message storage,
ledger logging and deterministic pseudo-randomness. Every helper degrades instead of raising."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from typing import Any

from ..config import get_settings
from ..db import ARCHETYPES, BUSINESSES, MESSAGES, RESIDENTS, db
from ..models import LedgerEntry, Message
from ..sim.world_state import World
from ..timeutil import sim_iso

log = logging.getLogger(__name__)

DB_TIMEOUT_S = 3.0

_static: dict[str, dict[str, dict[str, Any]]] = {}


def det_uniform(*parts: Any) -> float:
    """Deterministic U[0,1) from the parts (keeps fallbacks reproducible, leaves world.rng alone)."""
    h = hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()
    return int(h[:12], 16) / float(16**12)


def now_ts(world: World) -> str:
    return sim_iso(world.sim_now)


_static_failed_at: dict[str, float] = {}
RETRY_STATIC_S = 60.0


async def _load_static(collection: str) -> dict[str, dict[str, Any]]:
    """id -> doc for a small static collection; Atlas first, then data/processed/<name>.json."""
    if collection in _static:
        return _static[collection]
    if time.monotonic() - _static_failed_at.get(collection, -1e9) < RETRY_STATIC_S:
        return {}
    docs: dict[str, dict[str, Any]] = {}
    try:
        cursor = db()[collection].find({}, {"geometry": 0})
        rows = await asyncio.wait_for(cursor.to_list(length=None), DB_TIMEOUT_S)
        docs = {str(d["_id"]): d for d in rows}
    except Exception as e:  # noqa: BLE001
        log.warning("loading %s from Atlas failed: %s", collection, e)
    if not docs:
        path = get_settings().data_dir / "processed" / f"{collection}.json"
        try:
            rows = json.loads(path.read_text(encoding="utf-8"))
            docs = {str(d["_id"]): d for d in rows}
        except Exception:  # noqa: BLE001
            docs = {}
    if docs:
        _static[collection] = docs
    else:
        _static_failed_at[collection] = time.monotonic()
    return docs


async def residents() -> dict[str, dict[str, Any]]:
    return await _load_static(RESIDENTS)


async def archetypes() -> dict[str, dict[str, Any]]:
    return await _load_static(ARCHETYPES)


async def businesses() -> dict[str, dict[str, Any]]:
    return await _load_static(BUSINESSES)


def featured_by_index(world: World) -> dict[int, str]:
    """household index -> featured resident id."""
    return {int(idx): rid for rid, idx in world.featured.items()}


async def thread_tail(run_id: str, thread_id: str, n: int = 5) -> list[dict[str, Any]]:
    """Last n messages of a thread, oldest first."""
    try:
        cursor = db()[MESSAGES].find({"run_id": run_id, "thread_id": thread_id}).sort("sim_ts", -1).limit(n)
        rows = await asyncio.wait_for(cursor.to_list(length=n), DB_TIMEOUT_S)
        return list(reversed(rows))
    except Exception as e:  # noqa: BLE001
        log.warning("reading thread %s failed: %s", thread_id, e)
        return []


async def store_message(msg: Message) -> None:
    try:
        await asyncio.wait_for(db()[MESSAGES].insert_one(msg.to_mongo()), DB_TIMEOUT_S)
    except Exception as e:  # noqa: BLE001
        log.warning("storing message %s failed: %s", msg.id, e)


async def ledger_append(run_id: str, type: str, actor: str, summary: str, *, sim_ts: str,
                        acting_for: str | None = None, refs: dict | None = None,
                        payload: dict | None = None,
                        langsmith_run_id: str | None = None) -> LedgerEntry | None:
    """governance/ledger.append(), the only ledger writer; logs and returns None on failure."""
    try:
        from ..governance.ledger import append
    except Exception as e:  # noqa: BLE001
        log.warning("governance.ledger unavailable (%s); ledger entry skipped: %s", e, summary)
        return None
    try:
        return await append(run_id, type, actor, summary, sim_ts=sim_ts, acting_for=acting_for,
                            refs=refs, payload=payload, langsmith_run_id=langsmith_run_id)
    except Exception as e:  # noqa: BLE001
        log.warning("ledger append (%s) failed: %s", type, e)
        return None


def clip_text(text: str, n: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: max(0, n - 1)].rstrip() + "…"
