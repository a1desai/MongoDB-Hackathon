"""R01 SCOPE: the proposer holds propose scope for the action type; the acting-for human is
active and on shift; only agent:executor executes (see `check_execute`)."""

from __future__ import annotations

from datetime import datetime

import numpy as np

from app.governance.rules.base import full_mask, gate_applies, request_level
from app.models import ActionRequest, Identity, RuleResult
from app.timeutil import parse_sim

RULE_ID, NAME, EFFECT = "R01", "SCOPE", "deny"
SUPERVISOR = "human:supervisor"


def _expired(ident: Identity, now: datetime) -> bool:
    if not ident.expires_at:
        return False
    try:
        return now > parse_sim(ident.expires_at)
    except Exception:
        return False


def problems(request: ActionRequest, identities: dict[str, Identity], now: datetime) -> list[str]:
    """Reasons the proposer / acting-for pair is not authorised (empty list = authorised)."""
    out: list[str] = []
    agent = identities.get(request.proposed_by)
    if agent is None:
        out.append(f"unknown identity {request.proposed_by}")
    else:
        if agent.status != "active":
            out.append(f"{agent.id} is {agent.status}")
        if _expired(agent, now):
            out.append(f"{agent.id} credentials expired")
        is_rollback = request.action_type == "undo" and request.acting_for == SUPERVISOR
        if request.action_type not in agent.scopes.propose and not is_rollback:
            out.append(f"{agent.id} lacks propose scope for {request.action_type}")
        if request.action_type != "undo" and agent.acting_for and agent.acting_for != request.acting_for:
            out.append(f"{agent.id} acts for {agent.acting_for}, not {request.acting_for}")
    human = identities.get(request.acting_for)
    if human is None or human.kind != "human":
        out.append(f"acting-for {request.acting_for} is not a known human")
    else:
        if human.status != "active":
            out.append(f"{human.id} is {human.status}")
        if not human.on_shift:
            out.append(f"{human.id} is not on shift")
    return out


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    issues = problems(request, ctx.identities, ctx.sim_now)
    ok = not issues
    return request_level(
        RULE_ID,
        NAME,
        ctx,
        mask,
        ok,
        f"{request.proposed_by} may propose {request.action_type} for {request.acting_for} (on shift)",
        "; ".join(issues),
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]


def check_execute(actor: str, request: ActionRequest, identities: dict[str, Identity]) -> RuleResult:
    """Executor re-check: only an active identity with execute scope for this action may execute."""
    ident = identities.get(actor)
    ok = (
        ident is not None
        and ident.status == "active"
        and (request.action_type in ident.scopes.execute or "*" in ident.scopes.execute)
    )
    return RuleResult(
        rule_id=RULE_ID,
        name=NAME,
        applies=True,
        passed=ok,
        effect="none" if ok else "deny",
        detail=f"{actor} {'may' if ok else 'may not'} execute {request.action_type}",
    )
