"""The Policy Gate (PLAN §9): deterministic Python that decides every action_request.

`evaluate` is pure (no I/O): it resolves the request's targets, runs the deny rules over a shrinking
"still allowed" mask (so each denied target is attributed to the first rule that denies it, and the
denied_reasons counts add up to denied_count), then runs the needs_approval rules on the allowed
remainder. Precedence: everything denied -> deny; else any needs_approval -> needs_approval (the
approval covers only the allowed subset); else some denied -> partial_allow; else allow.

`decide` does the I/O around it: builds the GateContext, stores the decision, updates the request
status and appends the "decision" ledger entry.
"""

from __future__ import annotations

import logging
from typing import Any

import numpy as np

from app import db as dbm
from app.governance.context import GateContext, build_gate_context
from app.governance.rules import APPROVAL_RULES, DENY_RULES
from app.governance.rules.base import affected_home_idx, fmt_n, resolve_targets
from app.governance.rules.r14_untrusted import influenced
from app.ids import new_id
from app.models import ActionRequest, BlastRadius, Decision, RuleResult
from app.sim.world_state import World
from app.timeutil import sim_iso

log = logging.getLogger(__name__)

GATE_ID = "system:gate"
MAX_FEATURED_DETAIL = 25
# Request status after the gate (approved means "may execute"; the Executor re-checks R15 + R01).
STATUS_BY_VERDICT = {"allow": "approved", "partial_allow": "approved", "deny": "denied", "needs_approval": "needs_approval"}


def _empty_targets_result() -> RuleResult:
    return RuleResult(rule_id="R00", name="TARGETS", applies=True, passed=False, effect="deny", detail="request has no targets", counts={"checked": 0})


def _blast_radius(request: ActionRequest, ctx: GateContext, mask: np.ndarray, n: int) -> BlastRadius:
    """Real-scale impact of the targets in `mask` (homes x run scale, MW scaled by allowed share)."""
    w = ctx.world
    idx = affected_home_idx(request, ctx, mask)
    homes = int(round(idx.size * ctx.scale))
    vul = int(round(int(w.vulnerable[idx].sum()) * ctx.scale)) if idx.size else 0
    share = float(mask.sum()) / n if n else 0.0
    mw = abs(float(request.expected_mw or 0.0)) * share
    if not mw and request.action_type in ("feeder_restore", "thermostat_restore", "undo"):
        mw = float((request.params or {}).get("mw_added") or (request.params or {}).get("restore_mw") or 0.0) * share
    return BlastRadius(homes_real=homes, vulnerable_real=vul, mw=round(mw, 2))


def _featured_detail(world: World, t, allowed: np.ndarray, rule_masks: list[tuple[str, np.ndarray]]) -> list[dict[str, Any]]:
    """Per-target detail for featured residents only (everything else is aggregated in counts)."""
    if not world.featured or t.kind not in ("households", "messages"):
        return []
    by_idx = {int(i): rid for rid, i in world.featured.items()}
    out: list[dict[str, Any]] = []
    for j in np.flatnonzero(t.hh_idx >= 0):
        rid = by_idx.get(int(t.hh_idx[j]))
        if rid is None:
            continue
        denied_by = [r for r, m in rule_masks if m[j]]
        out.append({"resident_id": rid, "household_id": t.ids[j], "allowed": bool(allowed[j]), "denied_by": denied_by})
        if len(out) >= MAX_FEATURED_DETAIL:
            break
    return out


def _summary(verdict: str, request: ActionRequest, allowed_n: int, denied_n: int, reasons: dict[str, int], results: list[RuleResult]) -> str:
    head = verdict.upper()
    if verdict == "partial_allow":
        why = ", ".join(f"{r} {fmt_n(c)}" for r, c in sorted(reasons.items(), key=lambda kv: -kv[1]))
        return f"{head} {request.id}: {fmt_n(allowed_n)} allowed, {fmt_n(denied_n)} denied ({why}). {request.summary}"[:500]
    failing = [r for r in results if not r.passed]
    if verdict == "allow":
        return f"ALLOW {request.id}: all rules passed. {request.summary}"[:500]
    tags = "; ".join(f"{r.rule_id} {r.name}: {r.detail}" for r in failing[:3])
    extra = f" ({fmt_n(denied_n)} denied)" if verdict == "needs_approval" and denied_n else ""
    return f"{head} {request.id}{extra}: {tags}. {request.summary}"[:500]


def evaluate(request: ActionRequest, ctx: GateContext) -> Decision:
    """Run all 16 rules (vectorized over batch targets) and apply PLAN §9 precedence. Pure."""
    t = resolve_targets(request, ctx.world)
    ctx.resolved = t
    n = t.n
    full = np.ones(n, dtype=bool)
    allowed = full.copy()
    results: list[RuleResult] = []
    reasons: dict[str, int] = {}
    rule_masks: list[tuple[str, np.ndarray]] = []

    for rule in DENY_RULES:
        # Once nothing is left, later rules still run on the full set so every failing rule shows.
        exhausted = not allowed.any()
        mask = full.copy() if exhausted else allowed.copy()
        try:
            res, denied = rule.evaluate(request, ctx, mask)
        except Exception as exc:  # a rule bug must fail closed, never crash the sim
            log.exception("rule %s crashed on %s", rule.RULE_ID, request.id)
            res = RuleResult(rule_id=rule.RULE_ID, name=rule.NAME, applies=True, passed=False, effect="deny", detail=f"rule error: {exc}")
            denied = mask.copy()
        denied = np.asarray(denied, dtype=bool) & mask
        results.append(res)
        if denied.any():
            if not exhausted:
                reasons[rule.RULE_ID] = int(denied.sum())
                allowed &= ~denied
                rule_masks.append((rule.RULE_ID, denied))
            elif n == 1:  # single target: list every rule that denies it
                reasons[rule.RULE_ID] = 1

    if n == 0:
        results.append(_empty_targets_result())
        allowed = np.zeros(0, dtype=bool)

    approve_mask = allowed if allowed.any() else full
    needs = False
    for rule in APPROVAL_RULES:
        try:
            res, _ = rule.evaluate(request, ctx, approve_mask.copy())
        except Exception as exc:
            log.exception("rule %s crashed on %s", rule.RULE_ID, request.id)
            res = RuleResult(rule_id=rule.RULE_ID, name=rule.NAME, applies=True, passed=False, effect="needs_approval", detail=f"rule error: {exc}")
        results.append(res)
        needs |= res.applies and not res.passed

    allowed_n = int(allowed.sum())
    denied_n = int(n - allowed_n)
    if allowed_n == 0:
        verdict = "deny"
    elif needs:
        verdict = "needs_approval"
    elif denied_n:
        verdict = "partial_allow"
    else:
        verdict = "allow"

    results.sort(key=lambda r: r.rule_id)
    allowed_ids: list[str] = []
    if t.kind in ("households", "messages") and verdict != "deny" and denied_n:
        allowed_ids = [t.ids[j] for j in np.flatnonzero(allowed)]
    blast = _blast_radius(request, ctx, allowed if allowed_n else full, n)

    extra: dict[str, Any] = {"target_kind": t.kind, "summary": _summary(verdict, request, allowed_n, denied_n, reasons, results)}
    featured = _featured_detail(ctx.world, t, allowed, rule_masks)
    if featured:
        extra["featured_detail"] = featured
    decision = Decision(
        _id=new_id("dec_", request.run_id),
        run_id=request.run_id,
        request_id=request.id,
        sim_ts=sim_iso(ctx.sim_now),
        verdict=verdict,
        rule_results=results,
        allowed_count=allowed_n,
        denied_count=denied_n,
        denied_reasons=reasons,
        allowed_household_ids=allowed_ids,
        blast_radius=blast,
        policy_version=ctx.policy_version,
        decided_by=GATE_ID,
        untrusted_influence=influenced(request, ctx),
        **extra,
    )
    return decision


async def decide(request: ActionRequest, world: World, run: dict) -> Decision:
    """Build the context, evaluate, store the decision, update the request, log the decision."""
    from app.governance.ledger import append

    ctx = await build_gate_context(world, run, request)
    decision = evaluate(request, ctx)
    snapshot = [ctx.policies[result.rule_id].to_mongo() for result in decision.rule_results
                if result.applies and result.rule_id in ctx.policies]
    for policy in snapshot:
        if policy["_id"] == "R05":
            policy["params"] = {**policy["params"], "t_safe_c": ctx.t_safe_c}
    decision.policy_snapshot = snapshot
    status = STATUS_BY_VERDICT[decision.verdict]
    now = sim_iso(world.sim_now)
    database = dbm.db()
    await database[dbm.DECISIONS].insert_one(decision.to_mongo())

    request.status = status  # type: ignore[assignment]
    request.decision_id = decision.id
    request.sim_ts = now
    res = await database[dbm.ACTION_REQUESTS].update_one(
        {"_id": request.id}, {"$set": {"status": status, "decision_id": decision.id, "sim_ts": now}}
    )
    if res.matched_count == 0:  # a request that was never stored (should not happen): store it now
        await database[dbm.ACTION_REQUESTS].replace_one({"_id": request.id}, request.to_mongo(), upsert=True)

    failed = [r for r in decision.rule_results if not r.passed]
    await append(
        request.run_id,
        "decision",
        GATE_ID,
        getattr(decision, "summary", f"{decision.verdict} {request.id}"),
        sim_ts=now,
        acting_for=request.acting_for,
        refs={"request_id": request.id, "decision_id": decision.id, "plan_version": request.plan_version},
        payload={
            "verdict": decision.verdict,
            "action_type": request.action_type,
            "proposed_by": request.proposed_by,
            "allowed_count": decision.allowed_count,
            "denied_count": decision.denied_count,
            "denied_reasons": decision.denied_reasons,
            "failed_rules": [{"rule_id": r.rule_id, "name": r.name, "effect": r.effect, "detail": r.detail, "counts": r.counts} for r in failed],
            "rules_checked": [r.rule_id for r in decision.rule_results if r.applies],
            "policy_snapshot": snapshot,
            "blast_radius": decision.blast_radius.model_dump(),
            "policy_version": decision.policy_version,
            "untrusted_influence": decision.untrusted_influence,
            "evidence": [e.model_dump() for e in request.evidence],
        },
        langsmith_run_id=request.langsmith_run_id,
    )
    return decision
