"""R02 CONSENT (per target): device commands only for households enrolled in the matching
CoolShift program; SMS only to opted-in recipients who have not replied STOP."""

from __future__ import annotations

import numpy as np

from app.governance.rules.base import deny_result, full_mask, gate_applies, not_applicable, targets_of
from app.models import ActionRequest, RuleResult

RULE_ID, NAME, EFFECT = "R02", "CONSENT", "deny"


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    w = ctx.world
    t = targets_of(request, ctx)
    a = request.action_type
    known = t.hh_idx >= 0
    i = np.where(known, t.hh_idx, 0)
    if a in ("precool", "thermostat_setback", "thermostat_restore"):
        ok = known & w.has_thermostat[i] & w.consent_thermostat[i]
        program = "CoolShift thermostat"
    elif a == "ev_pause":
        ok = known & w.has_ev[i] & w.consent_ev[i]
        program = "ChargeFlow EV"
    elif a == "battery_discharge":
        ok = known & w.has_battery[i] & w.consent_battery[i]
        program = "PowerCell battery"
    elif a == "send_sms" and t.kind == "messages":
        is_biz = np.array([r.startswith("biz_") for r in t.ids], dtype=bool)
        ok = is_biz | (known & w.sms_opt_in[i] & ~w.sms_stopped[i])
        program = "TextLine opt-in"
    else:
        return not_applicable(RULE_ID, NAME, mask)
    denied = ~ok
    return deny_result(
        RULE_ID,
        NAME,
        ctx,
        mask,
        denied,
        f"all {int(mask.sum()):,} targets consented ({program})",
        f"{int((denied & mask).sum()):,} targets without {program} consent",
        {"unknown": int((~known & mask).sum())} if a != "send_sms" else None,
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
