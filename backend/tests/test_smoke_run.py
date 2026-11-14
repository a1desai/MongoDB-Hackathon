"""End-to-end smoke: a short governed run and a short baseline run, headless, then the ledger
hash chain must verify (PLAN §10.5, §19).

The run tests need a reachable MongoDB with the world loaded (`scripts.init_db`); otherwise they
skip. The Executor reaches the mock systems in-process through httpx.ASGITransport. LLM keys are
blanked by conftest, so every agent takes its deterministic fallback.
"""

import asyncio

import httpx
import pytest
from pymongo import AsyncMongoClient

from app import runtime
from app.config import get_settings
from app.db import HOUSEHOLDS, LEDGER, POLICIES, RUNS, db

GOVERNED_TICKS = 6  # 12:00-12:30: first forecast, plan, gate, executor, verifier
BASELINE_TICKS = 4
BASELINE_FROM = "16:55"  # jump ahead so the status-quo setback and the 17:00 SMS appeal fire

PLAN_13_PATHS = {
    ("POST", "/runs"), ("GET", "/runs/{run_id}"), ("POST", "/runs/{run_id}/pause"), ("POST", "/runs/{run_id}/resume"),
    ("POST", "/runs/{run_id}/speed"), ("POST", "/runs/{run_id}/inject"), ("GET", "/runs/{run_id}/ledger"),
    ("GET", "/runs/{run_id}/approvals"), ("POST", "/approvals/{approval_id}/decide"), ("POST", "/control/halt"),
    ("POST", "/control/resume"), ("POST", "/executions/{execution_id}/rollback"), ("POST", "/audit/ask"),
    ("GET", "/ledger/verify"), ("GET", "/runs/{run_id}/metrics"), ("GET", "/runs/{run_id}/compare"),
    ("GET", "/world/static"), ("GET", "/residents/{resident_id}"), ("GET", "/residents/{resident_id}/thread"),
}


def test_plan_13_endpoints_mounted():
    from app.main import app

    spec = app.openapi()
    mounted = {(m.upper(), p) for p, ops in spec["paths"].items() for m in ops}
    missing = PLAN_13_PATHS - mounted
    assert not missing, f"missing endpoints: {sorted(missing)}"
    assert any(p.startswith("/mock/thermlink/v1") for p in spec["paths"]), "mock systems not mounted"


async def _db_ready() -> str | None:
    """None if the database is reachable and loaded, else the reason to skip."""
    s = get_settings()
    if not s.mongodb_uri or "<" in s.mongodb_uri:
        return "MONGODB_URI is not set"
    client: AsyncMongoClient = AsyncMongoClient(s.mongodb_uri, serverSelectionTimeoutMS=3000)
    try:
        await asyncio.wait_for(client.admin.command("ping"), 5)
        d = client[s.mongodb_db]
        if not await d[HOUSEHOLDS].find_one({}, {"_id": 1}):
            return f"no households in {s.mongodb_db}; run scripts.init_db first"
        if not await d[POLICIES].find_one({}, {"_id": 1}):
            return f"no policies in {s.mongodb_db}; run scripts.init_db first"
    except Exception as exc:
        return f"MongoDB not reachable: {type(exc).__name__}"
    finally:
        await client.close()
    return None


async def _run(mode: str, ticks: int, http: httpx.AsyncClient, start_hhmm: str | None = None) -> str:
    from app.sim.engine import RunEngine
    from app.timeutil import at

    eng = await RunEngine.create("heatwave_2031", mode, 42, http=http, headless=True, start=False,
                                 pause_on_approval=False, extra={"test": True})
    if start_hhmm:
        eng.world.sim_now = at(eng.world.sim_now, start_hhmm)
    try:
        await eng.run_ticks(ticks)
        await asyncio.sleep(0.2)  # let background world-agent tasks settle
    finally:
        await eng.stop()
        runtime.unregister(eng.run_id)
    assert eng.world.tick == ticks
    return eng.run_id


@pytest.mark.smoke
@pytest.mark.asyncio
async def test_governed_and_baseline_runs_verify():
    reason = await _db_ready()
    if reason:
        pytest.skip(reason)
    from app.main import app

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://cooldown.test",
                                 timeout=30.0) as http:
        gov_id = await _run("governed", GOVERNED_TICKS, http)
        base_id = await _run("baseline", BASELINE_TICKS, http, BASELINE_FROM)

        for run_id in (gov_id, base_id):
            run = await db()[RUNS].find_one({"_id": run_id})
            assert run is not None and run["households"] > 0 and run["tick"] >= 1
            n = await db()[LEDGER].count_documents({"run_id": run_id})
            assert n >= 2, f"{run_id}: expected ledger entries, got {n}"
            r = await http.get("/ledger/verify", params={"run_id": run_id})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["ok"], body
            assert body.get("checked", n) >= n

        r = await http.get(f"/runs/{gov_id}/ledger", params={"after_seq": 0})
        assert r.status_code == 200 and r.json()["entries"][0]["seq"] == 1
        types = {e["type"] for e in r.json()["entries"]}
        assert "world_event" in types
        r = await http.get(f"/runs/{gov_id}")
        assert r.status_code == 200 and r.json()["mode"] == "governed"
