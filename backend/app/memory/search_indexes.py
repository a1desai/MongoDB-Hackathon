"""Atlas Search and Vector Search index definitions, plus idempotent create/update and polling.

EMBED_MODE=auto  -> autoEmbed vector indexes (Atlas Automated Embedding, model voyage-4).
EMBED_MODE=client -> classic vector indexes on a stored `embedding` field (Voyage from Python).
The ledger also gets a full-text Atlas Search index, so the Auditor can run $rankFusion hybrid search.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from pymongo.errors import OperationFailure, PyMongoError
from pymongo.operations import SearchIndexModel

from app import db as dbm
from app.config import get_settings

log = logging.getLogger(__name__)

LEDGER_VEC = "ledger_vec"  # autoEmbed index on ledger.summary (auto mode only)
EMBED_MODEL = "voyage-4"


@dataclass(frozen=True)
class SearchIndexSpec:
    collection: str
    name: str
    type: str  # "vectorSearch" | "search"
    definition: dict[str, Any]
    text_path: str | None = None  # the embedded text field, for vector indexes

    def model(self) -> SearchIndexModel:
        return SearchIndexModel(definition=self.definition, name=self.name, type=self.type)


def embed_mode() -> str:
    return "client" if get_settings().embed_mode.strip().lower() == "client" else "auto"


def _vector_def(text_path: str, filters: list[str], mode: str) -> dict[str, Any]:
    if mode == "auto":
        vec: dict[str, Any] = {"type": "autoEmbed", "modality": "text", "path": text_path, "model": EMBED_MODEL}
    else:
        vec = {"type": "vector", "path": "embedding", "numDimensions": get_settings().voyage_dim, "similarity": "cosine"}
    return {"fields": [vec, *({"type": "filter", "path": f} for f in filters)]}


LEDGER_SEARCH_DEF: dict[str, Any] = {
    "mappings": {
        "dynamic": False,
        "fields": {
            "summary": {"type": "string", "analyzer": "lucene.english"},
            "run_id": {"type": "token"},
            "type": {"type": "token"},
            "actor": {"type": "token"},
            "seq": {"type": "number"},
        },
    }
}


def specs(mode: str | None = None) -> list[SearchIndexSpec]:
    """All search indexes for the given embed mode (default: settings)."""
    mode = mode or embed_mode()
    out = [
        SearchIndexSpec(dbm.EVENT_MEMORY, dbm.EVENT_MEMORY_VEC, "vectorSearch", _vector_def("summary", ["kind"], mode), "summary"),
        SearchIndexSpec(dbm.POLICY_DOCS, dbm.POLICY_DOCS_VEC, "vectorSearch", _vector_def("text", ["rule_id"], mode), "text"),
        SearchIndexSpec(dbm.LEDGER, dbm.LEDGER_SEARCH, "search", LEDGER_SEARCH_DEF),
    ]
    if mode == "auto":
        # Ledger entries are written by governance/ledger.py without vectors, so the ledger only
        # gets a semantic index when Atlas embeds it for us.
        out.append(SearchIndexSpec(dbm.LEDGER, LEDGER_VEC, "vectorSearch", _vector_def("summary", ["run_id", "type"], mode), "summary"))
    return out


def _vector_kind(definition: dict[str, Any] | None) -> str | None:
    for f in (definition or {}).get("fields", []):
        if f.get("type") in ("autoEmbed", "vector"):
            return f["type"]
    return None


def _search_fields(definition: dict[str, Any] | None) -> set[str]:
    # Atlas normalizes stored definitions, so compare mapped field names, not whole documents.
    return set(((definition or {}).get("mappings") or {}).get("fields", {}))


async def search_supported() -> bool:
    """True when the server runs Atlas Search (mongot). False on plain mongod."""
    try:
        coll = dbm.db()[dbm.LEDGER]
        cursor = await coll.list_search_indexes()
        await cursor.to_list(length=1)
        return True
    except (OperationFailure, PyMongoError) as e:
        log.warning("Atlas Search is not available on this server (%s); skipping search indexes", _short(e))
        return False


async def ensure_search_indexes(mode: str | None = None) -> dict[str, str]:
    """Create missing search indexes and update changed ones. Returns {name: action}."""
    database = dbm.db()
    result: dict[str, str] = {}
    for spec in specs(mode):
        coll = database[spec.collection]
        try:
            existing = {i["name"]: i async for i in await coll.list_search_indexes()}
            current = existing.get(spec.name)
            if current is None:
                await coll.create_search_index(spec.model())
                result[spec.name] = "created"
            elif current.get("type", "search") != spec.type:
                await coll.drop_search_index(spec.name)
                result[spec.name] = "dropped (type changed); re-run init_db to recreate"
            elif spec.type == "vectorSearch" and _vector_kind(current.get("latestDefinition")) != _vector_kind(spec.definition):
                await coll.update_search_index(spec.name, spec.definition)
                result[spec.name] = "updated (embed mode changed)"
            elif spec.type == "search" and _search_fields(current.get("latestDefinition")) != _search_fields(spec.definition):
                await coll.update_search_index(spec.name, spec.definition)
                result[spec.name] = "updated"
            else:
                result[spec.name] = "exists"
        except (OperationFailure, PyMongoError) as e:
            log.warning("search index %s on %s failed: %s", spec.name, spec.collection, _short(e))
            result[spec.name] = f"failed: {_short(e)}"
    return result


async def wait_queryable(names: list[str] | None = None, timeout_s: float = 180, mode: str | None = None) -> dict[str, str]:
    """Poll list_search_indexes() until each index is queryable or the timeout passes."""
    database = dbm.db()
    wanted = [s for s in specs(mode) if names is None or s.name in names]
    status: dict[str, str] = {s.name: "unknown" for s in wanted}
    deadline = time.monotonic() + timeout_s
    while True:
        pending = 0
        for spec in wanted:
            if status[spec.name] == "READY":
                continue
            try:
                cursor = await database[spec.collection].list_search_indexes(spec.name)
                info = next(iter(await cursor.to_list(length=1)), None)
            except PyMongoError as e:
                status[spec.name] = f"error: {_short(e)}"
                continue
            if info is None:
                status[spec.name] = "missing"
                pending += 1
            elif info.get("queryable") and info.get("status") in ("READY", None):
                status[spec.name] = "READY"
            else:
                status[spec.name] = str(info.get("status", "PENDING"))
                if status[spec.name] == "FAILED":
                    log.warning("search index %s FAILED: %s", spec.name, info.get("message") or info)
                else:
                    pending += 1
        if pending == 0 or time.monotonic() >= deadline:
            break
        await asyncio.sleep(5)
    for name, st in status.items():
        if st != "READY":
            log.warning("search index %s not queryable yet (%s); retrieval falls back to $text until it is", name, st)
    return status


def _short(e: Exception) -> str:
    msg = getattr(e, "details", None)
    if isinstance(msg, dict) and msg.get("errmsg"):
        return str(msg["errmsg"])[:200]
    return str(e)[:200]
