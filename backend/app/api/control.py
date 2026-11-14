"""Kill switch and rollback (PLAN §10.4, §13).

halt/resume write agent_control (the change stream flips the live World's flag; this endpoint
also does it directly). Rollback creates a gated undo request and runs it through the request
graph right away, so it works while the clock is paused.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from ..db import AGENT_CONTROL, db
from .common import J, http_error, jsonable, live_engine

router = APIRouter(tags=["control"])


class HaltBody(BaseModel):
    run_id: str
    scope: str = "all"
    by: str
    reason: str = "halted from the console"


class ResumeBody(BaseModel):
    run_id: str
    by: str


class RollbackBody(BaseModel):
    by: str
    reason: str = "rolled back from the console"


@router.get("/control/{run_id}")
async def get_control(run_id: str) -> Any:
    doc = await db()[AGENT_CONTROL].find_one({"_id": run_id})
    return J(doc or {"_id": run_id, "halted": False, "scope": "all"})


@router.post("/control/halt")
async def halt(body: HaltBody) -> Any:
    try:
        from ..governance import control

        doc = jsonable(await control.halt(body.run_id, body.scope or "all", body.by, body.reason))
    except Exception as exc:
        raise http_error(exc) from exc
    eng = live_engine(body.run_id)
    if eng is not None:
        eng.on_control_update(doc)
    return J(doc)


@router.post("/control/resume")
async def resume(body: ResumeBody) -> Any:
    try:
        from ..governance import control

        doc = jsonable(await control.resume(body.run_id, body.by))
    except Exception as exc:
        raise http_error(exc) from exc
    eng = live_engine(body.run_id)
    if eng is not None:
        eng.on_control_update(doc)
    return J(doc)


@router.post("/executions/{execution_id}/rollback")
async def rollback(execution_id: str, body: RollbackBody) -> Any:
    if not body.by.startswith("human:"):
        raise HTTPException(403, f"{body.by} is not a human identity")
    try:
        from ..governance.rollback import request_rollback

        req = await request_rollback(execution_id, body.by, body.reason)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    except Exception as exc:
        raise http_error(exc) from exc
    eng = live_engine(req.run_id)
    if eng is not None:
        eng.submit_request(req.id)
    return J(req)
