"""R12 PRE_NOTIFY: every reachable vulnerable home in the section (TextLine opt-in, not STOPped)
was notified with the nearest open designated cool space at least min_notice_min sim-minutes
before the outage starts.

A home counts as notified when TextLine delivered the notice (world.notified_sim_minute), or when a
cool-space notice for it was already requested and not denied (ctx.notice_minute, gathered by
build_gate_context): the Targeter proposes the notice together with the outage, and the outage
cannot start before its approval anyway.
"""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import full_mask, gate_applies, not_applicable, params, request_level, targets_of, window_bounds
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R12", "PRE_NOTIFY", "deny"


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    if request.action_type != "feeder_outage":
        return not_applicable(RULE_ID, NAME, mask)
    t = targets_of(request, ctx)
    if t.section_idx is None:
        return not_applicable(RULE_ID, NAME, mask, "unknown section")
    w = ctx.world
    need = int(params(ctx.policies, RULE_ID)["min_notice_min"])
    start, _ = window_bounds(request, ctx.sim_now)
    start_min = int((start - w.sim_start).total_seconds() // 60)
    deadline = start_min - need

    in_sec = (w.section_idx == t.section_idx) & w.vulnerable
    reachable = in_sec & w.sms_opt_in & ~w.sms_stopped
    sent_at = np.asarray(w.notified_sim_minute)
    delivered = w.notified_cool_space & (sent_at >= 0) & (sent_at <= deadline)
    pending = np.zeros(w.n, dtype=bool)
    nm = getattr(ctx, "notice_minute", None)
    if nm is not None and len(nm) == w.n:
        nm = np.asarray(nm)
        pending = (nm >= 0) & (nm <= deadline)
    ok_home = delivered | pending
    missing = int((reachable & ~ok_home).sum())
    total = int(reachable.sum())
    unreachable = int((in_sec & ~reachable).sum())
    return request_level(
        RULE_ID,
        NAME,
        ctx,
        mask,
        missing == 0,
        f"{total} vulnerable homes in {t.ids[0]} notified >= {need} min before the outage with nearest cool space",
        f"{missing} of {total} vulnerable homes in {t.ids[0]} not notified >= {need} min before the outage",
        {"vulnerable_reachable": total, "not_notified": missing, "pending_notice": int((reachable & pending & ~delivered).sum()), "unreachable": unreachable},
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
