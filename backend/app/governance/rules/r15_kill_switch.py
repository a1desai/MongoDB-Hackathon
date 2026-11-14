"""R15 KILL_SWITCH: nothing is approved for execution while agent_control is halted for "all" or
for the proposing agent. A supervisor's rollback (action "undo") stays possible during a halt."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import full_mask, gate_applies, request_level
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R15", "KILL_SWITCH", "deny"


def halted_for(ctx, agent_id: str) -> bool:
    return bool(ctx.halted) and (ctx.halted_scope or "all") in ("all", agent_id)


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    stopped = halted_for(ctx, request.proposed_by)
    if stopped and request.action_type == "undo":
        return request_level(RULE_ID, NAME, ctx, mask, True, f"agents halted ({ctx.halted_scope}); rollback still permitted", "")
    return request_level(
        RULE_ID,
        NAME,
        ctx,
        mask,
        not stopped,
        "kill switch not engaged" if not ctx.halted else f"halt scope {ctx.halted_scope} does not cover {request.proposed_by}",
        f"kill switch engaged (scope {ctx.halted_scope}); {request.proposed_by} is halted",
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
