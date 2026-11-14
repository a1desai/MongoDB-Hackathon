"""Auditor questions and ledger hash-chain verification (PLAN §8.2 Auditor, §10.5, §13)."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from .common import J, http_error

log = logging.getLogger("cooldown.api.audit")

router = APIRouter(tags=["audit"])


class AskBody(BaseModel):
    run_id: str
    question: str = Field(min_length=1, max_length=1000)
    by: str = "human:auditor"


@router.post("/audit/ask")
async def ask(body: AskBody) -> Any:
    if not body.by.startswith("human:"):
        raise HTTPException(403, f"{body.by} is not a human identity")
    try:
        from ..agents import auditor

        answer = await auditor.ask(body.run_id, body.question, body.by)
    except Exception as exc:
        log.exception("auditor failed")
        raise http_error(exc) from exc
    return J(answer)


@router.get("/ledger/verify")
async def verify(run_id: str = Query(...)) -> Any:
    try:
        from ..governance.ledger import verify_chain

        return J({"run_id": run_id, **await verify_chain(run_id)})
    except Exception as exc:
        raise http_error(exc) from exc
