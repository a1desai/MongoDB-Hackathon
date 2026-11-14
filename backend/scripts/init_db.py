"""Idempotent Atlas setup for Cooldown. Run from backend/: `.venv/bin/python -m scripts.init_db`.

1. Create (or collMod) every collection with a $jsonSchema validator (validationAction "error").
2. feeder_load as a time series collection (timeField ts, metaField meta, granularity minutes).
3. PLAN §6.3 indexes: 2dsphere geo, unique {run_id, seq} ledger chain, text, query indexes.
4. Replace the static world collections from data/processed/*.json and scenarios/*.yaml.
5. Upsert identities and versioned policies from app/governance/seed.py.
6. Create Atlas Search + Vector Search indexes (autoEmbed in EMBED_MODE=auto) and poll until
   queryable. On a server without Atlas Search this warns and continues.

Flags: --skip-data, --skip-search, --search-timeout SECONDS, --drop-run-data (clears per-run collections).
"""

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path
from typing import Any

import yaml
from pymongo import ASCENDING, DESCENDING, GEOSPHERE, TEXT, IndexModel, ReplaceOne
from pymongo.errors import BulkWriteError, OperationFailure

from app import db as dbm
from app.config import get_settings
from app.governance.seed import IDENTITIES, POLICIES, POLICY_VERSION
from app.memory import search_indexes

log = logging.getLogger("init_db")

STR = {"bsonType": "string"}
INT = {"bsonType": ["int", "long"]}
NUM = {"bsonType": ["int", "long", "double", "decimal"]}
BOOL = {"bsonType": "bool"}
OBJ = {"bsonType": "object"}
ARR = {"bsonType": "array"}
NULLABLE_STR = {"bsonType": ["string", "null"]}

ACTION_TYPES = [
    "precool", "thermostat_setback", "ev_pause", "battery_discharge", "business_curtail_request",
    "send_sms", "feeder_outage", "feeder_restore", "thermostat_restore", "undo",
]
SYSTEMS = ["thermlink", "chargeflow", "powercell", "textline", "gridops"]
REQUEST_STATUS = [
    "proposed", "denied", "needs_approval", "approved", "executing",
    "verified", "partial", "failed", "rolled_back", "cancelled",
]
LEDGER_TYPES = [
    "plan_created", "request_proposed", "decision", "approval_requested", "approval_decided",
    "execution_sent", "execution_verified", "execution_failed", "rollback_requested",
    "halt", "resume", "message_out", "message_in", "replan_triggered", "contingency",
    "world_event", "audit_query",
]
GEO_POINT = {"bsonType": "object", "required": ["type", "coordinates"], "properties": {"type": {"enum": ["Point"]}, "coordinates": ARR}}
GEO_AREA = {"bsonType": "object", "required": ["type", "coordinates"], "properties": {"type": {"enum": ["Polygon", "MultiPolygon"]}, "coordinates": ARR}}
EVIDENCE = {
    "bsonType": "array",
    "items": {
        "bsonType": "object",
        "required": ["id", "kind", "trust"],
        "properties": {"id": STR, "kind": STR, "trust": {"enum": ["system", "untrusted"]}},
    },
}
SCOPES = {"bsonType": "object", "properties": {"propose": ARR, "execute": ARR, "read": ARR}}


def schema(required: list[str], properties: dict[str, Any]) -> dict[str, Any]:
    return {"$jsonSchema": {"bsonType": "object", "required": required, "properties": properties}}


VALIDATORS: dict[str, dict[str, Any]] = {
    # PLAN §6.2, verbatim.
    dbm.LEDGER: schema(
        ["run_id", "seq", "sim_ts", "wall_ts", "type", "actor", "summary", "prev_hash", "hash"],
        {
            "run_id": STR,
            "seq": {"bsonType": ["int", "long"], "minimum": 1},
            "type": {"enum": LEDGER_TYPES},
            "actor": {"bsonType": "string", "pattern": "^(agent|human|system|sim):"},
            "acting_for": NULLABLE_STR,
            "prev_hash": {"bsonType": "string", "minLength": 64, "maxLength": 64},
            "hash": {"bsonType": "string", "minLength": 64, "maxLength": 64},
        },
    ),
    dbm.ACTION_REQUESTS: schema(
        ["run_id", "action_type", "system", "proposed_by", "acting_for", "evidence", "status", "idempotency_key", "created_sim_ts"],
        {
            "run_id": STR,
            "action_type": {"enum": ACTION_TYPES},
            "system": {"enum": SYSTEMS},
            "proposed_by": {"bsonType": "string", "pattern": "^agent:"},
            "acting_for": {"bsonType": "string", "pattern": "^human:"},
            "evidence": EVIDENCE,
            "status": {"enum": REQUEST_STATUS},
            "idempotency_key": STR,
            "created_sim_ts": STR,
        },
    ),
    dbm.DECISIONS: schema(
        ["request_id", "verdict", "rule_results", "policy_version"],
        {
            "request_id": STR,
            "verdict": {"enum": ["allow", "partial_allow", "deny", "needs_approval"]},
            "rule_results": {
                "bsonType": "array",
                "minItems": 1,
                "items": {"bsonType": "object", "required": ["rule_id", "applies", "passed"], "properties": {"rule_id": STR, "applies": BOOL, "passed": BOOL}},
            },
            "policy_version": STR,
            "decided_by": {"bsonType": "string", "pattern": "^system:"},
        },
    ),
    # Required fields follow app.models (fields without defaults, plus enum-typed defaults).
    dbm.POLICIES: schema(
        ["_id", "version", "name", "description", "params", "applies_to", "effect"],
        {
            "_id": {"bsonType": "string", "pattern": "^R[0-9]{2}$"},
            "version": STR,
            "name": STR,
            "description": STR,
            "params": OBJ,
            "applies_to": ARR,
            "effect": {"enum": ["deny", "needs_approval"]},
            "active": BOOL,
        },
    ),
    dbm.IDENTITIES: schema(
        ["_id", "kind", "display_name", "scopes", "status"],
        {
            "_id": {"bsonType": "string", "pattern": "^(agent|human|system):"},
            "kind": {"enum": ["agent", "human", "system"]},
            "display_name": STR,
            "acting_for": {"bsonType": ["string", "null"], "pattern": "^human:"},
            "scopes": SCOPES,
            "status": {"enum": ["active", "suspended", "revoked"]},
            "on_shift": BOOL,
        },
    ),
    dbm.APPROVALS: schema(
        ["run_id", "request_id", "decision_id", "status", "summary", "requested_sim_ts", "expires_sim_ts"],
        {
            "run_id": STR,
            "request_id": STR,
            "decision_id": STR,
            "status": {"enum": ["pending", "approved", "denied", "expired"]},
            "summary": STR,
            "requested_sim_ts": STR,
            "expires_sim_ts": STR,
            "decided_by": NULLABLE_STR,
        },
    ),
    dbm.EXECUTIONS: schema(
        ["run_id", "request_id", "executed_by", "system", "idempotency_key", "path", "status", "sent_sim_ts", "sent_tick"],
        {
            "run_id": STR,
            "request_id": STR,
            "executed_by": {"bsonType": "string", "pattern": "^agent:"},
            "system": {"enum": SYSTEMS},
            "idempotency_key": STR,
            "path": STR,
            "status": {"enum": ["sent", "verified", "partial", "failed", "rolled_back"]},
            "sent_sim_ts": STR,
            "sent_tick": INT,
            "attempts": INT,
        },
    ),
    dbm.PLANS: schema(
        ["run_id", "version", "sim_ts", "created_by", "source", "rationale"],
        {
            "run_id": STR,
            "version": INT,
            "sim_ts": STR,
            "created_by": {"bsonType": "string", "pattern": "^agent:"},
            "source": {"enum": ["llm", "cache", "fallback"]},
            "rationale": STR,
            "steps": ARR,
        },
    ),
    dbm.MESSAGES: schema(
        ["run_id", "thread_id", "from", "to", "body", "direction", "trust", "sim_ts"],
        {
            "run_id": STR,
            "thread_id": STR,
            "from": STR,
            "to": STR,
            "body": STR,
            "direction": {"enum": ["out", "in"]},
            "trust": {"enum": ["system", "untrusted"]},
            "sim_ts": STR,
        },
    ),
    dbm.RUNS: schema(
        ["scenario_id", "mode", "seed", "status", "sim_ts", "households"],
        {
            "scenario_id": STR,
            "mode": {"enum": ["governed", "baseline"]},
            "seed": INT,
            "status": {"enum": ["created", "running", "paused", "finished", "failed"]},
            "sim_ts": STR,
            "speed": NUM,
            "scale": NUM,
            "households": INT,
        },
    ),
    # Sensible validators for the rest.
    dbm.FORECASTS: schema(["run_id", "sim_ts"], {"run_id": STR, "sim_ts": STR, "hourly": ARR, "windows": ARR}),
    dbm.AGENT_CONTROL: schema(["halted"], {"halted": BOOL, "scope": STR}),
    dbm.EVENT_MEMORY: schema(["kind", "summary"], {"kind": STR, "summary": {"bsonType": "string", "minLength": 1}, "mw_by_action": OBJ}),
    dbm.POLICY_DOCS: schema(["rule_id", "title", "text"], {"rule_id": {"bsonType": "string", "pattern": "^R[0-9]{2}$"}, "title": STR, "text": STR}),
    dbm.HOUSEHOLDS: schema(["idx", "location", "section_id", "archetype_id"], {"idx": INT, "location": GEO_POINT, "section_id": STR, "archetype_id": STR}),
    dbm.SECTIONS: schema(["geometry"], {"geometry": GEO_AREA, "protected": BOOL}),
    dbm.FACILITIES: schema(["location"], {"location": GEO_POINT}),
    dbm.NEIGHBOURHOODS: schema(["geometry"], {"geometry": GEO_AREA}),
    dbm.RESIDENTS: schema(["household_id", "name"], {"household_id": STR, "name": STR, "injection_actor": BOOL}),
    dbm.BUSINESSES: schema(["name", "max_mw"], {"name": STR, "max_mw": NUM, "protected": BOOL}),
    dbm.WEATHER: schema(["scenario_id", "ts", "temp_c"], {"scenario_id": STR, "ts": {"bsonType": ["string", "date"]}, "temp_c": NUM}),
    dbm.ARCHETYPES: schema(["defaults"], {"defaults": OBJ}),
}

PLAIN = [
    dbm.SCENARIOS, dbm.COUNTERS, dbm.LLM_CACHE, dbm.RUN_METRICS, dbm.MAP_SNAPSHOTS,
    dbm.LANGGRAPH_CHECKPOINTS, dbm.LANGGRAPH_WRITES,
    dbm.MOCK_THERMLINK_DEVICES, dbm.MOCK_THERMLINK_BATCHES, dbm.MOCK_CHARGEFLOW_SESSIONS,
    dbm.MOCK_POWERCELL_UNITS, dbm.MOCK_TEXTLINE_OUTBOX, dbm.MOCK_GRIDOPS_SECTIONS,
]

# Change-stream consumers (main.py) get before/after images on the collections they watch.
PRE_POST_IMAGES = [dbm.APPROVALS, dbm.AGENT_CONTROL, dbm.ACTION_REQUESTS]

TIMESERIES = {"timeField": "ts", "metaField": "meta", "granularity": "minutes"}

INDEXES: dict[str, list[IndexModel]] = {
    dbm.HOUSEHOLDS: [
        IndexModel([("location", GEOSPHERE)], name="location_2dsphere"),
        IndexModel([("section_id", ASCENDING)], name="section_id"),
        IndexModel([("archetype_id", ASCENDING)], name="archetype_id"),
        IndexModel([("idx", ASCENDING)], name="idx_unique", unique=True),
    ],
    dbm.SECTIONS: [IndexModel([("geometry", GEOSPHERE)], name="geometry_2dsphere")],
    dbm.NEIGHBOURHOODS: [IndexModel([("geometry", GEOSPHERE)], name="geometry_2dsphere")],
    dbm.FACILITIES: [
        IndexModel([("location", GEOSPHERE)], name="location_2dsphere"),
        IndexModel([("kind", ASCENDING), ("designated", ASCENDING)], name="kind_designated"),
    ],
    dbm.BUSINESSES: [IndexModel([("location", GEOSPHERE)], name="location_2dsphere"), IndexModel([("section_id", ASCENDING)], name="section_id")],
    dbm.RESIDENTS: [IndexModel([("household_id", ASCENDING)], name="household_id")],
    dbm.WEATHER: [IndexModel([("scenario_id", ASCENDING), ("ts", ASCENDING)], name="scenario_ts")],
    dbm.LEDGER: [
        IndexModel([("run_id", ASCENDING), ("seq", ASCENDING)], name="run_seq_unique", unique=True),
        IndexModel([("summary", TEXT)], name="summary_text"),
        IndexModel([("run_id", ASCENDING), ("type", ASCENDING), ("seq", ASCENDING)], name="run_type_seq"),
        IndexModel([("refs.request_id", ASCENDING)], name="refs_request_id", sparse=True),
    ],
    dbm.ACTION_REQUESTS: [
        IndexModel([("run_id", ASCENDING), ("status", ASCENDING)], name="run_status"),
        IndexModel([("run_id", ASCENDING), ("created_sim_ts", ASCENDING)], name="run_created"),
    ],
    dbm.DECISIONS: [IndexModel([("run_id", ASCENDING), ("request_id", ASCENDING)], name="run_request")],
    dbm.APPROVALS: [
        IndexModel([("run_id", ASCENDING), ("status", ASCENDING)], name="run_status"),
        IndexModel([("request_id", ASCENDING)], name="request_id"),
    ],
    dbm.EXECUTIONS: [
        IndexModel([("run_id", ASCENDING), ("status", ASCENDING)], name="run_status"),
        IndexModel([("request_id", ASCENDING)], name="request_id"),
    ],
    dbm.MESSAGES: [IndexModel([("run_id", ASCENDING), ("thread_id", ASCENDING), ("sim_ts", ASCENDING)], name="run_thread_ts")],
    dbm.PLANS: [IndexModel([("run_id", ASCENDING), ("version", DESCENDING)], name="run_version")],
    dbm.FORECASTS: [IndexModel([("run_id", ASCENDING), ("sim_ts", DESCENDING)], name="run_ts")],
    dbm.MAP_SNAPSHOTS: [IndexModel([("run_id", ASCENDING), ("sim_ts", ASCENDING)], name="run_ts")],
    dbm.RUN_METRICS: [IndexModel([("run_id", ASCENDING)], name="run_id")],
    dbm.FEEDER_LOAD: [IndexModel([("meta.run_id", ASCENDING), ("meta.section_id", ASCENDING), ("ts", ASCENDING)], name="run_section_ts")],
    dbm.EVENT_MEMORY: [IndexModel([("summary", TEXT), ("lesson", TEXT)], name="summary_lesson_text")],
    dbm.POLICY_DOCS: [IndexModel([("title", TEXT), ("text", TEXT)], name="title_text_text", weights={"title": 3, "text": 1})],
    dbm.POLICIES: [IndexModel([("version", ASCENDING)], name="version")],
}

# data/processed/<file>.json -> collection (fully replaced on every run).
STATIC_FILES = {
    dbm.NEIGHBOURHOODS: "neighbourhoods.json",
    dbm.SECTIONS: "sections.json",
    dbm.FACILITIES: "facilities.json",
    dbm.HOUSEHOLDS: "households.json",
    dbm.ARCHETYPES: "archetypes.json",
    dbm.RESIDENTS: "residents.json",
    dbm.BUSINESSES: "businesses.json",
    dbm.WEATHER: "weather.json",
}

RUN_SCOPED = [
    dbm.RUNS, dbm.FORECASTS, dbm.PLANS, dbm.ACTION_REQUESTS, dbm.DECISIONS, dbm.APPROVALS,
    dbm.EXECUTIONS, dbm.MESSAGES, dbm.LEDGER, dbm.AGENT_CONTROL, dbm.COUNTERS, dbm.RUN_METRICS,
    dbm.MAP_SNAPSHOTS, dbm.FEEDER_LOAD, dbm.LANGGRAPH_CHECKPOINTS, dbm.LANGGRAPH_WRITES,
    dbm.MOCK_THERMLINK_DEVICES, dbm.MOCK_THERMLINK_BATCHES, dbm.MOCK_CHARGEFLOW_SESSIONS,
    dbm.MOCK_POWERCELL_UNITS, dbm.MOCK_TEXTLINE_OUTBOX, dbm.MOCK_GRIDOPS_SECTIONS,
]

BATCH = 1000


async def ensure_collections() -> dict[str, str]:
    """Create every collection with its validator, or collMod the validator onto an existing one."""
    database = dbm.db()
    existing = {c["name"]: c async for c in await database.list_collections()}
    actions: dict[str, str] = {}
    opts = {"validationLevel": "strict", "validationAction": "error"}

    for name, validator in VALIDATORS.items():
        if name in existing:
            await database.command("collMod", name, validator=validator, **opts)
            actions[name] = "validator updated"
        else:
            await database.create_collection(name, validator=validator, **opts)
            actions[name] = "created + validator"

    if dbm.FEEDER_LOAD in existing:
        ts = existing[dbm.FEEDER_LOAD].get("options", {}).get("timeseries")
        actions[dbm.FEEDER_LOAD] = "time series exists" if ts else "EXISTS BUT NOT time series (drop it to fix)"
    else:
        await database.create_collection(dbm.FEEDER_LOAD, timeseries=TIMESERIES)
        actions[dbm.FEEDER_LOAD] = "created time series"

    for name in PLAIN:
        if name not in existing:
            await database.create_collection(name)
            actions[name] = "created"

    for name in PRE_POST_IMAGES:
        try:
            await database.command("collMod", name, changeStreamPreAndPostImages={"enabled": True})
        except OperationFailure as e:
            log.warning("changeStreamPreAndPostImages on %s: %s", name, e)
    return actions


async def ensure_indexes() -> dict[str, int]:
    database = dbm.db()
    out: dict[str, int] = {}
    for coll, models in INDEXES.items():
        made = 0
        for model in models:
            try:
                await database[coll].create_indexes([model])
                made += 1
            except OperationFailure as e:
                # An index with the same name but different options: replace it.
                if e.code in (85, 86):
                    name = model.document["name"]
                    log.warning("index %s.%s changed; recreating", coll, name)
                    try:
                        await database[coll].drop_index(name)
                        await database[coll].create_indexes([model])
                        made += 1
                    except OperationFailure as e2:
                        log.warning("index %s.%s could not be recreated: %s", coll, name, e2)
                else:
                    log.warning("index %s.%s failed: %s", coll, model.document["name"], e)
        out[coll] = made
    return out


def _read_json(path: Path) -> list[dict[str, Any]] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if isinstance(data, dict):
        data = data.get("features") or data.get("items") or [data]
    return data


async def replace_collection(name: str, docs: list[dict[str, Any]]) -> tuple[int, int]:
    """Replace a static collection's contents in batches. Returns (inserted, rejected)."""
    coll = dbm.db()[name]
    await coll.delete_many({})
    inserted = rejected = 0
    first_error: str | None = None
    for i in range(0, len(docs), BATCH):
        chunk = docs[i : i + BATCH]
        try:
            res = await coll.insert_many(chunk, ordered=False)
            inserted += len(res.inserted_ids)
        except BulkWriteError as e:
            errs = e.details.get("writeErrors", [])
            inserted += e.details.get("nInserted", 0)
            rejected += len(errs)
            if errs and first_error is None:
                first_error = errs[0].get("errmsg", "")[:300]
    if rejected:
        log.warning("%s: %d docs rejected by validator/indexes; first error: %s", name, rejected, first_error)
    return inserted, rejected


async def load_static_data() -> dict[str, str]:
    s = get_settings()
    processed = s.data_dir / "processed"
    out: dict[str, str] = {}
    for coll, fname in STATIC_FILES.items():
        docs = _read_json(processed / fname)
        if docs is None:
            out[coll] = f"skipped ({fname} missing; run scripts.build_world)"
            continue
        ins, rej = await replace_collection(coll, docs)
        out[coll] = f"{ins} loaded" + (f", {rej} rejected" if rej else "")

    scenarios = []
    for path in sorted(Path(s.scenarios_dir).glob("*.y*ml")):
        doc = yaml.safe_load(path.read_text()) or {}
        doc["_id"] = str(doc.get("id") or doc.get("scenario_id") or path.stem)
        doc["source_file"] = path.name
        scenarios.append(json.loads(json.dumps(doc, default=str)))  # dates -> strings
    if scenarios:
        ins, _ = await replace_collection(dbm.SCENARIOS, scenarios)
        out[dbm.SCENARIOS] = f"{ins} loaded"
    else:
        out[dbm.SCENARIOS] = "skipped (no scenarios/*.yaml)"

    meta = processed / "meta.json"
    if meta.exists():
        await dbm.db()[dbm.COUNTERS].replace_one({"_id": "world_meta"}, {"_id": "world_meta", **json.loads(meta.read_text())}, upsert=True)
    return out


async def seed_governance() -> dict[str, int]:
    """Upsert identities and the current policy set (versioned by POLICY_VERSION)."""
    database = dbm.db()
    ids = [ReplaceOne({"_id": i.id}, i.to_mongo(), upsert=True) for i in IDENTITIES]
    pols = [ReplaceOne({"_id": p.id}, p.to_mongo(), upsert=True) for p in POLICIES]
    await database[dbm.IDENTITIES].bulk_write(ids, ordered=True)
    await database[dbm.POLICIES].bulk_write(pols, ordered=True)
    # Drop rules that are no longer in the seed (none today, but keeps the set exact).
    await database[dbm.POLICIES].delete_many({"_id": {"$nin": [p.id for p in POLICIES]}})
    return {"identities": len(ids), "policies": len(pols)}


async def drop_run_data() -> None:
    database = dbm.db()
    for name in RUN_SCOPED:
        await database[name].delete_many({"_id": {"$ne": "world_meta"}} if name == dbm.COUNTERS else {})


async def setup_search(timeout_s: float) -> dict[str, str]:
    if not await search_indexes.search_supported():
        return {"search": "skipped (server has no Atlas Search)"}
    actions = await search_indexes.ensure_search_indexes()
    status = await search_indexes.wait_queryable(timeout_s=timeout_s)
    return {name: f"{actions.get(name, '?')}, {status.get(name, '?')}" for name in actions}


async def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--skip-data", action="store_true", help="do not reload data/processed/*.json")
    p.add_argument("--skip-search", action="store_true", help="do not create Atlas Search / Vector Search indexes")
    p.add_argument("--search-timeout", type=float, default=180.0)
    p.add_argument("--drop-run-data", action="store_true", help="delete all runs, ledgers and mock state")
    args = p.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    s = get_settings()
    if not s.mongodb_uri:
        log.error("MONGODB_URI is not set")
        return 2
    database = dbm.db()
    info = await database.client.server_info()
    print(f"connected: db={s.mongodb_db} server={info.get('version')} embed_mode={search_indexes.embed_mode()} policy_version={POLICY_VERSION}")

    _report("collections", await ensure_collections())
    _report("indexes", await ensure_indexes())
    if args.drop_run_data:
        await drop_run_data()
        print("run-scoped collections cleared")
    if not args.skip_data:
        _report("static data", await load_static_data())
    _report("governance seed", await seed_governance())
    if not args.skip_search:
        _report("search indexes", await setup_search(args.search_timeout))
    print("init_db done")
    return 0


def _report(title: str, d: dict[str, Any]) -> None:
    print(f"\n== {title}")
    for k, v in d.items():
        print(f"  {k:<32} {v}")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
