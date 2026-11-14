"""R14 UNTRUSTED_EVIDENCE (needs_approval): if the evidence includes untrusted input (labelled
trust "untrusted" by the proposer, or resolved as untrusted from the messages collection by
build_gate_context) and the action is not a low-impact reply, it needs supervisor approval.
Any untrusted evidence sets Decision.untrusted_influence (see `influenced`)."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import approval_result, full_mask, gate_applies, params
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R14", "UNTRUSTED_EVIDENCE", "needs_approval"


def untrusted_ids(request: ActionRequest, ctx) -> list[str]:
    """Evidence ids that are untrusted, whatever the proposer claimed."""
    resolved = getattr(ctx, "untrusted_evidence_ids", frozenset()) or frozenset()
    return [e.id for e in request.evidence if e.trust == "untrusted" or e.id in resolved]


def influenced(request: ActionRequest, ctx) -> bool:
    return bool(untrusted_ids(request, ctx))


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    low_impact = set(params(ctx.policies, RULE_ID).get("low_impact_actions", []))
    bad = untrusted_ids(request, ctx)
    needs = bool(bad) and request.action_type not in low_impact
    if bad and not needs:
        ok_detail = f"cites untrusted input ({', '.join(bad[:3])}) but {request.action_type} is a low-impact reply"
    else:
        ok_detail = "all evidence is trusted"
    return approval_result(
        RULE_ID,
        NAME,
        ctx,
        mask,
        needs,
        ok_detail,
        f"influenced by untrusted input ({', '.join(bad[:3])}{'…' if len(bad) > 3 else ''}); supervisor must review",
        {"untrusted_evidence": len(bad)},
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
