"""The LLM Planner path must never under-escalate: when the LLM's own proposal leaves the
shortfall materially uncovered without reaching feeder_outage, `_top_up_outages` tops the plan
up deterministically (PLAN §7: "LLMs propose; code decides"). This is the guardrail added after
a live run showed the LLM path proposing only business_curtail_request steps and taking three
uncontrolled trips while the fallback planner (same shortfall) takes zero."""

from app.agents.planner import _top_up_outages
from app.agents.fallback_planner import COVERAGE
from app.models import PlannerOutput, PlanStep, RUNGS, Selector, StepParams, Window
from tests.test_fallback_planner import make_ctx


def step(action: str, mw: float) -> PlanStep:
    return PlanStep(
        step_id="s1", rung=RUNGS[action], action_type=action, selector=Selector(),
        params=StepParams(mw_target=mw), window=Window(start="17:00", end="18:00"),
        expected_mw=mw, evidence_ids=["fc_test0001"],
    )


def test_tops_up_when_llm_plan_under_covers() -> None:
    ctx = make_ctx(400.0)  # a large shortfall relative to the non-outage rungs' combined ~100 MW
    llm_plan = PlannerOutput(
        rationale="LLM: curtailed what was willing.",
        expected_shortfall_mw=400.0,
        steps=[step("business_curtail_request", 15.0)],
    )
    topped_up = _top_up_outages(llm_plan, ctx)
    assert any(s.action_type == "feeder_outage" for s in topped_up.steps)
    total_mw = sum(s.expected_mw for s in topped_up.steps)
    assert total_mw >= COVERAGE * 400.0 - 1.0  # within rounding of the target
    assert "top-up" in topped_up.rationale.lower()


def test_no_top_up_when_llm_plan_already_covers() -> None:
    ctx = make_ctx(10.0)  # small shortfall, easily covered by the first rungs
    llm_plan = PlannerOutput(
        rationale="LLM: precooled and curtailed.",
        expected_shortfall_mw=10.0,
        steps=[step("precool", 20.0)],
    )
    topped_up = _top_up_outages(llm_plan, ctx)
    assert topped_up is llm_plan  # unchanged object: no top-up was needed
    assert not any(s.action_type == "feeder_outage" for s in topped_up.steps)


def test_no_shortfall_is_a_noop() -> None:
    ctx = make_ctx(0.0)
    llm_plan = PlannerOutput(rationale="LLM: nothing to do.", expected_shortfall_mw=0.0, steps=[])
    assert _top_up_outages(llm_plan, ctx) is llm_plan
