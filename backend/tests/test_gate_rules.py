"""PLAN §19: at least one allow and one deny per rule, batch partial_allow counts, precedence.

Synthetic World + hand-built GateContext (seed identities/policies), no database.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from app.governance import gate
from app.governance.context import GateContext
from app.governance.rules import (
    ALL_RULES,
    r01_scope,
    r02_consent,
    r03_program_limits,
    r04_vulnerable,
    r05_safe_indoor,
    r06_messaging,
    r07_ladder,
    r08_protected,
    r09_outage_duration,
    r10_rotation,
    r11_equity,
    r12_prenotify,
    r13_approval,
    r14_untrusted,
    r15_kill_switch,
    r16_restore,
)
from app.governance.seed import IDENTITIES, POLICIES, POLICY_VERSION
from app.models import ACTION_SYSTEM, ActionRequest, Evidence, Identity, OutboundMessage, Targets, Window
from app.sim.world_state import World
from app.timeutil import TZ, sim_iso

N, S = 20, 4
VULNERABLE = (3, 7)  # sections 3 and 3 (idx % 4)
NO_CONSENT = (1, 5)
STOP = "Reply STOP to opt out"
START = datetime(2031, 6, 24, 12, 0, tzinfo=TZ)


def make_world(n: int = N, s: int = S, scale: float = 100.0, now: str = "15:00") -> World:
    h, m = (int(x) for x in now.split(":"))
    sim_now = START.replace(hour=h, minute=m)
    f = lambda v, dt=float: np.full(n, v, dtype=dt)  # noqa: E731
    vul = np.zeros(n, dtype=bool)
    vul[list(VULNERABLE)] = True
    consent = np.ones(n, dtype=bool)
    consent[list(NO_CONSENT)] = False
    return World(
        run_id="run_test01", mode="governed", scenario={"t_safe_c": 29.0, "budget_cad": 60000}, scale=scale,
        sim_start=START, sim_end=START.replace(hour=21, minute=30), sim_now=sim_now, tick=36,
        rng=np.random.default_rng(0),
        hh_ids=[f"hh_{i + 1:05d}" for i in range(n)], lon=f(-79.4), lat=f(43.7),
        section_idx=np.arange(n) % s, archetype_idx=np.zeros(n, dtype=int), neighbourhood_idx=np.zeros(n, dtype=int),
        income_quintile=(np.arange(n) % 5) + 1, dwelling=np.zeros(n, dtype=np.int8), ac_type=f(2, np.int8),
        has_thermostat=f(True, bool), has_ev=f(True, bool), has_battery=f(True, bool), vulnerable=vul,
        consent_thermostat=consent.copy(), consent_ev=consent.copy(), consent_battery=consent.copy(), sms_opt_in=f(True, bool),
        k_env=f(0.25), k_ac=f(4.5), ac_kw=f(3.0), solar_gain=f(0.2), setpoint_base=f(23.5), comfort_max=f(26.5),
        base_kw_scale=f(1.0), ev_arrival_h=f(17.75), featured={"res_01": 3, "res_02": 4}, injection_resident_id="res_02",
        t_in=f(24.0), duty=f(0.6), p_kw=f(2.5), power_on=f(True, bool), offset_c=f(0.0), precool_active=f(False, bool),
        setback_active=f(False, bool), setback_count_today=f(0, int), overridden=f(False, bool), sms_accepted=f(False, bool),
        sms_count_today=f(0, int), sms_stopped=f(False, bool), ev_charging=f(True, bool), ev_paused=f(False, bool),
        ev_kwh_needed=f(10.0), battery_soc=f(0.9), battery_discharging=f(False, bool), battery_kw=f(5.0),
        notified_cool_space=f(False, bool), notified_sim_minute=f(-1, int), over_t_safe_minutes=f(0, int), outage_minutes=f(0, int),
        section_ids=[f"TOR-S{i + 1:02d}" for i in range(s)], section_protected=np.array([True] + [False] * (s - 1)),
        section_quintile=np.array([1, 2, 3, 4][:s]), section_households=np.bincount(np.arange(n) % s, minlength=s),
        section_cut=np.zeros(s, dtype=bool), section_cut_until=[None] * s, section_cut_minutes_today=np.zeros(s, dtype=int),
        section_cut_count_today=np.zeros(s, dtype=int), section_demand_mw=np.full(s, 50.0),
        archetype_ids=["a0"], archetype_params={}, neighbourhood_ids=["n0"], businesses={}, facilities=[],
    )


def no_rise(world, idx, offset, start, end, power_off, t_out):
    return world.t_in[np.asarray(idx)].astype(float)


def make_ctx(world: World | None = None, **kw) -> GateContext:
    world = world or make_world()
    base = dict(
        world=world, run={"_id": world.run_id, "scale": world.scale}, policies={p.id: p for p in POLICIES},
        policy_version=POLICY_VERSION, identities={i.id: i for i in IDENTITIES}, sim_now=world.sim_now, halted=False,
        halted_scope="all", t_safe_c=29.0, budget_left_cad=60000.0, lower_rung_potential_mw=0.0, active_delivered_mw=0.0,
        projected_shortfall_mw=100.0, margin_mw=50.0, predictor=no_rise,
    )
    base.update(kw)
    return GateContext(**base)


_n = 0


def req(action: str, *, hh: list[int] | None = None, section: str | None = None, messages: list[OutboundMessage] | None = None,
        params: dict | None = None, by: str | None = None, evidence: list[Evidence] | None = None, window=("15:30", "17:00"),
        expected_mw: float = 10.0, undo_of: str | None = None) -> ActionRequest:
    global _n
    _n += 1
    rid = f"ar_test{_n:04d}"
    default_by = {"send_sms": "agent:negotiator", "business_curtail_request": "agent:negotiator",
                  "thermostat_restore": "agent:restorer", "feeder_restore": "agent:restorer", "undo": "agent:restorer"}
    targets = Targets(household_ids=[f"hh_{i + 1:05d}" for i in (hh or [])], section_id=section,
                      recipient_ids=[m.to for m in messages or []])
    return ActionRequest(
        _id=rid, run_id="run_test01", created_sim_ts="2031-06-24T15:00:00-04:00", sim_ts="2031-06-24T15:00:00-04:00",
        action_type=action, system=ACTION_SYSTEM.get(action, "thermlink"), proposed_by=by or default_by.get(action, "agent:targeter"),
        acting_for="human:supervisor", summary=f"test {action}", targets=targets, params=params or {},
        window=Window(start=window[0], end=window[1]) if window else None, messages=messages or [],
        evidence=evidence or [Evidence(id="fc_test0001", kind="forecast")], expected_mw=expected_mw,
        idempotency_key=rid, undo_of=undo_of,
    )


def sms(i: int, body: str = f"CoolShift: earn a $5 credit by easing AC 17:00-19:00. {STOP}", offer: dict | None = None) -> OutboundMessage:
    return OutboundMessage(to=f"hh_{i + 1:05d}", body=body, offer=offer if offer is not None else {"type": "reduce_cooling", "amount_cad": 5})


ALL = list(range(N))
SETBACK = {"offset_c": 2.0, "duration_min": 90}
PRECOOL = {"offset_c": -2.0, "duration_min": 90}
OUTAGE = {"duration_min": 60}


def denied(res) -> int:
    return int(res.counts.get("denied", 0))


# ---------------------------------------------------------------- R01 SCOPE

def test_r01_allow_scoped_agent():
    assert r01_scope.check(req("precool", hh=ALL, params=PRECOOL), make_ctx()).passed


def test_r01_deny_missing_scope_and_off_shift():
    assert not r01_scope.check(req("precool", hh=ALL, params=PRECOOL, by="agent:forecaster"), make_ctx()).passed
    ids = {i.id: i for i in IDENTITIES}
    ids["human:supervisor"] = Identity(_id="human:supervisor", kind="human", display_name="A. Rahman", on_shift=False)
    res = r01_scope.check(req("precool", hh=ALL, params=PRECOOL), make_ctx(identities=ids))
    assert not res.passed and "not on shift" in res.detail


def test_r01_only_executor_executes():
    ids = {i.id: i for i in IDENTITIES}
    r = req("precool", hh=ALL, params=PRECOOL)
    assert r01_scope.check_execute("agent:executor", r, ids).passed
    assert not r01_scope.check_execute("agent:planner", r, ids).passed


# ---------------------------------------------------------------- R02 CONSENT

def test_r02_allow_consented():
    assert r02_consent.check(req("thermostat_setback", hh=[0, 2, 4], params=SETBACK), make_ctx()).passed


def test_r02_deny_per_target():
    res = r02_consent.check(req("thermostat_setback", hh=ALL, params=SETBACK), make_ctx())
    assert not res.passed and denied(res) == len(NO_CONSENT)


def test_r02_sms_stopped_denied():
    w = make_world()
    w.sms_stopped[0] = True
    res = r02_consent.check(req("send_sms", messages=[sms(0), sms(2)]), make_ctx(w))
    assert denied(res) == 1


# ---------------------------------------------------------------- R03 PROGRAM_LIMITS

def test_r03_allow_within_limits():
    assert r03_program_limits.check(req("precool", hh=ALL, params=PRECOOL), make_ctx()).passed


def test_r03_deny_plus_4c_and_long_precool():
    res = r03_program_limits.check(req("thermostat_setback", hh=ALL, params={"offset_c": 4.0}), make_ctx())
    assert not res.passed and denied(res) == N and "outside program limits" in res.detail
    long = req("precool", hh=ALL, params={"offset_c": -2.0, "duration_min": 180})
    assert not r03_program_limits.check(long, make_ctx()).passed


def test_r03_one_setback_per_day():
    w = make_world()
    w.setback_count_today[[0, 2]] = 1
    res = r03_program_limits.check(req("thermostat_setback", hh=ALL, params=SETBACK), make_ctx(w))
    assert denied(res) == 2


# ---------------------------------------------------------------- R04 VULNERABLE_EXEMPT

def test_r04_allow_precool_and_cool_space_notice():
    assert r04_vulnerable.check(req("precool", hh=ALL, params=PRECOOL), make_ctx()).passed
    notice = sms(3, f"Lakeshore Grid Ops: nearest open cool space is Elm Library. {STOP}", {"type": "cool_space_notice", "amount_cad": 0})
    assert r04_vulnerable.check(req("send_sms", messages=[notice]), make_ctx()).passed


def test_r04_deny_setback_and_reduce_cooling_offer():
    res = r04_vulnerable.check(req("thermostat_setback", hh=ALL, params=SETBACK), make_ctx())
    assert denied(res) == len(VULNERABLE)
    res = r04_vulnerable.check(req("send_sms", messages=[sms(3), sms(4)]), make_ctx())
    assert denied(res) == 1


# ---------------------------------------------------------------- R05 SAFE_INDOOR

def test_r05_allow_when_homes_stay_cool():
    assert r05_safe_indoor.check(req("thermostat_setback", hh=ALL, params=SETBACK), make_ctx()).passed


def test_r05_deny_homes_pushed_over_t_safe():
    def hot(world, idx, offset, start, end, power_off, t_out):
        idx = np.asarray(idx)
        base = np.full(idx.size, 27.0)
        return np.where(np.isin(idx, [0, 2]) & (offset > 0), 30.0, base)

    res = r05_safe_indoor.check(req("thermostat_setback", hh=ALL, params=SETBACK), make_ctx(predictor=hot))
    assert not res.passed and denied(res) == 2


def test_r05_outage_denied_if_vulnerable_overheats():
    def outage_hot(world, idx, offset, start, end, power_off, t_out):
        return np.full(np.asarray(idx).size, 31.0 if power_off else 26.0)

    res = r05_safe_indoor.check(req("feeder_outage", section="TOR-S04", params=OUTAGE), make_ctx(predictor=outage_hot))
    assert not res.passed  # TOR-S04 (idx 3) holds vulnerable homes 3 and 7
    assert r05_safe_indoor.check(req("feeder_outage", section="TOR-S03", params=OUTAGE), make_ctx(predictor=outage_hot)).passed


def test_r05_default_predictor_runs():
    """No injected predictor: sim.thermal (or the local model) predicts; a mild setback stays safe."""
    ctx = make_ctx(predictor=None)
    res = r05_safe_indoor.check(req("thermostat_setback", hh=[0, 2], params=SETBACK), ctx)
    assert res.applies and res.passed, res.detail


# ---------------------------------------------------------------- R06 MESSAGING

def test_r06_allow_clean_message():
    assert r06_messaging.check(req("send_sms", messages=[sms(0), sms(2)]), make_ctx()).passed


def test_r06_deny_content_rate_and_incentive():
    w = make_world()
    w.sms_count_today[6] = 2
    msgs = [
        sms(0),  # ok
        sms(2, f"Call your doctor if you feel unwell. {STOP}"),  # denylist
        sms(4, "CoolShift: earn $5 by easing AC."),  # missing STOP
        sms(6),  # rate limit
        sms(8, offer={"type": "reduce_cooling", "amount_cad": 25}),  # incentive > $10
        sms(10, "x" * 330 + STOP),  # too long
    ]
    res = r06_messaging.check(req("send_sms", messages=msgs), make_ctx(w))
    assert denied(res) == 5
    assert res.counts["denylist"] == 1 and res.counts["missing_stop"] == 1 and res.counts["rate_limit"] == 1
    assert res.counts["incentive"] == 1 and res.counts["too_long"] == 1


def test_r06_deny_quiet_hours_and_budget():
    w = make_world(now="21:00")
    w.sim_now = w.sim_now.replace(hour=22, minute=30)
    assert not r06_messaging.check(req("send_sms", messages=[sms(0)]), make_ctx(w, sim_now=w.sim_now)).passed
    res = r06_messaging.check(req("send_sms", messages=[sms(0), sms(2)]), make_ctx(budget_left_cad=7.0))
    assert denied(res) == 1 and res.counts["budget"] == 1


# ---------------------------------------------------------------- R07 LADDER

def test_r07_allow_when_lower_rungs_exhausted():
    assert r07_ladder.check(req("feeder_outage", section="TOR-S02", params=OUTAGE), make_ctx(projected_shortfall_mw=300, lower_rung_potential_mw=100, active_delivered_mw=150)).passed


def test_r07_deny_when_lower_rungs_cover():
    res = r07_ladder.check(req("feeder_outage", section="TOR-S02", params=OUTAGE), make_ctx(projected_shortfall_mw=300, lower_rung_potential_mw=200, active_delivered_mw=150))
    assert not res.passed and res.counts["residual_mw"] == -50


# ---------------------------------------------------------------- R08 PROTECTED_SECTION

def test_r08_allow_unprotected():
    assert r08_protected.check(req("feeder_outage", section="TOR-S02", params=OUTAGE), make_ctx()).passed


def test_r08_deny_protected_and_unknown():
    assert not r08_protected.check(req("feeder_outage", section="TOR-S01", params=OUTAGE), make_ctx()).passed
    assert not r08_protected.check(req("feeder_outage", section="TOR-S99", params=OUTAGE), make_ctx()).passed


# ---------------------------------------------------------------- R09 OUTAGE_DURATION

def test_r09_allow_first_hour():
    assert r09_outage_duration.check(req("feeder_outage", section="TOR-S02", params=OUTAGE), make_ctx()).passed


def test_r09_deny_over_daily_cap():
    w = make_world()
    w.section_cut_minutes_today[1] = 30
    res = r09_outage_duration.check(req("feeder_outage", section="TOR-S02", params=OUTAGE), make_ctx(w))
    assert not res.passed and res.counts["minutes_after"] == 90


# ---------------------------------------------------------------- R10 ROTATION

def test_r10_allow_uncut_section():
    w = make_world()
    w.section_cut_count_today[1] = 1
    assert r10_rotation.check(req("feeder_outage", section="TOR-S03", params=OUTAGE), make_ctx(w)).passed


def test_r10_deny_second_cut_while_others_uncut():
    w = make_world()
    w.section_cut_count_today[2] = 1  # TOR-S03 already cut; TOR-S02, TOR-S04 not
    res = r10_rotation.check(req("feeder_outage", section="TOR-S03", params=OUTAGE), make_ctx(w))
    assert not res.passed and res.counts["eligible_uncut"] == 2


# ---------------------------------------------------------------- R11 EQUITY

def test_r11_allow_balanced():
    w = make_world()
    w.section_cut_minutes_today[[1, 2]] = 60  # Q2, Q3 cut; Q4 next keeps the gap at 60
    assert r11_equity.check(req("feeder_outage", section="TOR-S04", params=OUTAGE), make_ctx(w)).passed


def test_r11_deny_widening_gap():
    w = make_world()
    w.section_cut_minutes_today[1] = 30  # Q2 has 30; another 60 there -> gap 90 > 60
    res = r11_equity.check(req("feeder_outage", section="TOR-S02", params=OUTAGE), make_ctx(w))
    assert not res.passed and res.counts["gap_after"] == 90


# ---------------------------------------------------------------- R12 PRE_NOTIFY

def test_r12_allow_notified_vulnerable():
    w = make_world()
    w.notified_cool_space[list(VULNERABLE)] = True
    w.notified_sim_minute[list(VULNERABLE)] = 170  # 14:50, start 15:30 -> 40 min notice
    assert r12_prenotify.check(req("feeder_outage", section="TOR-S04", params=OUTAGE), make_ctx(w)).passed


def test_r12_allow_pending_notice_request():
    w = make_world()
    nm = np.full(w.n, -1)
    nm[list(VULNERABLE)] = 180  # notice requested at 15:00 for a 15:30 outage
    assert r12_prenotify.check(req("feeder_outage", section="TOR-S04", params=OUTAGE), make_ctx(w, notice_minute=nm)).passed


def test_r12_deny_unnotified_or_late():
    res = r12_prenotify.check(req("feeder_outage", section="TOR-S04", params=OUTAGE), make_ctx())
    assert not res.passed and res.counts["not_notified"] == 2
    w = make_world()
    w.notified_cool_space[list(VULNERABLE)] = True
    w.notified_sim_minute[list(VULNERABLE)] = 205  # 15:25 for a 15:30 start: only 5 min notice
    assert not r12_prenotify.check(req("feeder_outage", section="TOR-S04", params=OUTAGE), make_ctx(w)).passed


# ---------------------------------------------------------------- R13 HUMAN_APPROVAL

def test_r13_small_batch_no_approval():
    assert r13_approval.check(req("precool", hh=ALL, params=PRECOOL), make_ctx()).passed


def test_r13_needs_approval_for_outage_and_large_batch():
    res = r13_approval.check(req("feeder_outage", section="TOR-S02", params=OUTAGE), make_ctx())
    assert not res.passed and res.effect == "needs_approval"
    big = make_world(scale=20000.0)  # 20 homes x 20,000 = 400,000 real homes > 250,000
    assert not r13_approval.check(req("precool", hh=ALL, params=PRECOOL), make_ctx(big)).passed


# ---------------------------------------------------------------- R14 UNTRUSTED_EVIDENCE

def test_r14_trusted_and_low_impact_reply_pass():
    assert r14_untrusted.check(req("thermostat_setback", hh=[0], params=SETBACK), make_ctx()).passed
    reply = req("send_sms", messages=[sms(0)], evidence=[Evidence(id="msg_x0001", kind="message", trust="untrusted")])
    ctx = make_ctx()
    assert r14_untrusted.check(reply, ctx).passed and r14_untrusted.influenced(reply, ctx)


def test_r14_untrusted_needs_approval_even_if_mislabelled():
    ev = [Evidence(id="msg_x0002", kind="message", trust="system")]  # proposer claims "system"
    ctx = make_ctx(untrusted_evidence_ids=frozenset({"msg_x0002"}))
    res = r14_untrusted.check(req("thermostat_setback", hh=[0], params=SETBACK, evidence=ev), ctx)
    assert not res.passed and res.effect == "needs_approval"


# ---------------------------------------------------------------- R15 KILL_SWITCH

def test_r15_allow_when_not_halted_or_other_scope():
    r = req("precool", hh=ALL, params=PRECOOL)
    assert r15_kill_switch.check(r, make_ctx()).passed
    assert r15_kill_switch.check(r, make_ctx(halted=True, halted_scope="agent:negotiator")).passed


def test_r15_deny_when_halted_but_rollback_allowed():
    r = req("precool", hh=ALL, params=PRECOOL)
    assert not r15_kill_switch.check(r, make_ctx(halted=True, halted_scope="all")).passed
    undo = req("undo", hh=[0], undo_of="exe_test0001", window=None)
    assert r15_kill_switch.check(undo, make_ctx(halted=True)).passed


# ---------------------------------------------------------------- R16 RESTORE_SAFETY

def test_r16_allow_within_margin_or_staggered():
    assert r16_restore.check(req("feeder_restore", section="TOR-S02", params={"mw_added": 20}), make_ctx(margin_mw=50)).passed
    staggered = req("thermostat_restore", hh=ALL, params={"mw_added": 80, "stagger_group": 1, "stagger_groups": 4, "stagger_min": 10})
    assert r16_restore.check(staggered, make_ctx(margin_mw=50)).passed


def test_r16_needs_approval_over_margin():
    res = r16_restore.check(req("feeder_restore", section="TOR-S02", params={"mw_added": 80}), make_ctx(margin_mw=50))
    assert not res.passed and res.effect == "needs_approval"
    undo = req("undo", hh=[0], undo_of="exe_test0002", params={"restore_mw": 120.0}, window=None)
    assert not r16_restore.check(undo, make_ctx(margin_mw=50)).passed


# ---------------------------------------------------------------- gate: batches and precedence

def test_every_rule_reports_on_every_decision():
    d = gate.evaluate(req("precool", hh=ALL, params=PRECOOL), make_ctx())
    assert [r.rule_id for r in d.rule_results] == [m.RULE_ID for m in ALL_RULES]
    assert d.policy_version == POLICY_VERSION and d.decided_by == "system:gate"


def test_allow():
    d = gate.evaluate(req("precool", hh=[0, 2, 4], params=PRECOOL), make_ctx())
    assert d.verdict == "allow" and d.allowed_count == 3 and d.denied_count == 0
    assert d.blast_radius.homes_real == 300  # 3 sim homes x scale 100


def test_partial_allow_counts():
    d = gate.evaluate(req("thermostat_setback", hh=ALL, params=SETBACK, expected_mw=20.0), make_ctx())
    # 2 without consent (R02) are removed first; vulnerable 3 and 7 are consented -> R04
    assert d.verdict == "partial_allow"
    assert d.denied_reasons == {"R02": 2, "R04": 2}
    assert d.denied_count == 4 and d.allowed_count == 16
    assert sum(d.denied_reasons.values()) == d.denied_count
    assert len(d.allowed_household_ids) == 16 and "hh_00004" not in d.allowed_household_ids
    assert d.blast_radius.homes_real == 1600 and d.blast_radius.vulnerable_real == 0
    assert d.blast_radius.mw == pytest.approx(16.0)
    featured = {f["resident_id"]: f for f in d.featured_detail}
    assert featured["res_01"]["allowed"] is False and featured["res_01"]["denied_by"] == ["R04"]
    assert featured["res_02"]["allowed"] is True


def test_full_deny_beats_needs_approval():
    """The injection beat: +4 °C on everything, citing an untrusted message -> DENY (R03), not approval."""
    ev = [Evidence(id="msg_inj0001", kind="message", trust="untrusted")]
    d = gate.evaluate(req("thermostat_setback", hh=ALL, params={"offset_c": 4.0}, evidence=ev), make_ctx())
    assert d.verdict == "deny" and d.allowed_count == 0
    assert d.untrusted_influence is True
    assert d.denied_reasons["R03"] == N - len(NO_CONSENT)


def test_single_target_deny_lists_every_failing_rule():
    """Cut TOR-S03 again while others are uncut: R10 denies; R12 also fails (no notice)."""
    w = make_world()
    w.section_cut_count_today[2] = 1
    w.section_cut_minutes_today[2] = 60
    d = gate.evaluate(req("feeder_outage", section="TOR-S03", params=OUTAGE), make_ctx(w))
    assert d.verdict == "deny"
    assert {"R09", "R10"} <= set(d.denied_reasons)


def test_outage_needs_approval_when_all_deny_rules_pass():
    w = make_world()
    w.notified_cool_space[list(VULNERABLE)] = True
    w.notified_sim_minute[list(VULNERABLE)] = 170
    d = gate.evaluate(req("feeder_outage", section="TOR-S04", params=OUTAGE, expected_mw=50.0), make_ctx(w))
    assert d.verdict == "needs_approval", [(r.rule_id, r.detail) for r in d.rule_results if not r.passed]
    assert d.blast_radius.homes_real == 500 and d.blast_radius.vulnerable_real == 200


def test_partial_batch_with_untrusted_evidence_needs_approval():
    ev = [Evidence(id="msg_x0009", kind="message", trust="untrusted")]
    d = gate.evaluate(req("thermostat_setback", hh=ALL, params=SETBACK, evidence=ev), make_ctx())
    assert d.verdict == "needs_approval" and d.denied_count == 4 and len(d.allowed_household_ids) == 16


def test_kill_switch_denies_whole_batch():
    d = gate.evaluate(req("precool", hh=ALL, params=PRECOOL), make_ctx(halted=True))
    assert d.verdict == "deny" and d.denied_count == N
    assert d.denied_reasons == {"R15": N}  # evaluated first: the halt is the reason for every target


def test_empty_batch_is_denied():
    d = gate.evaluate(req("precool", hh=[], params=PRECOOL), make_ctx())
    assert d.verdict == "deny" and any(r.rule_id == "R00" for r in d.rule_results)


def test_thresholds_come_from_policy_params():
    pols = {p.id: p for p in POLICIES}
    pols["R03"] = pols["R03"].model_copy(update={"params": {**pols["R03"].params, "offset_max_c": 5.0}})
    r = req("thermostat_setback", hh=[0], params={"offset_c": 4.0})
    assert not r03_program_limits.check(r, make_ctx()).passed
    assert r03_program_limits.check(r, make_ctx(policies=pols)).passed


def test_window_from_iso_params():
    start = START.replace(hour=16) + timedelta(minutes=0)
    r = req("feeder_outage", section="TOR-S04", params={"duration_min": 60, "start": sim_iso(start), "end": sim_iso(start + timedelta(minutes=60))}, window=None)
    w = make_world()
    w.notified_cool_space[list(VULNERABLE)] = True
    w.notified_sim_minute[list(VULNERABLE)] = 230  # 15:50 for a 16:00 start: 10 min < 15
    assert not r12_prenotify.check(r, make_ctx(w)).passed
    w.notified_sim_minute[list(VULNERABLE)] = 225  # 15:45 -> exactly 15 min
    assert r12_prenotify.check(r, make_ctx(w)).passed
