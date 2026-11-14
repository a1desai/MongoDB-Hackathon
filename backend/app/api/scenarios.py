"""Prompt-designed scenarios: infer parameters with an agent and persist them in Atlas."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from .common import J, http_error

router = APIRouter(prefix="/scenarios", tags=["scenarios"])


class GenerateScenarioBody(BaseModel):
    prompt: str = Field(min_length=12, max_length=4000)
    by: str = "human:operator"


@router.post("/generate")
async def generate_scenario(body: GenerateScenarioBody) -> Any:
    if not body.by.startswith("human:"):
        raise HTTPException(403, f"{body.by} may not create scenarios")
    prompt = body.prompt.strip()
    if len(prompt) < 12:
        raise HTTPException(422, "Describe the scenario in at least 12 characters.")
    try:
        from ..agents import scenario_designer

        scenario = await scenario_designer.generate_and_store(prompt, body.by)
    except Exception as exc:
        raise http_error(exc) from exc
    return J({"scenario_id": scenario["_id"], "scenario": scenario})
