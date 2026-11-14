"""R06 MESSAGING (per message): at most max_per_recipient_per_day per household, none in quiet
hours, <= max_chars, incentive <= max_incentive_cad and within budget, no denylisted health
terms, and the required STOP suffix (household recipients)."""

from __future__ import annotations

import re
from datetime import time

import numpy as np

from app.governance.rules.base import deny_result, full_mask, gate_applies, not_applicable, params, targets_of
from app.models import ActionRequest, RuleResult
from app.timeutil import TZ

RULE_ID, NAME, EFFECT = "R06", "MESSAGING", "deny"


def _hm(s: str) -> time:
    h, m = (int(x) for x in s.split(":"))
    return time(h, m)


def in_quiet_hours(now, quiet_start: str, quiet_end: str) -> bool:
    t = now.astimezone(TZ).time()
    qs, qe = _hm(quiet_start), _hm(quiet_end)
    return (t >= qs or t < qe) if qs > qe else (qs <= t < qe)


def evaluate(request: ActionRequest, ctx, mask: np.ndarray) -> tuple[RuleResult, np.ndarray]:
    if (na := gate_applies(RULE_ID, NAME, request, ctx, mask)) is not None:
        return na
    t = targets_of(request, ctx)
    if t.kind != "messages":
        return not_applicable(RULE_ID, NAME, mask, "no outbound messages")
    p = params(ctx.policies, RULE_ID)
    if in_quiet_hours(ctx.sim_now, p["quiet_start"], p["quiet_end"]):
        return deny_result(RULE_ID, NAME, ctx, mask, mask.copy(), "", f"quiet hours {p['quiet_start']}–{p['quiet_end']}", {"quiet_hours": int(mask.sum())})

    w = ctx.world
    max_chars = int(p["max_chars"])
    max_inc = float(p["max_incentive_cad"])
    per_day = int(p["max_per_recipient_per_day"])
    suffix = str(p["required_suffix"]).strip().lower()
    deny_re = re.compile(r"\b(" + "|".join(re.escape(x) for x in p.get("denylist", [])) + r")", re.I) if p.get("denylist") else None
    budget = float(ctx.budget_left_cad)

    reasons = {"too_long": 0, "denylist": 0, "missing_stop": 0, "incentive": 0, "budget": 0, "rate_limit": 0}
    denied = np.zeros(t.n, dtype=bool)
    sent_now: dict[str, int] = {}
    spend = 0.0
    for j in range(t.n):
        if not mask[j]:
            continue
        body = t.bodies[j] or ""
        is_hh = t.hh_idx[j] >= 0
        why = None
        if len(body) > max_chars:
            why = "too_long"
        elif deny_re is not None and deny_re.search(body):
            why = "denylist"
        elif is_hh and not body.rstrip().lower().endswith(suffix):
            why = "missing_stop"
        else:
            offer = t.offers[j] or {}
            amt = float(offer.get("amount_cad") or 0.0)
            if amt > max_inc:
                why = "incentive"
            elif amt > 0 and spend + amt > budget:
                why = "budget"
            elif is_hh:
                rid = t.ids[j]
                prior = int(w.sms_count_today[t.hh_idx[j]]) + sent_now.get(rid, 0)
                if prior >= per_day:
                    why = "rate_limit"
            if why is None:
                spend += amt
                if is_hh:
                    sent_now[t.ids[j]] = sent_now.get(t.ids[j], 0) + 1
        if why:
            denied[j] = True
            reasons[why] += 1
    k = int(denied.sum())
    fail = ", ".join(f"{v} {r.replace('_', ' ')}" for r, v in reasons.items() if v)
    return deny_result(
        RULE_ID,
        NAME,
        ctx,
        mask,
        denied,
        f"{int(mask.sum()):,} messages pass content, rate and budget checks (${spend:,.0f})",
        f"{k:,} messages denied: {fail}",
        {r: v for r, v in reasons.items() if v},
    )


def check(request: ActionRequest, ctx, mask: np.ndarray | None = None) -> RuleResult:
    return evaluate(request, ctx, full_mask(request, ctx) if mask is None else mask)[0]
