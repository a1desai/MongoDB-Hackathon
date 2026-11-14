"""R10 ROTATION: no section is cut a second time until every eligible (unprotected) section has
been cut once today."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import full_mask, gate_applies, not_applicable, request_level, targets_of
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R10", "ROTATION", "deny"


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    if request.action_type != "feeder_outage":
        return not_applicable(RULE_ID, NAME, mask)
    t = targets_of(request, ctx)
    if t.section_idx is None:
        return not_applicable(RULE_ID, NAME, mask, "unknown section")
    w = ctx.world
    # A section that is cut right now counts as cut once even if the counter has not ticked yet.
    counts = np.maximum(np.asarray(w.section_cut_count_today, dtype=int), np.asarray(w.section_cut, dtype=int))
    eligible = ~np.asarray(w.section_protected, dtype=bool)
    uncut = [w.section_ids[i] for i in np.flatnonzero(eligible & (counts == 0))]
    times = int(counts[t.section_idx])
    ok = times == 0 or not uncut
    return request_level(
        RULE_ID,
        NAME,
        ctx,
        mask,
        ok,
        f"{t.ids[0]} cut {times}x today; rotation respected",
        f"{t.ids[0]} already cut {times}x today while {len(uncut)} eligible sections are uncut ({', '.join(uncut[:4])}{'…' if len(uncut) > 4 else ''})",
        {"times_cut": times, "eligible_uncut": len(uncut)},
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
