"""Prompt-designed scenarios: inference, normalization, weather, and Atlas resolution."""

from __future__ import annotations

import importlib

import pytest

from app.llm.client import LLMResult
from app.sim import scenario as scenario_mod
from app.sim import weather


def _designer():
    return importlib.import_module("app.agents.scenario_designer")


def test_fallback_design_uses_numbers_and_time_from_the_prompt():
    designer = _designer()

    draft = designer.fallback_design(
        "At 18:15 a 600 MW generator fails during a 42 C heat wave; peak demand reaches 5,000 MW."
    )

    assert draft.max_temp_c == 42.0
    assert draft.target_peak_mw == 5000.0
    assert draft.contingencies[0].at == "18:15"
    assert draft.contingencies[0].mw == 600.0
    assert "generator" in draft.contingencies[0].label.lower()


def test_fallback_design_distinguishes_demand_supply_and_event_loss():
    designer = _designer()

    draft = designer.fallback_design(
        "At 18:30, a freezing rain storm drops Toronto to -12 C, demand reaches 7100 MW, "
        "supply falls to 5900 MW, and a 650 MW transmission corridor fails."
    )

    assert draft.target_peak_mw == 7100.0
    assert draft.supply_schedule_mw["12:00"] == 5900.0
    assert draft.contingencies[0].mw == 650.0
    assert "transmission" in draft.contingencies[0].label.lower()


def test_build_scenario_document_preserves_prompt_and_builds_runnable_profiles():
    designer = _designer()
    prompt = "A humid 40 C evening with a 450 MW transmission loss at 17:30."
    draft = designer.fallback_design(prompt)

    doc = designer.build_scenario_document(
        draft,
        prompt=prompt,
        scenario_id="scn_testprompt",
        created_by="human:operator",
        source="fallback",
        trace_id=None,
    )

    assert doc["_id"] == "scn_testprompt"
    assert doc["id"] == "scn_testprompt"
    assert doc["designer"]["prompt"] == prompt
    assert doc["designer"]["source"] == "fallback"
    assert doc["weather"]["profile_c"]
    assert doc["supply_schedule_mw"]
    assert doc["scripted_messages"] == []
    assert doc["contingencies"][0]["mw"] == 450.0
    assert doc["target_peak_mw"] > doc["supply_mw"]


def test_build_scenario_uses_the_latest_supply_step_at_sim_start():
    designer = _designer()
    draft = designer.ScenarioDesign(
        sim_start="12:00",
        target_peak_mw=6000,
        supply_schedule_mw={"00:00": 5500, "08:00": 5200, "12:00": 4800, "18:00": 4300},
    )

    doc = designer.build_scenario_document(
        draft,
        prompt="Supply falls in steps before an evening grid emergency.",
        scenario_id="scn_supply_steps",
        created_by="human:operator",
        source="fallback",
        trace_id=None,
    )

    assert doc["supply_mw"] == 4800


def test_synthetic_weather_uses_a_generated_temperature_profile():
    scenario = {
        "weather": {
            "source": "scenario_designer",
            "delta_c": 0.0,
            "profile_c": {"12:00": 31.0, "16:00": 44.0, "21:00": 33.0},
        }
    }

    cache = weather.synthetic_cache(scenario, 12.0, 21.0)
    at_1600 = cache["minutes"].index(16 * 60.0)

    assert cache["temps"][at_1600] == 44.0
    assert cache["temps"][0] == 31.0
    assert cache["temps"][-1] == 33.0


@pytest.mark.asyncio
async def test_resolve_scenario_prefers_a_custom_atlas_document(monkeypatch):
    class Scenarios:
        async def find_one(self, query, projection):
            assert query == {"_id": "scn_custom"}
            assert projection == {"_id": 0}
            return {
                "_id": "scn_custom",
                "id": "scn_custom",
                "title": "Custom ice storm",
                "date": "2032-01-12",
                "sim_start": "06:00",
                "sim_end": "14:00",
                "weather": {"source": "scenario_designer", "profile_c": {"06:00": -8, "14:00": -3}},
                "target_peak_mw": 3200,
                "supply_mw": 2700,
                "contingencies": [],
                "scripted_messages": [],
            }

    class Database:
        def __getitem__(self, name):
            assert name == "scenarios"
            return Scenarios()

    monkeypatch.setattr(scenario_mod, "db", lambda: Database(), raising=False)

    loaded = await scenario_mod.resolve_scenario("scn_custom")

    assert loaded["id"] == "scn_custom"
    assert loaded["title"] == "Custom ice storm"
    assert loaded["sim_start"] == "06:00"
    assert loaded["contingencies"] == []
    assert loaded["scripted_messages"] == []


@pytest.mark.asyncio
async def test_generate_and_store_persists_the_validated_agent_scenario(monkeypatch):
    designer = _designer()
    inserted = []

    class Scenarios:
        async def insert_one(self, doc):
            inserted.append(doc)

    class Database:
        def __getitem__(self, name):
            assert name == "scenarios"
            return Scenarios()

    async def fake_complete_json(**kwargs):
        prompt = kwargs["payload"]["scenario_prompt"]
        return LLMResult(output=designer.fallback_design(prompt), source="fallback")

    monkeypatch.setattr(designer, "db", lambda: Database())
    monkeypatch.setattr(designer, "complete_json", fake_complete_json)

    doc = await designer.generate_and_store("A 41 C heat dome with a 500 MW line failure at 18:05.")

    assert inserted == [doc]
    assert doc["_id"].startswith("scn_")
    assert doc["designer"]["agent"] == "agent:scenario_designer"
    assert doc["designer"]["prompt"].startswith("A 41 C heat dome")
    assert doc["contingencies"][0]["mw"] == 500.0
