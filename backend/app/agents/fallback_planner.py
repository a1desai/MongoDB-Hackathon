"""Deterministic Planner fallback (PLAN §8.2): fill rungs in ladder order until the expected
MW covers 1.15 x the forecast shortfall. Pure function of the planner context, no I/O.

Context keys read (built by agents/planner.py: build_context):
  now "HH:MM"; forecast {id, horizon_end, windows[{start, end, peak_short_mw}]};
  resources {thermostats{precool_mw, setback_mw_per_c}, batteries{mw}, ev{pausable_mw},
             businesses[{id, name, status, max_mw, accepted_mw, notice_min, protected}],
             sms{mw_potential, amount_cad}, sections[{id, mw, eligible, cut_count_today, cut_minutes_today}]};
  similar_past_events[{id}].
"""

from __future__ import annotations

from typing import Any

from ..models import RUNGS, PlannerOutput, PlanStep, Selector, StepParams, Window
from .common import hhmm_of, minutes

COVERAGE = 1.15
PRECOOL_OFFSET_C = -2.0
PRECOOL_LEAD_MIN = 120  # R03 precool_max_min
SETBACK_OFFSET_C = 2.0  # R03 offset_max_c
SETBACK_MAX_MIN = 240  # R03 setback_max_min
OPEN_ENDED_MIN = 180  # window that runs into the forecast horizon: plan this long
OUTAGE_MIN = 60  # R09 max_minutes_per_section_per_day
NOTICE_MIN = 15  # R12 min_notice_min
SMS_AMOUNT_CAD = 5.0  # R06 max_incentive_cad is 10
BUSINESS_ASK_FACTOR = 0.7  # expected share of max_mw from a curtailment ask not yet accepted
MIN_STEP_MW = 0.5

LADDER: list[str] = sorted(RUNGS, key=RUNGS.__getitem__)


def main_window(ctx: dict[str, Any]) -> dict[str, Any] | None:
    """The forecast window with the largest peak shortfall (None if no shortfall)."""
    windows = [w for w in (ctx.get("forecast") or {}).get("windows", []) if w.get("peak_short_mw", 0) > 0]
    if not windows:
        return None
    return max(windows, key=lambda w: w["peak_short_mw"])


EQUITY_GAP_MIN = 60  # R11 max_quintile_gap_min
OUTAGE_RESERVE_SHARE = 0.02  # outages need not cover the last 2% of supply (the trip threshold is 3%)


class _QuintileLedger:
    """Average outage minutes per home by income quintile, as R11 counts them (each section's homes
    under its dominant quintile, over quintiles with an unprotected section). Minutes are those cut
    today plus a full outage for every section with one in flight."""

    def __init__(self, sections: list[dict[str, Any]]) -> None:
        quintiles = {s.get("income_quintile") for s in sections
                     if not s.get("protected") and s.get("income_quintile") is not None}
        self.num = {q: 0.0 for q in quintiles}
        self.den = {q: 0.0 for q in quintiles}
        for s in sections:
            q = s.get("income_quintile")
            if q in self.num:
                h = max(float(s.get("homes_real") or 1.0), 1.0)
                m = int(s.get("cut_minutes_today", 0)) + (OUTAGE_MIN if s.get("outage_in_flight") else 0)
                self.num[q] += m * h
                self.den[q] += h

    def avg(self, extra: tuple[Any, float] | None = None) -> dict[Any, float]:
        out = {}
        for q in self.num:
            add = extra[1] if extra and extra[0] == q else 0.0
            out[q] = (self.num[q] + add) / self.den[q] if self.den[q] else 0.0
        return out

    def gap_ok(self, sec: dict[str, Any], minutes: int) -> bool:
        q = sec.get("income_quintile")
        if q not in self.num:
            return True
        b = self.avg()
        a = self.avg((q, minutes * max(float(sec.get("homes_real") or 1.0), 1.0)))
        g0, g1 = max(b.values()) - min(b.values()), max(a.values()) - min(a.values())
        return g1 <= EQUITY_GAP_MIN or g1 <= g0

    def add(self, sec: dict[str, Any], minutes: int) -> None:
        q = sec.get("income_quintile")
        if q in self.num:
            self.num[q] += minutes * max(float(sec.get("homes_real") or 1.0), 1.0)


def _ceil5(m: int) -> int:
    return -(-int(m) // 5) * 5


def _win(start_m: int, end_m: int) -> Window:
    return Window(start=hhmm_of(start_m), end=hhmm_of(max(end_m, start_m + 5)))


def select_outage_steps(res: dict[str, Any], need_mw: float, now_m: int, w_start: int,
                        evidence: list[str], *, step_offset: int = 0) -> tuple[list[PlanStep], float]:
    """Least-harm, rotation- and equity-aware feeder_outage section picker (PLAN §8.2's rung 7
    and fallback_plan's own last resort). Pulled out as its own function so planner.py can call
    it as a deterministic top-up when an LLM plan proposes less than the 1.15x coverage target
    without ever reaching the outage rung: the LLM proposes, this code still decides whether an
    outage is warranted and, if so, exactly which section. Returns (steps, mw_covered); steps
    is empty if `need_mw` is non-positive or no eligible section remains.
    """
    steps: list[PlanStep] = []
    covered = 0.0
    if need_mw <= 0:
        return steps, covered
    o_start = max(w_start, _ceil5(now_m + NOTICE_MIN))
    eligible = [s for s in res.get("sections", []) if s.get("eligible") and float(s.get("mw", 0)) > 0]

    def tier(s: dict[str, Any]) -> tuple[int, int]:
        unsafe = 0 if s.get("safe_duration_min") or not s.get("vulnerable_over_t_safe_if_cut") else 1
        return int(s.get("cut_count_today", 0)), unsafe

    equity = _QuintileLedger(res.get("sections", []))

    def dur_of(s: dict[str, Any]) -> int:
        return int(s.get("safe_duration_min") or OUTAGE_MIN)

    while covered < need_mw and eligible:
        pool = [s for s in eligible if equity.gap_ok(s, dur_of(s))] or list(eligible)
        best = min(tier(s) for s in pool)
        pool = [s for s in pool if tier(s) == best]
        remaining = need_mw - covered
        covers = [s for s in pool if float(s["mw"]) >= remaining]
        sec = min(covers, key=lambda s: float(s["mw"])) if covers else max(pool, key=lambda s: float(s["mw"]))
        eligible.remove(sec)
        dur = dur_of(sec)
        equity.add(sec, dur)
        mw = float(sec["mw"])
        steps.append(PlanStep(
            step_id=f"s{step_offset + len(steps) + 1}", rung=RUNGS["feeder_outage"], action_type="feeder_outage",
            selector=Selector(sections=[sec["id"]]),
            params=StepParams(mw_target=round(mw, 1), duration_min=dur),
            window=_win(o_start, o_start + dur), expected_mw=round(mw, 1), evidence_ids=list(evidence),
        ))
        covered += mw
    return steps, covered


def fallback_plan(ctx: dict[str, Any]) -> PlannerOutput:
    """Rungs in order until expected MW >= 1.15 x shortfall; feeder_outage only after every
    lower rung is fully used."""
    window = main_window(ctx)
    shortfall = float(window["peak_short_mw"]) if window else 0.0
    if window is None or shortfall <= 0:
        return PlannerOutput(
            rationale="Fallback planner: no shortfall in the forecast, so every rung stays in reserve.",
            expected_shortfall_mw=0,
            steps=[],
        )

    res = ctx.get("resources") or {}
    now_m = minutes(ctx.get("now", "12:00"))
    w_start = max(minutes(window["start"]), now_m)
    w_end = minutes(window["end"])
    horizon_end = (ctx.get("forecast") or {}).get("horizon_end")
    if horizon_end and w_end >= minutes(horizon_end) - 5:
        w_end = max(w_end, w_start + OPEN_ENDED_MIN)  # the shortfall runs past what we can see
    w_end = max(w_end, w_start + 15)

    evidence = [i for i in [(ctx.get("forecast") or {}).get("id")] if i]
    evidence += [e["id"] for e in ctx.get("similar_past_events", [])[:2] if e.get("id")]

    target = COVERAGE * shortfall
    steps: list[PlanStep] = []
    covered = 0.0
    used: list[str] = []

    def need() -> float:
        return target - covered

    def add(action: str, potential: float, window_: Window, *, selector: Selector | None = None,
            offset_c: float | None = None, amount_cad: float | None = None,
            duration_min: int | None = None, exact: bool = False) -> None:
        nonlocal covered
        if potential < MIN_STEP_MW or need() <= 0:
            return
        mw = potential if exact else min(potential, need())
        steps.append(PlanStep(
            step_id=f"s{len(steps) + 1}",
            rung=RUNGS[action],
            action_type=action,  # type: ignore[arg-type]
            selector=selector or Selector(),
            params=StepParams(offset_c=offset_c, amount_cad=amount_cad, mw_target=round(mw, 1),
                              duration_min=duration_min),
            window=window_,
            expected_mw=round(mw, 1),
            evidence_ids=list(evidence),
        ))
        covered += mw
        used.append(f"{action} {mw:.0f} MW")

    th = res.get("thermostats") or {}
    # 1. precool: needs lead time before the window starts
    if w_start - now_m >= 15:
        pre_start = max(now_m, w_start - PRECOOL_LEAD_MIN)
        add("precool", float(th.get("precool_mw", 0)), _win(pre_start, w_start),
            offset_c=PRECOOL_OFFSET_C, duration_min=w_start - pre_start)

    # 2. batteries, 3. EV pause: during the window
    add("battery_discharge", float((res.get("batteries") or {}).get("mw", 0)), _win(w_start, w_end),
        duration_min=w_end - w_start)
    add("ev_pause", float((res.get("ev") or {}).get("pausable_mw", 0)), _win(w_start, w_end),
        duration_min=w_end - w_start)

    # 4. businesses: one step each, honouring the notice period
    for biz in res.get("businesses", []):
        if biz.get("protected") or biz.get("status") in ("declined", "active", "asked", "dispatched", "completed"):
            continue
        accepted = float(biz.get("accepted_mw") or 0)
        potential = accepted if biz.get("status") == "accepted" and accepted > 0 \
            else BUSINESS_ASK_FACTOR * float(biz.get("max_mw") or 0)
        b_start = max(w_start, now_m + int(biz.get("notice_min") or 0))
        if b_start >= w_end:
            continue
        add("business_curtail_request", potential, _win(b_start, w_end),
            selector=Selector(business_ids=[biz["id"]]), duration_min=w_end - b_start)

    # 5. thermostat setback, capped at R03 limits
    sb_end = min(w_end, w_start + SETBACK_MAX_MIN)
    add("thermostat_setback", float(th.get("setback_mw_per_c", 0)) * SETBACK_OFFSET_C,
        _win(w_start, sb_end), offset_c=SETBACK_OFFSET_C, duration_min=sb_end - w_start)

    # 6. SMS incentive
    sms = res.get("sms") or {}
    add("send_sms", float(sms.get("mw_potential", 0)), _win(w_start, w_end),
        amount_cad=float(sms.get("amount_cad") or SMS_AMOUNT_CAD), duration_min=w_end - w_start)

    # 7. feeder_outage: last resort, only reached once every lower rung is spent
    committed = float(res.get("outages_committed_mw") or 0.0)  # outages proposed/approved, not switched yet
    if need() > 0 and committed >= MIN_STEP_MW:
        covered += committed
        used.append(f"outages already committed {committed:.0f} MW")
    # Outages are the last resort: leave a small residual (operating reserve, well under the 3%-of-supply
    # trip threshold) uncovered rather than darkening a whole extra section for it.
    supply = float((ctx.get("grid_now") or {}).get("supply_mw") or 0.0)
    reserve = OUTAGE_RESERVE_SHARE * supply
    if 0 < need() <= reserve:
        used.append(f"{need():.0f} MW left to operating reserve")
        covered += need()
    elif need() > reserve:
        covered += reserve
    if need() > 0:
        # select_outage_steps starts the outage at max(w_start, now + R12's notice period).
        outage_steps, outage_mw = select_outage_steps(res, need(), now_m, w_start, evidence,
                                                        step_offset=len(steps))
        if outage_steps:
            steps.extend(outage_steps)
            covered += outage_mw
            used.append(f"feeder_outage {outage_mw:.0f} MW ({len(outage_steps)} section(s))")

    gap = max(0.0, need())
    rationale = (
        f"Fallback planner (deterministic): forecast shortfall {shortfall:.0f} MW "
        f"{window['start']}-{window['end']}; target {target:.0f} MW (1.15x). "
        f"Rungs in ladder order: {', '.join(used) or 'none available'}."
    )
    if gap > 0:
        rationale += f" Still {gap:.0f} MW short after all rungs."
    elif not any(s.action_type == "feeder_outage" for s in steps):
        rationale += " No outage needed."
    return PlannerOutput(rationale=rationale[:600], expected_shortfall_mw=round(shortfall, 1), steps=steps)
