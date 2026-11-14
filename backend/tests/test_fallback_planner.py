"""PLAN §19: the fallback covers >= 1.15 x shortfall using rungs in order and never proposes
an outage while lower rungs remain."""

from app.agents.fallback_planner import COVERAGE, LADDER, fallback_plan
from app.models import RUNGS, PlannerOutput


def make_ctx(shortfall: float, *, now: str = "12:00", window=("17:00", "20:00"), precool=40.0,
             battery=20.0, ev=30.0, setback_per_c=60.0, sms=10.0, businesses=None, sections=None) -> dict:
    return {
        "now": now,
        "forecast": {
            "id": "fc_test0001",
            "horizon_end": "21:30",
            "windows": [{"start": window[0], "end": window[1], "peak_short_mw": shortfall}] if shortfall else [],
        },
        "resources": {
            "thermostats": {"precool_mw": precool, "setback_mw_per_c": setback_per_c},
            "batteries": {"mw": battery},
            "ev": {"pausable_mw": ev},
            "businesses": businesses if businesses is not None else [
                {"id": "biz_northline_dc", "name": "Northline Data Centre", "status": "available",
                 "max_mw": 15, "notice_min": 30, "protected": False},
                {"id": "biz_ashbridge_water", "name": "Ashbridge Water Treatment", "status": "available",
                 "max_mw": 0, "notice_min": 0, "protected": True},
            ],
            "sms": {"mw_potential": sms, "amount_cad": 5.0},
            "sections": sections if sections is not None else [
                {"id": f"TOR-S{i:02d}", "mw": 150.0, "eligible": i % 4 != 0, "cut_count_today": 0,
                 "cut_minutes_today": 0}
                for i in range(1, 25)
            ],
        },
        "similar_past_events": [{"id": "mem_0001"}, {"id": "mem_0002"}, {"id": "mem_0003"}],
    }


def total(plan: PlannerOutput) -> float:
    return sum(s.expected_mw for s in plan.steps)


def assert_ladder_order(plan: PlannerOutput) -> None:
    rungs = [s.rung for s in plan.steps]
    assert rungs == sorted(rungs), f"rungs out of order: {rungs}"
    for s in plan.steps:
        assert s.rung == RUNGS[s.action_type]


def test_ladder_is_least_harm_first():
    assert LADDER[0] == "precool" and LADDER[-1] == "feeder_outage"


def test_no_shortfall_no_steps():
    plan = fallback_plan(make_ctx(0))
    assert plan.steps == [] and plan.expected_shortfall_mw == 0


def test_small_shortfall_covered_by_lowest_rungs_only():
    plan = fallback_plan(make_ctx(40.0))
    assert total(plan) >= COVERAGE * 40.0 - 1e-6
    assert_ladder_order(plan)
    kinds = {s.action_type for s in plan.steps}
    assert "feeder_outage" not in kinds
    assert "thermostat_setback" not in kinds  # 46 MW target is met by precool + battery
    assert plan.steps[0].action_type == "precool"


def test_moderate_shortfall_uses_rungs_in_order_without_outage():
    shortfall = 150.0  # lower rungs hold 40+20+30+10.5+120+10 = 230 MW
    plan = fallback_plan(make_ctx(shortfall))
    assert total(plan) >= COVERAGE * shortfall - 1e-6
    assert_ladder_order(plan)
    assert all(s.action_type != "feeder_outage" for s in plan.steps)
    # every step cites evidence
    assert all("fc_test0001" in s.evidence_ids for s in plan.steps)


def test_outage_only_after_all_lower_rungs_exhausted():
    shortfall = 300.0  # 345 MW target > 230 MW of lower rungs
    ctx = make_ctx(shortfall)
    plan = fallback_plan(ctx)
    assert total(plan) >= COVERAGE * shortfall - 1e-6
    assert_ladder_order(plan)
    outages = [s for s in plan.steps if s.action_type == "feeder_outage"]
    assert outages, "a gap beyond every lower rung needs an outage"
    lower = [s for s in plan.steps if s.action_type != "feeder_outage"]
    # each lower rung was used to its full potential before any outage
    by_type: dict[str, float] = {}
    for s in lower:
        by_type[s.action_type] = by_type.get(s.action_type, 0) + s.expected_mw
    assert by_type["precool"] == 40.0
    assert by_type["battery_discharge"] == 20.0
    assert by_type["ev_pause"] == 30.0
    assert by_type["thermostat_setback"] == 120.0
    assert by_type["send_sms"] == 10.0
    # outages: eligible sections only, one per step, at most 60 minutes
    eligible = {s["id"] for s in ctx["resources"]["sections"] if s["eligible"]}
    for s in outages:
        assert len(s.selector.sections) == 1 and s.selector.sections[0] in eligible
        assert s.params.duration_min == 60


def test_no_outage_before_window_lower_rungs_with_capacity():
    # Lower rungs could cover everything: an outage must never appear.
    for shortfall in (10, 50, 100, 150, 190):
        plan = fallback_plan(make_ctx(float(shortfall)))
        assert all(s.action_type != "feeder_outage" for s in plan.steps), shortfall
        assert total(plan) >= COVERAGE * shortfall - 1e-6


def test_precool_skipped_when_window_already_started():
    plan = fallback_plan(make_ctx(100.0, now="17:45", window=("17:40", "20:00")))
    assert all(s.action_type != "precool" for s in plan.steps)
    assert_ladder_order(plan)
    assert total(plan) >= COVERAGE * 100.0 - 1e-6


def test_business_notice_period_respected():
    plan = fallback_plan(make_ctx(110.0, now="17:45", window=("17:40", "20:00")))
    biz = [s for s in plan.steps if s.action_type == "business_curtail_request"]
    assert biz and biz[0].window.start >= "18:15"  # 30 min notice for the data centre
    assert all("biz_ashbridge_water" not in s.selector.business_ids for s in biz)


def test_setback_respects_program_limits():
    plan = fallback_plan(make_ctx(150.0, window=("16:00", "21:30")))
    sb = [s for s in plan.steps if s.action_type == "thermostat_setback"]
    assert sb
    assert all(-2.0 <= (s.params.offset_c or 0) <= 2.0 for s in sb)
    assert all((s.params.duration_min or 0) <= 240 for s in sb)


def test_rationale_fits_schema():
    plan = fallback_plan(make_ctx(400.0))
    assert len(plan.rationale) <= 600
    PlannerOutput.model_validate(plan.model_dump())


# ---- integration fixes: least-harm outage selection (R05 / R11 / R12 aware), committed outages


def _sec(i: int, mw: float, q: int, *, vul_over: int = 0, safe: int | None = 60, cut_min: int = 0,
         in_flight: bool = False, protected: bool = False) -> dict:
    return {"id": f"TOR-S{i:02d}", "mw": mw, "eligible": not protected and not in_flight, "protected": protected,
            "homes_real": 20000,
            "cut_count_today": 1 if cut_min else 0, "cut_minutes_today": cut_min, "income_quintile": q,
            "vulnerable_over_t_safe_if_cut": vul_over, "safe_duration_min": safe, "outage_in_flight": in_flight}


def _outages(plan: PlannerOutput) -> list:
    return [s for s in plan.steps if s.action_type == "feeder_outage"]


def _tight(**kw) -> dict:
    return make_ctx(300.0, now="17:40", window=("17:45", "19:30"), precool=0, battery=0, ev=0, setback_per_c=0,
                    sms=0, businesses=[], **kw)


def test_outage_prefers_sections_safe_for_vulnerable_homes():
    ctx = _tight(sections=[_sec(1, 200, 1, vul_over=2, safe=None), _sec(2, 180, 2), _sec(3, 190, 3)])
    picked = [s.selector.sections[0] for s in _outages(fallback_plan(ctx))]
    assert picked[:2] == ["TOR-S02", "TOR-S03"] or picked[:2] == ["TOR-S03", "TOR-S02"]
    assert "TOR-S01" not in picked[:2]


def test_outage_starts_after_the_r12_notice_period():
    plan = fallback_plan(_tight(sections=[_sec(2, 400, 2)]))
    (o,) = _outages(plan)
    assert o.window.start == "17:55"  # 17:40 + 15 min notice


def test_outage_uses_shorter_safe_duration():
    plan = fallback_plan(_tight(sections=[_sec(2, 400, 2, vul_over=1, safe=30)]))
    (o,) = _outages(plan)
    assert o.params.duration_min == 30 and o.window.end == "18:25"


def test_committed_outages_count_against_the_need():
    ctx = _tight(sections=[_sec(2, 200, 2), _sec(3, 200, 3), _sec(4, 200, 4)])
    ctx["resources"]["outages_committed_mw"] = 400.0  # already approved, not yet switched
    assert _outages(fallback_plan(ctx)) == []


def test_outages_keep_quintile_gap_within_r11():
    # R11 compares average outage minutes per home by quintile. Q1's only section already lost 30 min
    # (an uncontrolled trip): another 60 there makes Q1 average 90 vs Q2 0, a 90-min gap, so pick Q2.
    s5 = _sec(5, 350, 1)
    s5["cut_minutes_today"] = 30
    ctx = _tight(sections=[s5, _sec(2, 300, 2)])
    picked = [s.selector.sections[0] for s in _outages(fallback_plan(ctx))]
    assert picked[0] == "TOR-S02"
