"""R16 RESTORE_SAFETY (needs_approval): a restore adding X MW needs a projected margin >= X;
otherwise it must be staggered (>= stagger_groups groups, >= stagger_min apart) or approved.

Applies to feeder_restore, thermostat_restore and rollbacks (undo), since a rollback of a
load-reducing action puts that load back on the grid (PLAN §10.4)."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import affected_home_idx, approval_result, full_mask, not_applicable, params, policy_active
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R16", "RESTORE_SAFETY", "needs_approval"
RESTORE_ACTIONS = ("feeder_restore", "thermostat_restore", "undo")
BASE_KW_FALLBACK = 0.9  # non-AC household load per home when sim.thermal is unavailable (kW)


def _base_kw(world) -> np.ndarray:
    try:
        from app.sim import thermal

        return world.base_kw_scale * thermal.base_profile_kw(world.sim_now.hour + world.sim_now.minute / 60.0)
    except Exception:
        return world.base_kw_scale * BASE_KW_FALLBACK


def restore_mw(request: ActionRequest, ctx, mask: np.ndarray) -> float:
    """MW this restore adds: the proposer's figure when given, else a physics estimate (real scale)."""
    p = request.params or {}
    for key in ("mw_added", "restore_mw"):
        if p.get(key) is not None:
            try:
                return max(0.0, float(p[key]))
            except (TypeError, ValueError):
                pass
    if request.action_type != "undo" and request.expected_mw:
        return abs(float(request.expected_mw))
    w = ctx.world
    idx = affected_home_idx(request, ctx, mask)
    if idx.size == 0:
        return 0.0
    has_ac = w.ac_type[idx] > 0
    if request.action_type == "thermostat_restore":
        kw = np.where(has_ac, w.ac_kw[idx] * (1.0 - np.clip(w.duty[idx], 0.0, 1.0)), 0.0)
    else:  # power back on after an outage: hot homes run AC flat out
        kw = _base_kw(w)[idx] + np.where(has_ac, w.ac_kw[idx], 0.0)
    return float(kw.sum()) * ctx.scale / 1000.0


def is_staggered(request: ActionRequest, ctx) -> bool:
    p = params(ctx.policies, RULE_ID)
    rp = request.params or {}
    try:
        groups = int(rp.get("stagger_groups") or 0)
        gap = int(rp.get("stagger_min") or 0)
    except (TypeError, ValueError):
        return False
    return groups >= int(p["stagger_groups"]) and gap >= int(p["stagger_min"])


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if not policy_active(ctx, RULE_ID):
        return not_applicable(RULE_ID, NAME, mask, "policy inactive")
    if request.action_type not in RESTORE_ACTIONS:
        return not_applicable(RULE_ID, NAME, mask)
    x = restore_mw(request, ctx, mask)
    margin = ctx.margin()
    staggered = is_staggered(request, ctx)
    ok = x <= margin or x <= 0 or staggered
    if x <= margin or x <= 0:
        ok_detail = f"adds {x:,.1f} MW within projected margin {margin:,.1f} MW"
    else:
        g = (request.params or {}).get("stagger_group")
        ok_detail = f"adds {x:,.1f} MW > margin {margin:,.1f} MW but staggered (group {g}/{request.params.get('stagger_groups')})"
    p = params(ctx.policies, RULE_ID)
    return approval_result(
        RULE_ID,
        NAME,
        ctx,
        mask,
        not ok,
        ok_detail,
        f"adds {x:,.1f} MW > projected margin {margin:,.1f} MW and is not staggered ({p['stagger_groups']} groups, {p['stagger_min']} min apart)",
        {"restore_mw": round(x), "margin_mw": round(margin)},
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
