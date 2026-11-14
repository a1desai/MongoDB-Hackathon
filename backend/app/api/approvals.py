"""Approvals: the Supervisor approves or denies a pending action (PLAN §10.1, §13).

The approvals change stream (main.py) resumes the request's LangGraph thread. This endpoint also
nudges the live engine directly, which de-duplicates, so a missed change event never strands a
request.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import APPROVALS, db
from .common import J, http_error, jsonable, live_engine

router = APIRouter(tags=["approvals"])

DECISIONS = {"approve": "approved", "approved": "approved", "deny": "denied", "denied": "denied", "reject": "denied"}


class DecideBody(BaseModel):
    decision: str  # approved | denied (approve / deny accepted)
    by: str
    note: str | None = None


@router.get("/approvals/{approval_id}")
async def get_approval(approval_id: str) -> Any:
    doc = await db()[APPROVALS].find_one({"_id": approval_id})
    if doc is None:
        raise HTTPException(404, f"approval {approval_id} not found")
    return J(doc)


@router.post("/approvals/{approval_id}/decide")
async def decide(approval_id: str, body: DecideBody) -> Any:
    decision = DECISIONS.get(body.decision.strip().lower())
    if decision is None:
        raise HTTPException(400, "decision must be approved or denied")
    if not body.by.startswith("human:"):
        raise HTTPException(403, f"{body.by} is not a human identity")
    try:
        from ..governance.approvals import decide_approval

        approval = await decide_approval(approval_id, decision, body.by, body.note)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise http_error(exc) from exc
    doc = jsonable(approval)
    eng = live_engine(approval.run_id)
    if eng is not None:
        eng.on_approval_update(doc)
    return J(doc)
