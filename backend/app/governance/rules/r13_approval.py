"""R13 HUMAN_APPROVAL (needs_approval): every action in always_actions (feeder_outage), and any
action whose allowed targets affect more than max_homes_real_without_approval real homes."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import affected_home_idx, approval_result, fmt_n, full_mask, gate_applies, params
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R13", "HUMAN_APPROVAL", "needs_approval"


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    p = params(ctx.policies, RULE_ID)
    always = set(p.get("always_actions", []))
    cap = int(p["max_homes_real_without_approval"])
    homes = affected_home_idx(request, ctx, mask)
    homes_real = int(round(homes.size * ctx.scale))
    reasons: list[str] = []
    if request.action_type in always:
        reasons.append(f"{request.action_type} always needs supervisor approval")
    if homes_real > cap:
        reasons.append(f"affects {fmt_n(homes_real)} real homes > {fmt_n(cap)}")
    return approval_result(
        RULE_ID,
        NAME,
        ctx,
        mask,
        bool(reasons),
        f"affects {fmt_n(homes_real)} real homes (<= {fmt_n(cap)}); no approval needed",
        "; ".join(reasons),
        {"homes_real": homes_real},
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
