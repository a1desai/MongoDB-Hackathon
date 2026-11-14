"""HTTP contract for turning a free-form prompt into a stored scenario."""

from __future__ import annotations

import importlib

import httpx
import pytest
from fastapi import FastAPI


@pytest.mark.asyncio
async def test_generate_scenario_endpoint_returns_the_agent_designed_parameters(monkeypatch):
    scenarios_api = importlib.import_module("app.api.scenarios")
    scenario_designer = importlib.import_module("app.agents.scenario_designer")

    async def fake_generate(prompt: str, created_by: str):
        assert prompt == "A 44 C heat dome and a 700 MW intertie failure at 18:20"
        assert created_by == "human:operator"
        return {
            "_id": "scn_http_test",
            "id": "scn_http_test",
            "title": "Intertie heat-dome test",
            "weather": {"profile_c": {"12:00": 35, "16:00": 44, "21:30": 36}},
            "target_peak_mw": 5200,
            "supply_mw": 5000,
            "supply_schedule_mw": {"12:00": 5000},
            "contingencies": [{"at": "18:20", "type": "intertie_loss", "mw": 700}],
            "budget_cad": 75000,
            "t_safe_c": 29,
            "designer": {"prompt": prompt, "reasoning": "Mapped the intertie failure to lost supply.", "source": "llm"},
        }

    monkeypatch.setattr(scenario_designer, "generate_and_store", fake_generate)
    app = FastAPI()
    app.include_router(scenarios_api.router)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/scenarios/generate",
            json={
                "prompt": "A 44 C heat dome and a 700 MW intertie failure at 18:20",
                "by": "human:operator",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["scenario_id"] == "scn_http_test"
    assert body["scenario"]["contingencies"][0]["mw"] == 700
    assert body["scenario"]["designer"]["source"] == "llm"
