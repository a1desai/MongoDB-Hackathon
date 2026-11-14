"""Agent memory retrieval on Atlas.

- similar_events: $vectorSearch over event_memory (Planner's "what worked last time").
- policy_snippets: $vectorSearch over policy_docs (Auditor's rule explanations).
- ledger_search: hybrid $rankFusion of Atlas Search ($search) and $vectorSearch over ledger.summary.

EMBED_MODE=auto queries autoEmbed indexes with `query: {text}` (Atlas embeds the query with
voyage-4). EMBED_MODE=client embeds the query with Voyage and uses `queryVector`.
Every function degrades to a $text query, then to [] — none of them ever raises.
"""

import logging
import re
from typing import Any

from pymongo.errors import PyMongoError

from app import db as dbm
from app.embeddings import voyage
from app.memory.search_indexes import LEDGER_VEC, embed_mode
from app.models import SimilarEvent

log = logging.getLogger(__name__)

_STOP = {"the", "and", "for", "was", "who", "why", "what", "when", "how", "did", "with", "that", "this", "from", "are", "were"}


async def _vector_stage(index: str, text_path: str, query_text: str, k: int, flt: dict | None) -> dict[str, Any] | None:
    """Build a $vectorSearch stage for the active embed mode, or None if the query can't be embedded."""
    stage: dict[str, Any] = {"index": index, "numCandidates": max(100, k * 20), "limit": k}
    if flt:
        stage["filter"] = flt
    if embed_mode() == "auto":
        stage.update(path=text_path, query={"text": query_text})
    else:
        if not voyage.available():
            return None
        try:
            stage.update(path="embedding", queryVector=await voyage.embed_query(query_text))
        except voyage.EmbeddingUnavailable as e:
            log.warning("query embedding failed: %s", e)
            return None
    return {"$vectorSearch": stage}


async def _aggregate(coll: str, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    cursor = await dbm.db()[coll].aggregate(pipeline)
    return await cursor.to_list(length=None)


async def _vector_query(coll: str, index: str, text_path: str, query_text: str, k: int, flt: dict | None, project: dict) -> list[dict]:
    """$vectorSearch with a retry using the string `query` form (Automated Embedding is in Preview)."""
    stage = await _vector_stage(index, text_path, query_text, k, flt)
    if stage is None:
        return []
    tail = [{"$project": {**project, "score": {"$meta": "vectorSearchScore"}}}]
    try:
        return await _aggregate(coll, [stage, *tail])
    except PyMongoError as e:
        if "query" not in stage["$vectorSearch"]:
            raise
        log.debug("$vectorSearch with query.text failed (%s); retrying with string query", e)
        stage["$vectorSearch"]["query"] = query_text
        return await _aggregate(coll, [stage, *tail])


async def _text_query(coll: str, query_text: str, k: int, flt: dict | None, project: dict) -> list[dict]:
    """Lexical fallback via the classic text index (works on any MongoDB)."""
    match: dict[str, Any] = {"$text": {"$search": query_text}, **(flt or {})}
    pipeline = [
        {"$match": match},
        {"$addFields": {"score": {"$meta": "textScore"}}},
        {"$sort": {"score": -1}},
        {"$limit": k},
        {"$project": {**project, "score": 1}},
    ]
    return await _aggregate(coll, pipeline)


async def _search(coll: str, index: str, text_path: str, query_text: str, k: int, flt: dict | None, project: dict) -> list[dict]:
    if not query_text.strip():
        return []
    try:
        rows = await _vector_query(coll, index, text_path, query_text, k, flt, project)
        if rows:
            return rows
    except PyMongoError as e:
        log.debug("$vectorSearch on %s unavailable: %s", coll, e)
    try:
        return await _text_query(coll, query_text, k, flt, project)
    except PyMongoError as e:
        log.warning("text fallback on %s failed: %s", coll, e)
        return []


async def similar_events(query_text: str, k: int = 3) -> list[SimilarEvent]:
    """Top-k past heat events most similar to the query (Planner memory)."""
    try:
        rows = await _search(
            dbm.EVENT_MEMORY, dbm.EVENT_MEMORY_VEC, "summary", query_text, k, {"kind": "heat_event"},
            {"summary": 1, "lesson": 1, "mw_by_action": 1},
        )
        return [
            SimilarEvent(
                id=str(r["_id"]),
                summary=str(r.get("summary", "")),
                lesson=str(r.get("lesson", "")),
                score=float(r.get("score") or 0),
                mw_by_action={str(a): float(v) for a, v in (r.get("mw_by_action") or {}).items() if _is_num(v)},
            )
            for r in rows
        ]
    except Exception as e:  # never break the Planner
        log.warning("similar_events failed: %s", e)
        return []


async def policy_snippets(query_text: str, k: int = 3) -> list[dict]:
    """Top-k policy docs for the query: [{rule_id, title, text, score}] (Auditor context)."""
    try:
        rows = await _search(
            dbm.POLICY_DOCS, dbm.POLICY_DOCS_VEC, "text", query_text, k, None,
            {"_id": 0, "rule_id": 1, "title": 1, "text": 1},
        )
        return [
            {"rule_id": r.get("rule_id"), "title": r.get("title", ""), "text": r.get("text", ""), "score": float(r.get("score") or 0)}
            for r in rows
        ]
    except Exception as e:
        log.warning("policy_snippets failed: %s", e)
        return []


_LEDGER_PROJECT = {"embedding": 0}


async def ledger_search(run_id: str, query_text: str, k: int = 10) -> list[dict]:
    """Ledger entries of a run matching the query, best first.

    Hybrid: $rankFusion(Atlas Search on summary, $vectorSearch on summary) -> Atlas Search only
    -> $text -> $regex on keywords -> []. Each doc gets `score` and `retrieval` (which path served it).
    """
    if not query_text.strip():
        return []
    try:
        for name, fn in (("hybrid", _ledger_hybrid), ("search", _ledger_fulltext), ("text", _ledger_text), ("regex", _ledger_regex)):
            try:
                rows = await fn(run_id, query_text, k)
            except PyMongoError as e:
                log.debug("ledger_search %s path failed: %s", name, e)
                continue
            if rows:
                return [_clean({**r, "retrieval": name}) for r in rows]
        return []
    except Exception as e:
        log.warning("ledger_search failed: %s", e)
        return []


def _search_stage(run_id: str, query_text: str) -> dict[str, Any]:
    return {
        "$search": {
            "index": dbm.LEDGER_SEARCH,
            "compound": {
                "must": [{"text": {"query": query_text, "path": "summary", "fuzzy": {"maxEdits": 1}}}],
                "filter": [{"equals": {"path": "run_id", "value": run_id}}],
            },
        }
    }


async def _ledger_hybrid(run_id: str, query_text: str, k: int) -> list[dict]:
    if embed_mode() != "auto":
        return []  # ledger has no stored vectors in client mode
    vec = await _vector_stage(LEDGER_VEC, "summary", query_text, k * 2, {"run_id": run_id})
    if vec is None:
        return []
    pipeline = [
        {
            "$rankFusion": {
                "input": {"pipelines": {"semantic": [vec], "keyword": [_search_stage(run_id, query_text), {"$limit": k * 2}]}},
                "combination": {"weights": {"semantic": 1, "keyword": 1}},
            }
        },
        {"$limit": k},
        {"$project": _LEDGER_PROJECT},
    ]
    rows = await _aggregate(dbm.LEDGER, pipeline)
    # Fused order is the ranking; expose a reciprocal-rank score rather than depend on $meta support.
    return [{**r, "score": 1.0 / (i + 1)} for i, r in enumerate(rows)]


async def _ledger_fulltext(run_id: str, query_text: str, k: int) -> list[dict]:
    pipeline = [
        _search_stage(run_id, query_text),
        {"$limit": k},
        {"$addFields": {"score": {"$meta": "searchScore"}}},
        {"$project": _LEDGER_PROJECT},
    ]
    return await _aggregate(dbm.LEDGER, pipeline)


async def _ledger_text(run_id: str, query_text: str, k: int) -> list[dict]:
    pipeline = [
        {"$match": {"run_id": run_id, "$text": {"$search": query_text}}},
        {"$addFields": {"score": {"$meta": "textScore"}}},
        {"$sort": {"score": -1, "seq": 1}},
        {"$limit": k},
        {"$project": _LEDGER_PROJECT},
    ]
    return await _aggregate(dbm.LEDGER, pipeline)


async def _ledger_regex(run_id: str, query_text: str, k: int) -> list[dict]:
    words = [w for w in re.findall(r"[\w-]{3,}", query_text) if w.lower() not in _STOP]
    if not words:
        return []
    pattern = "|".join(re.escape(w) for w in words[:12])
    pipeline = [
        {"$match": {"run_id": run_id, "summary": {"$regex": pattern, "$options": "i"}}},
        {"$sort": {"seq": 1}},
        {"$limit": k},
        {"$addFields": {"score": 0.0}},
        {"$project": _LEDGER_PROJECT},
    ]
    return await _aggregate(dbm.LEDGER, pipeline)


def _clean(doc: dict[str, Any]) -> dict[str, Any]:
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    if "score" in doc:
        doc["score"] = float(doc["score"] or 0)
    return doc


def _is_num(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)
