"""The append-only, hash-chained ledger (PLAN §10.5). `append()` is the ONLY writer of `ledger`.

    seq            = counters["ledger:<run_id>"].seq, via find_one_and_update($inc)
    payload_digest = sha256(canonical_json(payload))
    hash           = sha256(canonical_json({seq, prev_hash, run_id, sim_ts, type, actor,
                                            acting_for, refs, summary, payload_digest}))
    prev_hash      = "0"*64 for seq 1

Appends for a run are serialized by an asyncio.Lock, so many coroutines can append at once and
the chain stays gap-free. Payloads are normalized to plain JSON before hashing and storing, so what
`verify_chain` reads back hashes to exactly what was written.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np
from pydantic import BaseModel
from pymongo import ReturnDocument

from app import db as dbm
from app.models import LedgerEntry, LedgerRefs
from app.timeutil import sim_iso, wall_now

GENESIS = "0" * 64
HASH_FIELDS = ("seq", "prev_hash", "run_id", "sim_ts", "type", "actor", "acting_for", "refs", "summary", "payload_digest")

_locks: dict[tuple[int, str], asyncio.Lock] = {}
_heads: dict[str, tuple[int, str]] = {}  # run_id -> (seq, hash) of the last entry this process wrote


# ---------------------------------------------------------------- hashing


def _default(o: Any) -> Any:
    if isinstance(o, BaseModel):
        return o.model_dump(by_alias=True, exclude_none=True)
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, datetime):
        return sim_iso(o)
    if isinstance(o, (set, frozenset)):
        return sorted(o, key=str)
    if isinstance(o, Enum):
        return o.value
    return str(o)


def _clean_floats(o: Any) -> Any:
    """NaN/inf are not JSON; store them as null so the stored and hashed forms agree."""
    if isinstance(o, float):
        return o if math.isfinite(o) else None
    if isinstance(o, dict):
        return {str(k): _clean_floats(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_clean_floats(v) for v in o]
    return o


def canonical_json(obj: Any) -> str:
    """Deterministic JSON: sorted keys, no whitespace, UTF-8, numpy/datetime/pydantic coerced."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default, allow_nan=False)


def normalize(obj: Any) -> Any:
    """Round-trip through canonical JSON so the stored value is exactly what gets hashed."""
    return json.loads(json.dumps(_clean_floats(json.loads(json.dumps(obj, default=_default))), default=_default))


def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def payload_digest(payload: dict) -> str:
    return sha256_hex(canonical_json(payload))


def entry_hash(fields: dict[str, Any]) -> str:
    return sha256_hex(canonical_json({k: fields.get(k) for k in HASH_FIELDS}))


def _refs(refs: dict | LedgerRefs | None) -> dict[str, Any]:
    if isinstance(refs, LedgerRefs):
        refs = refs.model_dump(exclude_none=True)
    return normalize({k: v for k, v in (refs or {}).items() if v is not None})


# ---------------------------------------------------------------- append


def _lock(run_id: str) -> asyncio.Lock:
    key = (id(asyncio.get_running_loop()), run_id)
    lock = _locks.get(key)
    if lock is None:
        lock = _locks[key] = asyncio.Lock()
    return lock


def _counter_id(run_id: str) -> str:
    return f"ledger:{run_id}"


def entry_id(run_id: str, seq: int) -> str:
    return f"{run_id}:{seq:06d}"


async def _next_seq_and_prev(run_id: str) -> tuple[int, str]:
    """Reserve the next seq and find the previous hash. Self-heals a counter that drifted from
    the ledger (e.g. a reset database), so the chain never gets a gap."""
    database = dbm.db()
    doc = await database[dbm.COUNTERS].find_one_and_update(
        {"_id": _counter_id(run_id)}, {"$inc": {"seq": 1}}, upsert=True, return_document=ReturnDocument.AFTER
    )
    seq = int(doc["seq"])
    head = _heads.get(run_id)
    if head is not None and head[0] == seq - 1:
        return seq, head[1]
    last = await database[dbm.LEDGER].find_one({"run_id": run_id}, {"seq": 1, "hash": 1}, sort=[("seq", -1)])
    want = int(last["seq"]) + 1 if last else 1
    if want != seq:
        await database[dbm.COUNTERS].update_one({"_id": _counter_id(run_id)}, {"$set": {"seq": want}}, upsert=True)
        seq = want
    return seq, (last["hash"] if last else GENESIS)


async def append(
    run_id: str,
    type: str,
    actor: str,
    summary: str,
    *,
    sim_ts: str,
    acting_for: str | None = None,
    refs: dict | None = None,
    payload: dict | None = None,
    langsmith_run_id: str | None = None,
) -> LedgerEntry:
    """Append one entry to the run's hash chain and return it. The only ledger writer."""
    clean_payload = normalize(payload or {})
    clean_refs = _refs(refs)
    digest = payload_digest(clean_payload)
    summary = str(summary)
    async with _lock(run_id):
        seq, prev = await _next_seq_and_prev(run_id)
        fields = {
            "seq": seq,
            "prev_hash": prev,
            "run_id": run_id,
            "sim_ts": sim_ts,
            "type": type,
            "actor": actor,
            "acting_for": acting_for,
            "refs": clean_refs,
            "summary": summary,
            "payload_digest": digest,
        }
        h = entry_hash(fields)
        doc = {
            "_id": entry_id(run_id, seq),
            **fields,
            "wall_ts": wall_now(),
            "payload": clean_payload,
            "hash": h,
            "langsmith_run_id": langsmith_run_id,
        }
        try:
            await dbm.db()[dbm.LEDGER].insert_one(doc)
        except Exception:
            # Give the seq back so the next append does not leave a gap.
            await dbm.db()[dbm.COUNTERS].update_one({"_id": _counter_id(run_id), "seq": seq}, {"$inc": {"seq": -1}})
            _heads.pop(run_id, None)
            raise
        _heads[run_id] = (seq, h)
    return LedgerEntry.model_validate({k: v for k, v in doc.items() if k != "_id"})


# ---------------------------------------------------------------- verify


async def verify_chain(run_id: str) -> dict[str, Any]:
    """Recompute the whole chain. Returns {ok, checked, first_broken_seq, reason, head_hash}."""
    checked = 0
    expected_seq = 1
    prev = GENESIS
    cursor = dbm.db()[dbm.LEDGER].find({"run_id": run_id}, {"_id": 0}, sort=[("seq", 1)])
    async for doc in cursor:
        seq = int(doc.get("seq", -1))
        reason = None
        if seq != expected_seq:
            reason = f"expected seq {expected_seq}, found {seq} (gap or duplicate)"
            seq = expected_seq if seq > expected_seq else seq
        elif doc.get("prev_hash") != prev:
            reason = "prev_hash does not match the previous entry's hash"
        elif payload_digest(doc.get("payload") or {}) != doc.get("payload_digest"):
            reason = "payload does not match payload_digest"
        elif entry_hash(doc) != doc.get("hash"):
            reason = "entry fields do not match hash"
        if reason:
            return {"ok": False, "checked": checked, "first_broken_seq": seq, "reason": reason, "head_hash": prev}
        prev = doc["hash"]
        checked += 1
        expected_seq += 1
    return {"ok": True, "checked": checked, "first_broken_seq": None, "reason": None, "head_hash": prev}
