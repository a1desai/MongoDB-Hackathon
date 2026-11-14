"""Seed agent memory in Atlas. Run from backend/: `.venv/bin/python -m scripts.seed_memory`.

- event_memory: 30 past heat events from data/processed/event_memory_seed.json (_id mem_0001..).
- policy_docs: one plain-language doc per Gate rule from app/governance/seed.py (_id pd_R01..).

EMBED_MODE=auto: documents are stored as text only; the autoEmbed Vector Search index embeds them
in Atlas (voyage-4). EMBED_MODE=client: summaries/texts are embedded with Voyage
(input_type "document") into `embedding`. Idempotent: upserts by _id and removes stale docs.
"""

import asyncio
import json
import logging
import random
import sys
from typing import Any

from pymongo import ReplaceOne

from app import db as dbm
from app.config import get_settings
from app.embeddings import voyage
from app.governance.seed import POLICY_DOCS
from app.memory import search_indexes

log = logging.getLogger("seed_memory")


def load_events() -> list[dict[str, Any]]:
    path = get_settings().data_dir / "processed" / "event_memory_seed.json"
    if path.exists():
        events = json.loads(path.read_text())
    else:
        log.warning("%s missing; using built-in synthetic events (run scripts.build_world for the real seed)", path)
        events = synthetic_events()
    out = []
    for i, e in enumerate(events, start=1):
        doc = dict(e)
        doc["_id"] = str(doc.get("_id") or f"mem_{i:04d}")
        doc.setdefault("kind", "heat_event")
        doc.pop("embedding", None)
        out.append(doc)
    return out


def synthetic_events(n: int = 30, seed: int = 42) -> list[dict[str, Any]]:
    """Fallback only: seeded template events with the PLAN §5.3 shape (no real data)."""
    rng = random.Random(seed)
    actions = ["precool", "battery_discharge", "ev_pause", "business_curtail_request", "thermostat_setback", "send_sms", "feeder_outage"]
    lessons = [
        "Pre-cooling two hours ahead flattened the 17:00 peak more than late setbacks did.",
        "EV pauses delivered reliably because most sessions start after 17:30.",
        "Setbacks in window-AC high-rises produced many overrides; target central-AC houses instead.",
        "Short rotating outages were avoided once batteries were dispatched before the peak.",
        "SMS offers had low uptake after 19:00; send them before 16:00.",
        "Staggered restores prevented a rebound peak after the event window closed.",
    ]
    out = []
    for i in range(n):
        year = 2019 + i % 11
        temp = round(rng.uniform(31.0, 38.0), 1)
        short = round(rng.uniform(40, 420), 0)
        used = sorted(rng.sample(actions[:-1], rng.randint(2, 4)), key=actions.index)
        if short > 330:
            used.append("feeder_outage")
        mw = {a: round(short * rng.uniform(0.1, 0.45), 1) for a in used}
        lesson = rng.choice(lessons)
        outcome = "no outages" if "feeder_outage" not in used else f"{rng.randint(2, 5)} rotating outages of 30-60 min"
        out.append(
            {
                "kind": "heat_event",
                "date": f"{year}-{rng.choice(['06', '07', '08'])}-{rng.randint(1, 28):02d}",
                "max_temp_c": temp,
                "shortfall_mw": short,
                "actions": used,
                "mw_by_action": mw,
                "optout_by_archetype": {},
                "outcome": outcome,
                "lesson": lesson,
                "summary": (
                    f"Heat event reaching {temp} C with a projected shortfall of {short:.0f} MW. "
                    f"Operators used {', '.join(used)}. Outcome: {outcome}. {lesson}"
                ),
            }
        )
    return out


async def _embed_into(docs: list[dict[str, Any]], field: str) -> int:
    if search_indexes.embed_mode() != "client":
        return 0
    if not voyage.available():
        log.warning("EMBED_MODE=client but VOYAGE_API_KEY is empty; storing %d docs without embeddings", len(docs))
        return 0
    try:
        vecs = await voyage.embed([d[field] for d in docs], "document")
    except voyage.EmbeddingUnavailable as e:
        log.warning("Voyage embedding failed (%s); storing without embeddings", e)
        return 0
    for d, v in zip(docs, vecs, strict=True):
        d["embedding"] = v
    return len(vecs)


async def upsert_all(coll_name: str, docs: list[dict[str, Any]]) -> None:
    coll = dbm.db()[coll_name]
    if docs:
        await coll.bulk_write([ReplaceOne({"_id": d["_id"]}, d, upsert=True) for d in docs], ordered=False)
    await coll.delete_many({"_id": {"$nin": [d["_id"] for d in docs]}})


async def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    if not get_settings().mongodb_uri:
        log.error("MONGODB_URI is not set")
        return 2
    events = load_events()
    docs = [dict(d) for d in POLICY_DOCS]
    n_ev = await _embed_into(events, "summary")
    n_pd = await _embed_into(docs, "text")
    await upsert_all(dbm.EVENT_MEMORY, events)
    await upsert_all(dbm.POLICY_DOCS, docs)
    mode = search_indexes.embed_mode()
    print(f"event_memory: {len(events)} docs ({n_ev} embedded client-side) | policy_docs: {len(docs)} docs ({n_pd} embedded) | embed_mode={mode}")
    if mode == "auto":
        print("Atlas Automated Embedding (voyage-4) embeds these via the autoEmbed indexes; no client vectors needed.")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
