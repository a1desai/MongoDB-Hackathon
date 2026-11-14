"""R04 VULNERABLE_EXEMPT (per target): no setback, EV pause or reduce-cooling incentive for
vulnerable homes. Cool-space and timing notifications are allowed."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import deny_result, full_mask, gate_applies, not_applicable, params, targets_of
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R04", "VULNERABLE_EXEMPT", "deny"


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    p = params(ctx.policies, RULE_ID)
    t = targets_of(request, ctx)
    known = t.hh_idx >= 0
    i = np.where(known, t.hh_idx, 0)
    vulnerable = known & ctx.world.vulnerable[i]
    a = request.action_type
    if a in p.get("exempt_actions", []):
        denied = vulnerable
        what = a
    elif t.kind == "messages":
        exempt_types = set(p.get("exempt_sms_offer_types", []))
        is_exempt_offer = np.array([bool(o) and (o.get("type") in exempt_types) for o in t.offers], dtype=bool)
        denied = vulnerable & is_exempt_offer
        what = "reduce-cooling offer"
    else:
        return not_applicable(RULE_ID, NAME, mask)
    k = int((denied & mask).sum())
    return deny_result(
        RULE_ID,
        NAME,
        ctx,
        mask,
        denied,
        f"no vulnerable homes receive {what}",
        f"{k:,} vulnerable homes exempt from {what}",
        {"vulnerable": k},
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
