"""One AsyncMongoClient per process (PyMongo Async, not Motor). Import `db()` everywhere."""

from functools import lru_cache

from pymongo import AsyncMongoClient
from pymongo.asynchronous.database import AsyncDatabase

from .config import get_settings

# Collection names. Use these constants, not string literals.
RUNS = "runs"
SCENARIOS = "scenarios"
NEIGHBOURHOODS = "neighbourhoods"
SECTIONS = "sections"
FACILITIES = "facilities"
HOUSEHOLDS = "households"
ARCHETYPES = "archetypes"
RESIDENTS = "residents"
BUSINESSES = "businesses"
WEATHER = "weather"
FEEDER_LOAD = "feeder_load"  # time series: timeField ts, metaField meta
FORECASTS = "forecasts"
PLANS = "plans"
ACTION_REQUESTS = "action_requests"
DECISIONS = "decisions"
APPROVALS = "approvals"
EXECUTIONS = "executions"
MESSAGES = "messages"
LEDGER = "ledger"
IDENTITIES = "identities"
POLICIES = "policies"
POLICY_DOCS = "policy_docs"
EVENT_MEMORY = "event_memory"
AGENT_CONTROL = "agent_control"
COUNTERS = "counters"
LLM_CACHE = "llm_cache"
RUN_METRICS = "run_metrics"
MAP_SNAPSHOTS = "map_snapshots"
LANGGRAPH_CHECKPOINTS = "langgraph_checkpoints"
LANGGRAPH_WRITES = "langgraph_checkpoint_writes"
MOCK_THERMLINK_DEVICES = "mock_thermlink_devices"
MOCK_THERMLINK_BATCHES = "mock_thermlink_batches"
MOCK_CHARGEFLOW_SESSIONS = "mock_chargeflow_sessions"
MOCK_POWERCELL_UNITS = "mock_powercell_units"
MOCK_TEXTLINE_OUTBOX = "mock_textline_outbox"
MOCK_GRIDOPS_SECTIONS = "mock_gridops_sections"

# Atlas Search / Vector Search index names.
EVENT_MEMORY_VEC = "event_memory_vec"
POLICY_DOCS_VEC = "policy_docs_vec"
LEDGER_SEARCH = "ledger_search"


@lru_cache
def client() -> AsyncMongoClient:
    return AsyncMongoClient(get_settings().mongodb_uri, appname="cooldown", tz_aware=True)


def db() -> AsyncDatabase:
    return client()[get_settings().mongodb_db]
