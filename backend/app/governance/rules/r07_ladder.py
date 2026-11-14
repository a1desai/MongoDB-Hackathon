"""R07 LADDER: a feeder_outage is denied unless
projected shortfall - (active delivered + remaining lower-rung potential) > 0."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import full_mask, gate_applies, not_applicable, request_level
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R07", "LADDER", "deny"


def residual_mw(ctx) -> float:
    return float(ctx.projected_shortfall_mw - (ctx.active_delivered_mw + ctx.lower_rung_potential_mw))


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    if request.action_type != "feeder_outage":
        return not_applicable(RULE_ID, NAME, mask)
    r = residual_mw(ctx)
    calc = (
        f"projected {ctx.projected_shortfall_mw:,.0f} MW − (delivered {ctx.active_delivered_mw:,.0f} MW"
        f" + lower rungs {ctx.lower_rung_potential_mw:,.0f} MW) = {r:,.0f} MW"
    )
    return request_level(
        RULE_ID,
        NAME,
        ctx,
        mask,
        r > 0,
        f"lower rungs exhausted: {calc}",
        f"lower rungs still cover the gap: {calc}",
        {"residual_mw": round(r)},
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
