"""R03 PROGRAM_LIMITS: thermostat offsets within [offset_min_c, offset_max_c] (pre-cool <= 0,
setback >= 0), pre-cool <= precool_max_min, setback <= setback_max_min, and at most
setbacks_per_home_per_day setbacks per home (per target)."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import deny_result, duration_min, full_mask, gate_applies, not_applicable, params, targets_of
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R03", "PROGRAM_LIMITS", "deny"


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    a = request.action_type
    if a not in ("precool", "thermostat_setback"):
        return not_applicable(RULE_ID, NAME, mask)
    p = params(ctx.policies, RULE_ID)
    lo, hi = float(p["offset_min_c"]), float(p["offset_max_c"])
    issues: list[str] = []

    offsets = {k: request.params.get(k) for k in ("offset_c", "precool_offset_c") if request.params.get(k) is not None}
    if not offsets:
        issues.append("no offset_c given")
    for key, raw in offsets.items():
        try:
            v = float(raw)
        except (TypeError, ValueError):
            issues.append(f"{key}={raw!r} is not a number")
            continue
        if not lo <= v <= hi:
            issues.append(f"{key} {v:+.1f} °C outside program limits [{lo:+.1f}, {hi:+.1f}] °C")
        elif a == "precool" and v > 0:
            issues.append(f"pre-cool offset {v:+.1f} °C must be <= 0")
        elif a == "thermostat_setback" and key == "offset_c" and v < 0:
            issues.append(f"setback offset {v:+.1f} °C must be >= 0")

    dur = duration_min(request, ctx.sim_now)
    cap = int(p["precool_max_min"] if a == "precool" else p["setback_max_min"])
    if dur is not None and dur > cap:
        issues.append(f"{a} lasts {dur} min > {cap} min limit")

    if issues:
        return deny_result(RULE_ID, NAME, ctx, mask, mask.copy(), "", "; ".join(issues))

    if a == "thermostat_setback":
        t = targets_of(request, ctx)
        per_day = int(p["setbacks_per_home_per_day"])
        i = np.where(t.hh_idx >= 0, t.hh_idx, 0)
        denied = (t.hh_idx >= 0) & (ctx.world.setback_count_today[i] >= per_day)
        k = int((denied & mask).sum())
        return deny_result(
            RULE_ID,
            NAME,
            ctx,
            mask,
            denied,
            f"offset and duration within limits; no home over {per_day} setback/day",
            f"{k:,} homes already had {per_day} setback today",
            {"setback_limit": k},
        )
    return deny_result(RULE_ID, NAME, ctx, mask, np.zeros_like(mask), "offset and duration within program limits", "")


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
