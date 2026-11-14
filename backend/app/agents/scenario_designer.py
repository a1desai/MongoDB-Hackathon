"""Scenario Designer: turn a free-form prompt into a runnable, versioned grid scenario.

The LLM infers the parameters, but Pydantic and ``build_scenario_document`` decide the
shape and safety bounds. With no LLM key, ``fallback_design`` extracts useful numbers from
the prompt and still produces a prompt-specific scenario.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, date, datetime
from typing import Any

from pydantic import BaseModel, Field

from ..config import get_settings
from ..db import SCENARIOS, db
from ..llm.client import complete_json, load_prompt, traced

AGENT_ID = "agent:scenario_designer"
DEFAULT_DATE = "2031-06-24"
DEFAULT_START = "12:00"
DEFAULT_END = "21:30"


class DesignedContingency(BaseModel):
    at: str = "17:40"
    type: str = "supply_loss"
    mw: float = Field(default=350.0, ge=0, le=20_000)
    label: str = "Scenario-designed supply loss"
    duration_min: int | None = Field(default=None, ge=5, le=720)


class ScenarioDesign(BaseModel):
    title: str = Field(default="Custom grid stress test", min_length=3, max_length=120)
    summary: str = Field(default="A prompt-designed grid stress test.", max_length=500)
    reasoning: str = Field(default="Parameters were inferred from the user's description.", max_length=1200)
    assumptions: list[str] = []
    date: str = DEFAULT_DATE
    sim_start: str = DEFAULT_START
    sim_end: str = DEFAULT_END
    max_temp_c: float = Field(default=39.0, ge=-50, le=60)
    temperature_profile_c: dict[str, float] = {}
    target_peak_mw: float = Field(default=4000.0, ge=100, le=50_000)
    supply_schedule_mw: dict[str, float] = {}
    contingencies: list[DesignedContingency] = []
    budget_cad: float = Field(default=60_000.0, ge=0, le=10_000_000)
    t_safe_c: float = Field(default=29.0, ge=20, le=35)
    peak_time: str = "17:00"
    commercial_background_share: float = Field(default=0.45, ge=0.05, le=0.9)


_TIME = re.compile(r"\b([01]?\d|2[0-3]):([0-5]\d)\b")
_TEMP = re.compile(r"(-?\d+(?:\.\d+)?)\s*(?:°\s*)?[cC]\b")
_PEAK = re.compile(
    r"(?:peak(?:\s+electricity)?\s+demand|demand(?:\s+(?:peaks?|reaches?|rises?|surges?)(?:\s+(?:at|to))?)?)"
    r"\D{0,20}([\d,]+(?:\.\d+)?)\s*MW",
    re.I,
)
_SUPPLY = re.compile(r"(?:available\s+)?supply\D{0,20}([\d,]+(?:\.\d+)?)\s*MW", re.I)
_LOSS = re.compile(
    r"([\d,]+(?:\.\d+)?)\s*MW\D{0,30}(generator|transmission|plant|unit|substation|intertie|line)",
    re.I,
)


def _number(match: re.Match[str] | None, default: float) -> float:
    if match is None:
        return default
    return float(match.group(1).replace(",", ""))


def _clean_title(prompt: str) -> str:
    text = " ".join(prompt.strip().split())
    if not text:
        return "Custom grid stress test"
    head = text.split(";", 1)[0].split(".", 1)[0]
    return head[:117] + ("…" if len(head) > 117 else "")


def fallback_design(prompt: str) -> ScenarioDesign:
    """Prompt-sensitive deterministic design used when the LLM is unavailable."""
    text = " ".join(str(prompt or "").split())
    temp_matches = [float(v) for v in _TEMP.findall(text)]
    max_temp = min(60.0, max(-50.0, max(temp_matches))) if temp_matches else 39.0
    peak = min(50_000.0, max(100.0, _number(_PEAK.search(text), 4000.0)))
    supply_match = _SUPPLY.search(text)
    supply = _number(supply_match, peak * 0.97)
    loss_match = _LOSS.search(text)
    loss = min(20_000.0, _number(loss_match, max(100.0, peak * 0.08)))
    times = [f"{int(h):02d}:{m}" for h, m in _TIME.findall(text)]
    event_at = times[0] if times else "17:40"
    peak_time = times[-1] if len(times) > 1 else "17:00"
    kind = loss_match.group(2).lower() if loss_match else "supply"
    minimum = max_temp - (10.0 if max_temp >= 20 else 5.0)
    profile = {
        DEFAULT_START: round(minimum + 3.0, 1),
        "16:00": round(max_temp, 1),
        DEFAULT_END: round(minimum + 2.0, 1),
    }
    assumptions = [
        "The existing synthetic Toronto grid and household population are used.",
        "Any narrative event is translated into weather, demand, supply, or supply-loss parameters.",
    ]
    return ScenarioDesign(
        title=_clean_title(text),
        summary=f"Prompt-designed stress test with a {max_temp:g} °C peak and a {loss:g} MW {kind} disruption.",
        reasoning=(
            f"The prompt implies a {max_temp:g} °C weather peak, about {peak:g} MW peak demand, "
            f"{supply:g} MW planned supply, and a {loss:g} MW disruption at {event_at}."
        ),
        assumptions=assumptions,
        max_temp_c=max_temp,
        temperature_profile_c=profile,
        target_peak_mw=peak,
        supply_schedule_mw={"00:00": supply, DEFAULT_START: supply, DEFAULT_END: supply},
        contingencies=[
            DesignedContingency(
                at=event_at,
                type=f"{kind}_loss" if kind != "generator" else "generator_trip",
                mw=loss,
                label=f"Scenario-designed {kind} disruption",
            )
        ],
        peak_time=peak_time,
    )


def _hhmm(value: str, default: str) -> str:
    m = _TIME.search(str(value or ""))
    return f"{int(m.group(1)):02d}:{m.group(2)}" if m else default


def _profile(values: dict[str, Any], fallback: dict[str, float], *, low: float, high: float) -> dict[str, float]:
    out: dict[str, float] = {}
    for key, value in (values or {}).items():
        try:
            out[_hhmm(str(key), "")] = round(min(max(float(value), low), high), 2)
        except (TypeError, ValueError):
            continue
    out.pop("", None)
    return out or fallback


def build_scenario_document(
    draft: ScenarioDesign,
    *,
    prompt: str,
    scenario_id: str,
    created_by: str,
    source: str,
    trace_id: str | None,
) -> dict[str, Any]:
    """Normalize an agent draft into the exact scenario shape consumed by the simulator."""
    sim_start = _hhmm(draft.sim_start, DEFAULT_START)
    sim_end = _hhmm(draft.sim_end, DEFAULT_END)
    if sim_end == sim_start:
        sim_end = DEFAULT_END if sim_start != DEFAULT_END else DEFAULT_START
    try:
        scenario_date = date.fromisoformat(draft.date).isoformat()
    except ValueError:
        scenario_date = DEFAULT_DATE
    max_temp = min(max(float(draft.max_temp_c), -50.0), 60.0)
    fallback_weather = {sim_start: round(max_temp - 7, 1), "16:00": round(max_temp, 1), sim_end: round(max_temp - 8, 1)}
    weather_profile = _profile(draft.temperature_profile_c, fallback_weather, low=-50, high=60)
    target = min(max(float(draft.target_peak_mw), 100.0), 50_000.0)
    fallback_supply = round(target * 0.97, 1)
    supply_profile = _profile(draft.supply_schedule_mw, {sim_start: fallback_supply}, low=0, high=75_000)
    supply_at_start = next((v for k, v in reversed(sorted(supply_profile.items())) if k <= sim_start), None)
    supply = float(supply_at_start if supply_at_start is not None else next(iter(supply_profile.values())))
    contingencies = []
    for c in draft.contingencies[:8]:
        contingencies.append(
            {
                "at": _hhmm(c.at, "17:40"),
                "type": str(c.type or "supply_loss")[:40],
                "mw": round(min(max(float(c.mw), 0.0), target), 1),
                "label": str(c.label or "Scenario-designed supply loss")[:160],
                **({"duration_min": int(c.duration_min)} if c.duration_min else {}),
            }
        )
    return {
        "_id": scenario_id,
        "id": scenario_id,
        "title": draft.title.strip()[:120],
        "summary": draft.summary.strip()[:500],
        "date": scenario_date,
        "weather": {"source": "scenario_designer", "delta_c": 0.0, "profile_c": weather_profile},
        "sim_start": sim_start,
        "sim_end": sim_end,
        "target_peak_mw": round(target, 1),
        "supply_mw": round(supply, 1),
        "supply_schedule_mw": supply_profile,
        "contingencies": contingencies,
        "designated_cool_spaces": 12,
        "budget_cad": round(min(max(float(draft.budget_cad), 0.0), 10_000_000.0), 2),
        "t_safe_c": round(min(max(float(draft.t_safe_c), 20.0), 35.0), 1),
        "scripted_messages": [],
        "demo": {"pause_on_planning": True, "pause_on_approval": True},
        "calibration": {
            "peak_time": _hhmm(draft.peak_time, "17:00"),
            "commercial_background_share": round(min(max(float(draft.commercial_background_share), 0.05), 0.9), 3),
        },
        "version": 1,
        "status": "ready",
        "created_by": created_by,
        "created_at": datetime.now(UTC),
        "designer": {
            "agent": AGENT_ID,
            "prompt": " ".join(prompt.split())[:4000],
            "reasoning": draft.reasoning.strip()[:1200],
            "assumptions": [str(x)[:300] for x in draft.assumptions[:12]],
            "source": source,
            "langsmith_run_id": trace_id,
        },
    }


@traced(AGENT_ID, tags=["scenario", "design"])
async def generate_and_store(prompt: str, created_by: str = "human:operator") -> dict[str, Any]:
    """Ask the Scenario Designer, validate its output, and persist the runnable version in Atlas."""
    clean = " ".join(str(prompt or "").split())[:4000]
    fallback = lambda: fallback_design(clean)
    result = await complete_json(
        agent=AGENT_ID,
        model=get_settings().llm_model_smart,
        system=load_prompt("scenario_designer"),
        payload={
            "scenario_prompt": clean,
            "simulation_capabilities": {
                "world": "synthetic Toronto households and feeder sections",
                "parameters": [
                    "temperature profile",
                    "peak demand",
                    "supply schedule",
                    "timed supply losses",
                    "budget",
                    "safe indoor limit",
                ],
            },
        },
        schema=ScenarioDesign,
        fallback=fallback,
        metadata={"agent_id": AGENT_ID, "created_by": created_by, "effort": "medium"},
    )
    scenario_id = f"scn_{secrets.token_hex(5)}"
    doc = build_scenario_document(
        result.output,  # type: ignore[arg-type]
        prompt=clean,
        scenario_id=scenario_id,
        created_by=created_by,
        source=result.source,
        trace_id=result.langsmith_run_id,
    )
    await db()[SCENARIOS].insert_one(doc)
    return doc
