"""R08 PROTECTED_SECTION: no outage on a protected section (critical facility, pumping station
or designated cool space). Unknown sections are denied too."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import full_mask, gate_applies, not_applicable, request_level, targets_of
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R08", "PROTECTED_SECTION", "deny"


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    if request.action_type != "feeder_outage":
        return not_applicable(RULE_ID, NAME, mask)
    t = targets_of(request, ctx)
    sid = t.ids[0] or "?"
    if t.section_idx is None:
        return request_level(RULE_ID, NAME, ctx, mask, False, "", f"unknown section {sid}")
    protected = bool(ctx.world.section_protected[t.section_idx])
    return request_level(RULE_ID, NAME, ctx, mask, not protected, f"{sid} is not protected", f"{sid} is a protected section")


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
