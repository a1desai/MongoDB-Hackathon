"""Human approvals (PLAN §10.1): a needs_approval decision opens an approval that expires after
10 simulated minutes. Only the on-shift human:supervisor may decide it. Expired approvals are
auto-denied and logged. Status changes are single atomic updates guarded on status "pending", so
a decision and an expiry can never both win.

Errors map to HTTP codes in api/common.py: PermissionError 403, KeyError 404, ValueError 400.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from pymongo import ReturnDocument

from app import db as dbm
from app.governance.identities import load_identities
from app.ids import new_id
from app.models import ActionRequest, Approval, Decision
from app.sim.world_state import World
from app.timeutil import parse_sim, sim_iso

log = logging.getLogger(__name__)

APPROVAL_TTL_MIN = 10  # PLAN §10.1
SUPERVISOR = "human:supervisor"
GATE_ID = "system:gate"
APPROVE_WORDS = {"approve", "approved", "allow", "yes"}
DENY_WORDS = {"deny", "denied", "reject", "rejected", "no"}


async def sim_now_for(run_id: str) -> datetime | None:
    """The run's current sim time: the live World if this process runs it, else runs.sim_ts."""
    try:
        from app import runtime

        return runtime.get_world(run_id).sim_now
    except Exception:
        pass
    try:
        run = await dbm.db()[dbm.RUNS].find_one({"_id": run_id}, {"sim_ts": 1})
        return parse_sim(run["sim_ts"]) if run and run.get("sim_ts") else None
    except Exception:
        return None


async def request_approval(request: ActionRequest, decision: Decision, world: World) -> Approval:
    """Open an approval for a needs_approval decision; ledger approval_requested. Idempotent per decision."""
    database = dbm.db()
    existing = await database[dbm.APPROVALS].find_one({"decision_id": decision.id})
    if existing:
        return Approval.model_validate(existing)
    now = world.sim_now
    failing = [r for r in decision.rule_results if not r.passed]
    approval = Approval(
        _id=new_id("apr_", request.run_id),
        run_id=request.run_id,
        request_id=request.id,
        decision_id=decision.id,
        status="pending",
        summary=request.summary,
        blast_radius=decision.blast_radius,
        rule_results=decision.rule_results,
        untrusted_influence=decision.untrusted_influence,
        requested_sim_ts=sim_iso(now),
        expires_sim_ts=sim_iso(now + timedelta(minutes=APPROVAL_TTL_MIN)),
        action_type=request.action_type,
        proposed_by=request.proposed_by,
        acting_for=request.acting_for,
        reasons=[f"{r.rule_id} {r.name}: {r.detail}" for r in failing],
        allowed_count=decision.allowed_count,
        denied_count=decision.denied_count,
    )
    await database[dbm.APPROVALS].insert_one(approval.to_mongo())
    request.approval_id = approval.id
    request.status = "needs_approval"
    request.sim_ts = sim_iso(now)
    await database[dbm.ACTION_REQUESTS].update_one(
        {"_id": request.id}, {"$set": {"approval_id": approval.id, "status": "needs_approval", "sim_ts": request.sim_ts}}
    )
    from app.governance.ledger import append

    flag = " [influenced by untrusted input]" if approval.untrusted_influence else ""
    br = approval.blast_radius
    await append(
        request.run_id,
        "approval_requested",
        GATE_ID,
        f"Approval {approval.id} requested from {SUPERVISOR} for {request.id}{flag}: {request.summary} "
        f"({br.homes_real:,} real homes, {br.vulnerable_real:,} vulnerable, {br.mw:,.1f} MW); expires {approval.expires_sim_ts[11:16]}"[:500],
        sim_ts=approval.requested_sim_ts,
        acting_for=request.acting_for,
        refs={"request_id": request.id, "decision_id": decision.id, "approval_id": approval.id, "plan_version": request.plan_version},
        payload={
            "approver": SUPERVISOR,
            "expires_sim_ts": approval.expires_sim_ts,
            "reasons": approval.reasons,  # type: ignore[attr-defined]
            "blast_radius": br.model_dump(),
            "untrusted_influence": approval.untrusted_influence,
        },
    )
    return approval


def _normalize(decision: str) -> str:
    d = (decision or "").strip().lower()
    if d in APPROVE_WORDS:
        return "approved"
    if d in DENY_WORDS:
        return "denied"
    raise ValueError(f"decision must be approve or deny, not {decision!r}")


async def _check_supervisor(by: str) -> None:
    if by != SUPERVISOR:
        raise PermissionError(f"{by} may not decide approvals; only {SUPERVISOR} may")
    ident = (await load_identities()).get(by)
    if ident is not None and (ident.status != "active" or not ident.on_shift):
        raise PermissionError(f"{by} is not active and on shift")


async def _finish(doc: dict, status: str, by: str, note: str | None, now_iso: str) -> Approval:
    """Request status + ledger approval_decided for an approval that just left 'pending'."""
    approval = Approval.model_validate(doc)
    req_status = "approved" if status == "approved" else "denied"
    await dbm.db()[dbm.ACTION_REQUESTS].update_one(
        {"_id": approval.request_id, "status": "needs_approval"}, {"$set": {"status": req_status, "sim_ts": now_iso}}
    )
    from app.governance.ledger import append

    if status == "expired":
        actor, summary = GATE_ID, f"Approval {approval.id} for {approval.request_id} expired after {APPROVAL_TTL_MIN} sim-min; auto-denied"
    else:
        verb = "APPROVED" if status == "approved" else "DENIED"
        summary = f"{by} {verb} {approval.request_id} (approval {approval.id}): {approval.summary}" + (f" Note: {note}" if note else "")
        actor = by
    await append(
        approval.run_id,
        "approval_decided",
        actor,
        summary[:500],
        sim_ts=now_iso,
        acting_for=None,
        refs={"request_id": approval.request_id, "decision_id": approval.decision_id, "approval_id": approval.id},
        payload={"status": status, "decided_by": by, "note": note, "requested_sim_ts": approval.requested_sim_ts, "expires_sim_ts": approval.expires_sim_ts},
    )
    return approval


async def decide_approval(approval_id: str, decision: str, by: str, note: str | None = None) -> Approval:
    """Approve or deny a pending approval. Only human:supervisor; expired approvals cannot be decided."""
    status = _normalize(decision)
    await _check_supervisor(by)
    coll = dbm.db()[dbm.APPROVALS]
    doc = await coll.find_one({"_id": approval_id})
    if doc is None:
        raise KeyError(f"approval {approval_id} not found")
    if doc.get("status") != "pending":
        raise ValueError(f"approval {approval_id} is already {doc.get('status')}")
    now = await sim_now_for(doc["run_id"])
    expires = parse_sim(doc["expires_sim_ts"])
    if now is not None and now >= expires:
        await expire_due(doc["run_id"], now)
        raise ValueError(f"approval {approval_id} expired at {doc['expires_sim_ts']}; it was auto-denied")
    now_iso = sim_iso(now) if now is not None else doc["requested_sim_ts"]
    updated = await coll.find_one_and_update(
        {"_id": approval_id, "status": "pending"},
        {"$set": {"status": status, "decided_by": by, "decided_sim_ts": now_iso, "note": note}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        cur = await coll.find_one({"_id": approval_id}, {"status": 1})
        raise ValueError(f"approval {approval_id} is already {(cur or {}).get('status')}")
    return await _finish(updated, status, by, note, now_iso)


async def expire_due(run_id: str, sim_now: datetime) -> list[Approval]:
    """Auto-deny pending approvals whose 10 sim-minutes have passed; ledger approval_decided each."""
    coll = dbm.db()[dbm.APPROVALS]
    now_iso = sim_iso(sim_now)
    out: list[Approval] = []
    async for doc in coll.find({"run_id": run_id, "status": "pending"}, {"_id": 1, "expires_sim_ts": 1}):
        try:
            if parse_sim(doc["expires_sim_ts"]) > sim_now:
                continue
        except Exception:
            continue
        updated = await coll.find_one_and_update(
            {"_id": doc["_id"], "status": "pending"},
            {"$set": {"status": "expired", "decided_by": GATE_ID, "decided_sim_ts": now_iso, "note": f"auto-denied: no decision within {APPROVAL_TTL_MIN} sim-minutes"}},
            return_document=ReturnDocument.AFTER,
        )
        if updated is not None:
            out.append(await _finish(updated, "expired", GATE_ID, updated.get("note"), now_iso))
    return out


async def pending(run_id: str) -> list[Approval]:
    """Pending approvals for a run, oldest first."""
    cur = dbm.db()[dbm.APPROVALS].find({"run_id": run_id, "status": "pending"}, sort=[("requested_sim_ts", 1)])
    return [Approval.model_validate(d) async for d in cur]
