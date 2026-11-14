"""R11 EQUITY: after this outage, the average outage minutes per home by income quintile (each
section's homes counted under the section's dominant quintile, over quintiles with an eligible
section) must keep a max−min gap <= max_quintile_gap_min, as the policy text says. An outage that
does not widen an already-wider gap is allowed, so an uncontrolled trip cannot lock out every
later rotation.

(Integration fix: this used to sum raw section-minutes per quintile. With 60-minute outages and a
60-minute cap that allowed one outage per quintile per day, contradicted the policy text and the
ComparisonCard metric, and left no legal outage once one quintile's sections were all R05-unsafe.)"""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import duration_min, full_mask, gate_applies, not_applicable, params, request_level, targets_of
from app.governance.rules.r09_outage_duration import committed_minutes
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R11", "EQUITY", "deny"


def _section_homes(w) -> np.ndarray:
    homes = np.asarray(getattr(w, "section_households", []), dtype=float)
    if homes.size != len(w.section_ids):
        homes = np.bincount(np.asarray(w.section_idx, dtype=int), minlength=len(w.section_ids)).astype(float)
    return np.maximum(homes, 1.0)


def _weighted(ctx, extra: tuple[int, int] | None = None) -> dict[int, float]:
    """{quintile: mean committed outage minutes per home}; `extra` = (section_idx, minutes) to add."""
    w = ctx.world
    eligible = ~np.asarray(w.section_protected, dtype=bool)
    quintiles = {int(w.section_quintile[s]) for s in range(len(w.section_ids)) if eligible[s]}
    homes = _section_homes(w)
    num = {q: 0.0 for q in quintiles}
    den = {q: 0.0 for q in quintiles}
    for s in range(len(w.section_ids)):
        q = int(w.section_quintile[s])
        if q in num:
            minutes = committed_minutes(ctx, s) + (extra[1] if extra and extra[0] == s else 0)
            num[q] += minutes * homes[s]
            den[q] += homes[s]
    return {q: round(num[q] / den[q], 1) if den[q] else 0.0 for q in quintiles}


def quintile_minutes(ctx) -> dict[int, float]:
    """{quintile: mean committed outage minutes per home} over quintiles with an eligible section."""
    return _weighted(ctx)


def _gap(m: dict[int, float]) -> float:
    return round(max(m.values()) - min(m.values()), 1) if m else 0.0


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    if request.action_type != "feeder_outage":
        return not_applicable(RULE_ID, NAME, mask)
    t = targets_of(request, ctx)
    if t.section_idx is None:
        return not_applicable(RULE_ID, NAME, mask, "unknown section")
    cap = int(params(ctx.policies, RULE_ID)["max_quintile_gap_min"])
    dur = duration_min(request, ctx.sim_now)
    if dur is None:
        dur = int(params(ctx.policies, "R09").get("max_minutes_per_section_per_day", 60))
    before = quintile_minutes(ctx)
    q = int(ctx.world.section_quintile[t.section_idx])
    after = _weighted(ctx, (t.section_idx, dur))
    g0, g1 = _gap(before), _gap(after)
    ok = g1 <= cap or g1 <= g0
    by_q = ", ".join(f"Q{k}:{v:g}" for k, v in sorted(after.items()))
    return request_level(
        RULE_ID,
        NAME,
        ctx,
        mask,
        ok,
        f"quintile gap after outage {g1:g} min/home (cap {cap}); {by_q}",
        f"outage in Q{q} widens quintile gap to {g1:g} min/home > {cap} min; {by_q}",
        {"gap_before": g0, "gap_after": g1, "quintile": q},
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
