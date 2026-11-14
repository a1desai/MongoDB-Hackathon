"""R05 SAFE_INDOOR (per target): the predicted max indoor temperature over the action window
must stay <= t_safe_c. A home is denied when the action would put it over t_safe_c (or make an
already-hot home hotter). For outages, the section is denied if any vulnerable home would be
pushed over t_safe_c; other homes pushed over are counted in the detail."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import deny_result, full_mask, gate_applies, not_applicable, targets_of, window_bounds
from app.governance.thermal_guard import default_predict
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R05", "SAFE_INDOOR", "deny"
EPS_C = 0.05


def _pushed_over(ctx, idx: np.ndarray, offset: float, start, end, power_off: bool) -> tuple[np.ndarray, np.ndarray]:
    predict = ctx.predictor or default_predict
    w = ctx.world
    with_action = np.asarray(predict(w, idx, offset, start, end, power_off, ctx.outdoor_c), dtype=float)
    base_off = float(0.0)
    without = np.asarray(predict(w, idx, base_off, start, end, False, ctx.outdoor_c), dtype=float)
    over = (with_action > ctx.t_safe_c) & (with_action > without + EPS_C)
    return over, with_action


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    a = request.action_type
    start, end = window_bounds(request, ctx.sim_now)
    tsafe = ctx.t_safe_c
    if a in ("precool", "thermostat_setback"):
        t = targets_of(request, ctx)
        sel = mask & (t.hh_idx >= 0)
        denied = np.zeros_like(mask, dtype=bool)
        if sel.any():
            raw = request.params.get("offset_c", request.params.get("precool_offset_c", 0.0))
            try:
                offset = float(raw)
            except (TypeError, ValueError):
                offset = 0.0
            over, pred = _pushed_over(ctx, t.hh_idx[sel], offset, start, end, False)
            denied[np.flatnonzero(sel)[over]] = True
            peak = float(pred.max()) if pred.size else 0.0
        else:
            peak = 0.0
        k = int(denied.sum())
        return deny_result(
            RULE_ID,
            NAME,
            ctx,
            mask,
            denied,
            f"all homes stay <= {tsafe:.1f} °C (max predicted {peak:.1f} °C)",
            f"{k:,} homes predicted over {tsafe:.1f} °C",
            {"over_t_safe": k},
        )
    if a == "feeder_outage":
        t = targets_of(request, ctx)
        if t.section_idx is None:
            return not_applicable(RULE_ID, NAME, mask, "unknown section")
        homes = np.flatnonzero(ctx.world.section_idx == t.section_idx)
        if homes.size == 0:
            return deny_result(RULE_ID, NAME, ctx, mask, np.zeros_like(mask), "section has no homes", "")
        over, pred = _pushed_over(ctx, homes, 0.0, start, end, True)
        vul = ctx.world.vulnerable[homes]
        k_vul, k_all = int((over & vul).sum()), int(over.sum())
        denied = mask.copy() if k_vul > 0 else np.zeros_like(mask)
        return deny_result(
            RULE_ID,
            NAME,
            ctx,
            mask,
            denied,
            f"no vulnerable home pushed over {tsafe:.1f} °C ({k_all:,} homes over; max {float(pred.max()):.1f} °C)",
            f"{k_vul:,} vulnerable homes predicted over {tsafe:.1f} °C during the outage",
            {"vulnerable_over_t_safe": k_vul, "homes_over_t_safe": k_all},
        )
    return not_applicable(RULE_ID, NAME, mask)


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
