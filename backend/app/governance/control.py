"""The kill switch (PLAN §10.4): one agent_control doc per run (`_id` = run_id).

halt() / resume() update it and log halt / resume; the engine's change stream on agent_control
sets world.halted. We also set the flag on a live World directly so the stop is immediate.
Any active human may halt (a safety stop); only the on-shift human:supervisor may resume.
"""

from __future__ import annotations

import logging

from pymongo import ReturnDocument

from app import db as dbm
from app.governance.approvals import sim_now_for
from app.governance.identities import load_identities
from app.models import AgentControl
from app.timeutil import sim_iso, wall_now

log = logging.getLogger(__name__)

SUPERVISOR = "human:supervisor"


def _live_world(run_id: str):
    try:
        from app import runtime

        return runtime.get_world(run_id)
    except Exception:
        return None


async def _check_human(by: str, *, supervisor_only: bool) -> None:
    if not by.startswith("human:"):
        raise PermissionError(f"{by} is not a human identity")
    if supervisor_only and by != SUPERVISOR:
        raise PermissionError(f"only {SUPERVISOR} may do this, not {by}")
    ident = (await load_identities()).get(by)
    if ident is not None and ident.status != "active":
        raise PermissionError(f"{by} is {ident.status}")


async def _now_iso(run_id: str) -> str:
    now = await sim_now_for(run_id)
    return sim_iso(now) if now is not None else wall_now()


async def get_control(run_id: str) -> AgentControl:
    doc = await dbm.db()[dbm.AGENT_CONTROL].find_one({"_id": run_id})
    return AgentControl.model_validate(doc) if doc else AgentControl(_id=run_id)


async def halt(run_id: str, scope: str, by: str, reason: str) -> AgentControl:
    """Engage the kill switch for "all" agents or one agent id; ledger halt."""
    await _check_human(by, supervisor_only=False)
    scope = scope or "all"
    if scope != "all" and not scope.startswith("agent:"):
        raise ValueError(f"scope must be 'all' or an agent id, not {scope!r}")
    now = await _now_iso(run_id)
    doc = await dbm.db()[dbm.AGENT_CONTROL].find_one_and_update(
        {"_id": run_id},
        {"$set": {"halted": True, "scope": scope, "by": by, "reason": reason, "sim_ts": now}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    if (w := _live_world(run_id)) is not None:
        w.halted, w.halted_scope = True, scope
    from app.governance.ledger import append

    await append(
        run_id,
        "halt",
        by,
        f"{by} HALTED {'all agents' if scope == 'all' else scope}: {reason}"[:500],
        sim_ts=now,
        payload={"scope": scope, "reason": reason},
    )
    return AgentControl.model_validate(doc)


async def resume(run_id: str, by: str) -> AgentControl:
    """Release the kill switch; ledger resume. Supervisor only."""
    await _check_human(by, supervisor_only=True)
    now = await _now_iso(run_id)
    prev = await get_control(run_id)
    doc = await dbm.db()[dbm.AGENT_CONTROL].find_one_and_update(
        {"_id": run_id},
        {"$set": {"halted": False, "scope": "all", "by": by, "reason": "resumed", "sim_ts": now}},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    if (w := _live_world(run_id)) is not None:
        w.halted, w.halted_scope = False, "all"
    from app.governance.ledger import append

    await append(
        run_id,
        "resume",
        by,
        f"{by} RESUMED agents (halt scope was {prev.scope}{', by ' + prev.by if prev.by and prev.halted else ''})"[:500],
        sim_ts=now,
        payload={"previous_scope": prev.scope, "was_halted": prev.halted},
    )
    return AgentControl.model_validate(doc)


async def is_halted(run_id: str, agent_id: str | None = None) -> bool:
    """True if the run is halted for this agent ("all" or its own id); with no agent, if halted at all."""
    try:
        doc = await dbm.db()[dbm.AGENT_CONTROL].find_one({"_id": run_id}, {"halted": 1, "scope": 1})
        halted, scope = bool(doc and doc.get("halted")), (doc or {}).get("scope") or "all"
    except Exception as exc:
        log.warning("is_halted: agent_control read failed (%s); using the live World flag", exc)
        w = _live_world(run_id)
        halted, scope = (bool(w.halted), w.halted_scope or "all") if w is not None else (False, "all")
    if not halted:
        return False
    return agent_id is None or scope in ("all", agent_id)
