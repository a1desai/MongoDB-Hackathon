"""R09 OUTAGE_DURATION: minutes already cut today + minutes still scheduled + this outage must be
<= max_minutes_per_section_per_day. A missing duration is treated as the daily maximum."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import duration_min, full_mask, gate_applies, not_applicable, params, request_level, targets_of
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R09", "OUTAGE_DURATION", "deny"


def committed_minutes(ctx, s_idx: int) -> int:
    """Minutes cut today plus minutes remaining on a cut that is still in progress."""
    w = ctx.world
    used = int(w.section_cut_minutes_today[s_idx])
    until = w.section_cut_until[s_idx] if s_idx < len(w.section_cut_until) else None
    if bool(w.section_cut[s_idx]) and until is not None and until > ctx.sim_now:
        used += int((until - ctx.sim_now).total_seconds() // 60)
    return used


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    if request.action_type != "feeder_outage":
        return not_applicable(RULE_ID, NAME, mask)
    t = targets_of(request, ctx)
    if t.section_idx is None:
        return not_applicable(RULE_ID, NAME, mask, "unknown section")
    cap = int(params(ctx.policies, RULE_ID)["max_minutes_per_section_per_day"])
    dur = duration_min(request, ctx.sim_now)
    dur = cap if dur is None else dur
    used = committed_minutes(ctx, t.section_idx)
    total = used + dur
    return request_level(
        RULE_ID,
        NAME,
        ctx,
        mask,
        dur > 0 and total <= cap,
        f"{t.ids[0]}: {used} + {dur} = {total} min <= {cap} min/day",
        f"{t.ids[0]}: {used} + {dur} = {total} min > {cap} min/day" if dur > 0 else "outage duration must be positive",
        {"minutes_after": total},
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
