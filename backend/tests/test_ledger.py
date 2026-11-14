"""PLAN §19: a 50-entry chain verifies and editing one payload is detected at the right seq.

Also: concurrent appends stay gap-free, and the approval / kill-switch / rollback / decide flows
write what they should. Runs against MongoDB at TEST_MONGODB_URI (default: the shared local
replica set on :27019) in its own database; skipped when that server is unreachable.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import timedelta

import pytest

TEST_URI = os.environ.get("TEST_MONGODB_URI", "mongodb://localhost:27019/?directConnection=true")
TEST_DB = os.environ.get("TEST_MONGODB_DB", "cooldown_w4")


@pytest.fixture()
def mongo(monkeypatch):
    """Point app.db at the test database for this test (settings and client caches cleared)."""
    from app import config, db

    monkeypatch.setenv("MONGODB_URI", TEST_URI)
    monkeypatch.setenv("MONGODB_DB", TEST_DB)
    config.get_settings.cache_clear()
    db.client.cache_clear()

    async def ping() -> bool:
        from pymongo import AsyncMongoClient

        c = AsyncMongoClient(TEST_URI, serverSelectionTimeoutMS=1500)
        try:
            await c.admin.command("ping")
            return True
        except Exception:
            return False
        finally:
            await c.close()

    if not asyncio.run(ping()):
        pytest.skip(f"MongoDB not reachable at {TEST_URI}")
    yield db
    config.get_settings.cache_clear()
    db.client.cache_clear()


def run(coro_fn):
    """Run one async test body on a fresh loop with a fresh client, closing it afterwards."""
    from app import db

    async def wrapper():
        db.client.cache_clear()
        try:
            return await coro_fn()
        finally:
            await db.client().close()
            db.client.cache_clear()

    return asyncio.run(wrapper())


def new_run_id() -> str:
    return f"run_t{uuid.uuid4().hex[:8]}"


SIM_TS = "2031-06-24T17:00:00-04:00"


def test_chain_of_50_verifies_and_tamper_is_located(mongo):
    from app.governance.ledger import GENESIS, append, verify_chain

    rid = new_run_id()

    async def body():
        for i in range(50):
            await append(rid, "world_event", "sim:world", f"event {i}", sim_ts=SIM_TS, refs={"request_id": f"ar_{i}"},
                         payload={"i": i, "mw": i * 1.5, "tags": ["a", "b"], "nested": {"x": i}})
        first = await mongo.db()[mongo.LEDGER].find_one({"run_id": rid, "seq": 1})
        assert first["prev_hash"] == GENESIS
        ok = await verify_chain(rid)
        assert ok["ok"] and ok["checked"] == 50 and ok["first_broken_seq"] is None

        # Tamper with one payload (as someone with raw DB access would): detected at that seq.
        await mongo.db()[mongo.LEDGER].update_one({"run_id": rid, "seq": 23}, {"$set": {"payload.mw": 999.0}})
        bad = await verify_chain(rid)
        assert not bad["ok"] and bad["first_broken_seq"] == 23 and bad["checked"] == 22
        assert "payload" in bad["reason"]

    run(body)


def test_tampered_summary_and_deleted_entry_are_detected(mongo):
    from app.governance.ledger import append, verify_chain

    rid = new_run_id()

    async def body():
        for i in range(10):
            await append(rid, "world_event", "sim:world", f"event {i}", sim_ts=SIM_TS, payload={"i": i})
        coll = mongo.db()[mongo.LEDGER]
        await coll.update_one({"run_id": rid, "seq": 4}, {"$set": {"summary": "nothing happened"}})
        assert (await verify_chain(rid))["first_broken_seq"] == 4
        await coll.update_one({"run_id": rid, "seq": 4}, {"$set": {"summary": "event 3"}})
        assert (await verify_chain(rid))["ok"]
        await coll.delete_one({"run_id": rid, "seq": 7})
        assert (await verify_chain(rid))["first_broken_seq"] == 7

    run(body)


def test_concurrent_appends_stay_gap_free(mongo):
    from app.governance.ledger import append, verify_chain

    rid = new_run_id()

    async def body():
        entries = await asyncio.gather(*[
            append(rid, "message_in", f"sim:resident:hh_{i:05d}", f"reply {i}", sim_ts=SIM_TS, payload={"i": i})
            for i in range(100)
        ])
        assert sorted(e.seq for e in entries) == list(range(1, 101))
        res = await verify_chain(rid)
        assert res["ok"] and res["checked"] == 100

    run(body)


def test_canonical_json_is_stable():
    import numpy as np

    from app.governance.ledger import canonical_json, normalize

    a = canonical_json({"b": 1, "a": [np.int64(2), np.float64(1.5)], "c": {"z": True, "y": None}})
    assert a == '{"a":[2,1.5],"b":1,"c":{"y":null,"z":true}}'
    assert normalize({"x": float("nan"), "k": (1, 2)}) == {"x": None, "k": [1, 2]}


# ---------------------------------------------------------------- approvals, kill switch, rollback, decide


def _world_and_request(rid: str):
    try:
        from test_gate_rules import OUTAGE, VULNERABLE, make_world, req
    except ImportError:
        from tests.test_gate_rules import OUTAGE, VULNERABLE, make_world, req

    w = make_world()
    w.run_id = rid
    w.notified_cool_space[list(VULNERABLE)] = True
    w.notified_sim_minute[list(VULNERABLE)] = 170
    w.shortfall_mw = 300.0  # lower rungs (~20 MW here) cannot cover it, so R07 lets the outage through
    r = req("feeder_outage", section="TOR-S04", params=OUTAGE, expected_mw=50.0)
    ar = f"ar_{rid[4:]}0001"
    return w, r.model_copy(update={"id": ar, "idempotency_key": ar, "run_id": rid})


def test_decide_then_approval_flow(mongo, monkeypatch):
    from app import runtime
    from app.governance import approvals, gate
    from app.governance.ledger import verify_chain

    rid = new_run_id()
    w, r = _world_and_request(rid)
    monkeypatch.setitem(runtime._worlds, rid, w)

    async def body():
        await mongo.db()[mongo.ACTION_REQUESTS].insert_one(r.to_mongo())
        d = await gate.decide(r, w, {"_id": rid, "scale": w.scale})
        assert d.verdict == "needs_approval"
        stored = await mongo.db()[mongo.ACTION_REQUESTS].find_one({"_id": r.id})
        assert stored["status"] == "needs_approval" and stored["decision_id"] == d.id

        apr = await approvals.request_approval(r, d, w)
        assert apr.status == "pending"
        with pytest.raises(PermissionError):
            await approvals.decide_approval(apr.id, "approve", "human:operator", None)
        done = await approvals.decide_approval(apr.id, "approve", "human:supervisor", "cool space confirmed")
        assert done.status == "approved" and done.decided_by == "human:supervisor"
        with pytest.raises(ValueError):
            await approvals.decide_approval(apr.id, "deny", "human:supervisor", None)
        assert (await mongo.db()[mongo.ACTION_REQUESTS].find_one({"_id": r.id}))["status"] == "approved"

        # A second approval left pending past 10 sim-minutes is auto-denied.
        r2 = r.model_copy(update={"id": r.id + "b", "idempotency_key": r.id + "b", "status": "proposed"})
        await mongo.db()[mongo.ACTION_REQUESTS].insert_one(r2.to_mongo())
        d2 = await gate.decide(r2, w, {"_id": rid, "scale": w.scale})
        apr2 = await approvals.request_approval(r2, d2, w)
        assert await approvals.expire_due(rid, w.sim_now + timedelta(minutes=5)) == []
        expired = await approvals.expire_due(rid, w.sim_now + timedelta(minutes=10))
        assert [a.id for a in expired] == [apr2.id] and expired[0].status == "expired"
        assert (await mongo.db()[mongo.ACTION_REQUESTS].find_one({"_id": r2.id}))["status"] == "denied"

        types = [e["type"] async for e in mongo.db()[mongo.LEDGER].find({"run_id": rid}, sort=[("seq", 1)])]
        assert types == ["decision", "approval_requested", "approval_decided", "decision", "approval_requested", "approval_decided"]
        assert (await verify_chain(rid))["ok"]

    run(body)


def test_halt_resume_and_rollback(mongo, monkeypatch):
    from app import runtime
    from app.governance import control, rollback
    from app.models import Execution, UndoCommand, Verification

    rid = new_run_id()
    w, _ = _world_and_request(rid)
    monkeypatch.setitem(runtime._worlds, rid, w)

    async def body():
        assert not await control.is_halted(rid)
        c = await control.halt(rid, "all", "human:supervisor", "demo halt")
        assert c.halted and w.halted
        assert await control.is_halted(rid, "agent:planner")
        with pytest.raises(PermissionError):
            await control.resume(rid, "human:operator")
        await control.resume(rid, "human:supervisor")
        assert not await control.is_halted(rid) and not w.halted
        await control.halt(rid, "agent:negotiator", "human:operator", "bad texts")
        assert await control.is_halted(rid, "agent:negotiator") and not await control.is_halted(rid, "agent:planner")
        await control.resume(rid, "human:supervisor")

        exe = Execution(_id=f"exe_{rid[4:]}0001", run_id=rid, request_id=f"ar_{rid[4:]}9001", system="powercell",
                        idempotency_key="k", path="/mock/powercell/v1/dispatch", status="verified",
                        undo=UndoCommand(system="powercell", path="/mock/powercell/v1/stop", body={"unit_ids": ["pc_1"]}),
                        verification=Verification(applied=1, delivered_mw=12.5), sent_sim_ts=SIM_TS, sent_tick=60)
        await mongo.db()[mongo.EXECUTIONS].insert_one(exe.to_mongo())
        await mongo.db()[mongo.ACTION_REQUESTS].insert_one({"_id": exe.request_id, "run_id": rid, "action_type": "battery_discharge",
                                                             "summary": "Discharge 1 battery", "targets": {"household_ids": ["hh_00001"]}})
        with pytest.raises(PermissionError):
            await rollback.request_rollback(exe.id, "agent:planner", "nope")
        with pytest.raises(KeyError):
            await rollback.request_rollback("exe_missing", "human:supervisor", "x")
        undo = await rollback.request_rollback(exe.id, "human:supervisor", "battery needed later")
        assert undo.action_type == "undo" and undo.undo_of == exe.id and undo.system == "powercell"
        assert undo.params["restore_mw"] == 12.5 and undo.targets.household_ids == ["hh_00001"]
        with pytest.raises(ValueError):
            await rollback.request_rollback(exe.id, "human:supervisor", "again")

        types = [e["type"] async for e in mongo.db()[mongo.LEDGER].find({"run_id": rid}, sort=[("seq", 1)])]
        assert types == ["halt", "resume", "halt", "resume", "rollback_requested"]

    run(body)
