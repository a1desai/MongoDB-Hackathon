"""Rollback (PLAN §10.4): turn an execution's stored undo into a new "undo" action_request.

The request is stored with status "proposed" and logged as rollback_requested; it then goes
through the Gate like any other request (Governance.process_request -> gate.decide), where R16
checks the load it puts back and R15 lets it through a halt. Only human:supervisor may roll back.
The request is proposed by agent:restorer (the identity with "undo" scope) acting for the supervisor.
"""

from __future__ import annotations

import logging

from app import db as dbm
from app.governance.approvals import sim_now_for
from app.governance.identities import load_identities
from app.ids import new_id
from app.models import ActionRequest, Evidence, Execution, Targets
from app.timeutil import sim_iso, wall_now

log = logging.getLogger(__name__)

SUPERVISOR = "human:supervisor"
PROPOSER = "agent:restorer"
# Undoing these puts their delivered MW back on the grid (R16 checks it against the margin).
LOAD_REDUCING = {"thermostat_setback", "ev_pause", "battery_discharge", "business_curtail_request", "feeder_outage", "send_sms"}
OPEN_STATUSES = ["proposed", "needs_approval", "approved", "executing"]


async def request_rollback(execution_id: str, by: str, reason: str) -> ActionRequest:
    """Build, store and log an undo request for an execution. Raises PermissionError / KeyError / ValueError."""
    if by != SUPERVISOR:
        raise PermissionError(f"only {SUPERVISOR} may roll back, not {by}")
    ident = (await load_identities()).get(by)
    if ident is not None and (ident.status != "active" or not ident.on_shift):
        raise PermissionError(f"{by} is not active and on shift")

    database = dbm.db()
    doc = await database[dbm.EXECUTIONS].find_one({"_id": execution_id})
    if doc is None:
        raise KeyError(f"execution {execution_id} not found")
    exe = Execution.model_validate(doc)
    if exe.undo is None:
        raise ValueError(f"execution {execution_id} has no undo command (SMS cannot be unsent; send a correction instead)")
    if exe.status == "rolled_back":
        raise ValueError(f"execution {execution_id} is already rolled back")
    dup = await database[dbm.ACTION_REQUESTS].find_one({"undo_of": execution_id, "status": {"$in": OPEN_STATUSES}}, {"_id": 1})
    if dup:
        raise ValueError(f"rollback {dup['_id']} for {execution_id} is already in progress")

    orig = await database[dbm.ACTION_REQUESTS].find_one({"_id": exe.request_id}) or {}
    orig_action = orig.get("action_type", "unknown")
    run_id = exe.run_id
    now_dt = await sim_now_for(run_id)
    now = sim_iso(now_dt) if now_dt is not None else wall_now()

    delivered = float(exe.verification.delivered_mw or 0.0)
    restore_mw = delivered if orig_action in LOAD_REDUCING else 0.0
    otargets = orig.get("targets") or {}
    targets = Targets(
        household_ids=list(otargets.get("household_ids") or []),
        section_id=otargets.get("section_id"),
        business_id=otargets.get("business_id"),
    )
    rid = new_id("ar_", run_id)
    req = ActionRequest(
        _id=rid,
        run_id=run_id,
        created_sim_ts=now,
        sim_ts=now,
        action_type="undo",
        system=exe.undo.system,
        rung=None,
        proposed_by=PROPOSER,
        acting_for=SUPERVISOR,
        plan_id=orig.get("plan_id"),
        plan_version=orig.get("plan_version"),
        summary=f"Roll back {execution_id} ({orig.get('summary') or orig_action}) requested by {by}: {reason}"[:300],
        targets=targets,
        params={
            "undo_path": exe.undo.path,
            "undo_body": exe.undo.body,
            "original_request_id": exe.request_id,
            "original_action": orig_action,
            "requested_by": by,
            "reason": reason,
            "restore_mw": round(restore_mw, 2),
        },
        evidence=[
            Evidence(id=execution_id, kind="execution", trust="system"),
            Evidence(id=exe.request_id, kind="request", trust="system"),
        ],
        expected_mw=0.0,
        status="proposed",
        idempotency_key=rid,
        undo_of=execution_id,
        requested_by=by,
    )
    await database[dbm.ACTION_REQUESTS].insert_one(req.to_mongo())

    from app.governance.ledger import append

    await append(
        run_id,
        "rollback_requested",
        by,
        f"{by} requested rollback of {execution_id} ({orig_action}, {delivered:,.1f} MW delivered) as {rid}: {reason}"[:500],
        sim_ts=now,
        acting_for=None,
        refs={"request_id": rid, "execution_id": execution_id, "plan_version": orig.get("plan_version")},
        payload={
            "undo": exe.undo.model_dump(),
            "original_request_id": exe.request_id,
            "original_action": orig_action,
            "restore_mw": round(restore_mw, 2),
            "proposed_by": PROPOSER,
            "reason": reason,
        },
    )
    return req
