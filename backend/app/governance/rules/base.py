"""Shared helpers for the 16 Policy Gate rules.

Every rule module exposes:
    RULE_ID, NAME, EFFECT
    evaluate(request, ctx, mask) -> (RuleResult, denied_mask)
    check(request, ctx, mask=None) -> RuleResult

`mask` is a bool array over the request's resolved targets (households in a device batch,
messages in an SMS batch, or a single section / business / execution). True means "still
allowed when this rule runs". `denied_mask` is the subset of `mask` this rule denies; a
request-level denial returns `mask` itself (everything still allowed is denied). Rules with a
needs_approval effect never deny targets; they return an all-False mask and set passed=False.
Rules are pure: no I/O, no mutation of the World.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np

from app.models import ActionRequest, RuleResult
from app.timeutil import at, parse_sim

if TYPE_CHECKING:
    from app.governance.context import GateContext
    from app.sim.world_state import World

DEVICE_ACTIONS = ("precool", "thermostat_setback", "thermostat_restore", "ev_pause", "battery_discharge")
THERMOSTAT_ACTIONS = ("precool", "thermostat_setback", "thermostat_restore")
SECTION_ACTIONS = ("feeder_outage", "feeder_restore")
MESSAGE_ACTIONS = ("send_sms", "business_curtail_request")


# ---------------------------------------------------------------- params


def seed_params(rule_id: str) -> dict[str, Any]:
    try:
        from app.governance.seed import POLICIES_BY_ID

        return dict(POLICIES_BY_ID[rule_id].params)
    except Exception:
        return {}


def params(policies: dict, rule_id: str) -> dict[str, Any]:
    """Policy params for a rule: the loaded (versioned) policy, falling back to seed values per key."""
    merged = seed_params(rule_id)
    pol = policies.get(rule_id) if policies else None
    if pol is not None:
        merged.update(pol.params or {})
    return merged


def policy_active(ctx: GateContext, rule_id: str) -> bool:
    pol = ctx.policies.get(rule_id)
    return True if pol is None else bool(pol.active)


def policy_covers(ctx: GateContext, rule_id: str, action_type: str) -> bool:
    """The policy's applies_to list (["*"] or empty = all actions) includes this action type."""
    pol = ctx.policies.get(rule_id)
    if pol is None or not pol.applies_to or "*" in pol.applies_to:
        return True
    return action_type in pol.applies_to


def effect_of(ctx: GateContext, rule_id: str, default: str) -> str:
    pol = ctx.policies.get(rule_id)
    return pol.effect if pol is not None else default


# ---------------------------------------------------------------- targets


@dataclass
class ResolvedTargets:
    request_id: str
    kind: str  # households | messages | section | business | execution
    ids: list[str]
    hh_idx: np.ndarray  # int64 world index per target, -1 when the target is not a known household
    section_idx: int | None = None
    bodies: list[str] = field(default_factory=list)
    offers: list[dict[str, Any] | None] = field(default_factory=list)

    @property
    def n(self) -> int:
        return len(self.ids)

    @property
    def is_household(self) -> np.ndarray:
        return self.hh_idx >= 0


def _hh_idx(world: World, hh_id: str) -> int:
    try:
        i = world.hh_index(hh_id)
    except Exception:
        return -1
    return i if hh_id.startswith("hh_") and 0 <= i < world.n else -1


def resolve_targets(request: ActionRequest, world: World) -> ResolvedTargets:
    a = request.action_type
    t = request.targets
    if a in DEVICE_ACTIONS:
        ids = list(t.household_ids)
        return ResolvedTargets(request.id, "households", ids, np.array([_hh_idx(world, h) for h in ids], dtype=np.int64))
    if a in MESSAGE_ACTIONS and (request.messages or t.recipient_ids):
        if request.messages:
            ids = [m.to for m in request.messages]
            bodies = [m.body for m in request.messages]
            offers = [m.offer for m in request.messages]
        else:
            ids = list(t.recipient_ids)
            bodies = [str(request.params.get("body", ""))] * len(ids)
            offers = [request.params.get("offer")] * len(ids)
        idx = np.array([_hh_idx(world, r) for r in ids], dtype=np.int64)
        return ResolvedTargets(request.id, "messages", ids, idx, bodies=bodies, offers=offers)
    if a in SECTION_ACTIONS:
        sid = t.section_id or ""
        s_idx = world.section_ids.index(sid) if sid in world.section_ids else None
        return ResolvedTargets(request.id, "section", [sid], np.array([-1], dtype=np.int64), section_idx=s_idx)
    if a == "business_curtail_request":
        return ResolvedTargets(request.id, "business", [t.business_id or ""], np.array([-1], dtype=np.int64))
    return ResolvedTargets(request.id, "execution", [request.undo_of or request.id], np.array([-1], dtype=np.int64))


def targets_of(request: ActionRequest, ctx: GateContext) -> ResolvedTargets:
    r = ctx.resolved
    if r is None or r.request_id != request.id:
        r = resolve_targets(request, ctx.world)
    return r


def full_mask(request: ActionRequest, ctx: GateContext) -> np.ndarray:
    return np.ones(targets_of(request, ctx).n, dtype=bool)


def section_home_mask(world: World, section_idx: int) -> np.ndarray:
    return world.section_idx == section_idx


def affected_home_idx(request: ActionRequest, ctx: GateContext, mask: np.ndarray) -> np.ndarray:
    """World indices of the households this request would touch, limited to targets in `mask`.

    Device batches and SMS: the masked household targets. Outages/restores: every home in the
    section. Undo: the original request's household or section targets, when the rollback copied them.
    """
    w = ctx.world
    t = targets_of(request, ctx)
    mask = np.asarray(mask, dtype=bool)
    if t.kind in ("households", "messages"):
        return np.unique(t.hh_idx[mask & (t.hh_idx >= 0)])
    if t.kind == "section":
        if t.section_idx is None or not mask.any():
            return np.zeros(0, dtype=np.int64)
        return np.flatnonzero(section_home_mask(w, t.section_idx))
    if t.kind == "execution" and mask.any():
        hh = [_hh_idx(w, h) for h in request.targets.household_ids]
        idx = np.array([i for i in hh if i >= 0], dtype=np.int64)
        sid = request.targets.section_id
        if sid and sid in w.section_ids:
            idx = np.union1d(idx, np.flatnonzero(section_home_mask(w, w.section_ids.index(sid))))
        return np.unique(idx)
    return np.zeros(0, dtype=np.int64)


# ---------------------------------------------------------------- windows


def _iso(v: Any) -> datetime | None:
    if isinstance(v, str) and "T" in v:
        try:
            return parse_sim(v)
        except Exception:
            return None
    return None


def window_bounds(request: ActionRequest, now: datetime) -> tuple[datetime, datetime]:
    """Action window as datetimes. Prefers ISO params.start/end (what the Targeter and Restorer
    send), then the "HH:MM" window on the sim day, then now + duration_min (or 60 min)."""
    p = request.params or {}
    start, end = _iso(p.get("start")), _iso(p.get("end")) or _iso(p.get("until"))
    if request.window is not None:
        try:
            if start is None:
                start = at(now, request.window.start)
            if end is None:
                end = at(now, request.window.end)
                if end <= start:
                    end = end + timedelta(days=1)
        except Exception:
            pass
    if start is None:
        start = now
    if end is None or end <= start:
        dur = p.get("duration_min")
        try:
            end = start + timedelta(minutes=int(dur) if dur else 60)
        except (TypeError, ValueError):
            end = start + timedelta(minutes=60)
    return start, end


def duration_min(request: ActionRequest, now: datetime) -> int | None:
    """Explicit duration_min param, else the window length, else None."""
    dur = request.params.get("duration_min")
    if dur is not None:
        try:
            return int(dur)
        except (TypeError, ValueError):
            return None
    if request.window is not None:
        s, e = window_bounds(request, now)
        return int((e - s).total_seconds() // 60)
    return None


# ---------------------------------------------------------------- results


def not_applicable(rule_id: str, name: str, mask: np.ndarray, detail: str = "not applicable") -> tuple[RuleResult, np.ndarray]:
    return RuleResult(rule_id=rule_id, name=name, applies=False, passed=True, effect="none", detail=detail), np.zeros_like(mask, dtype=bool)


def gate_applies(rule_id: str, name: str, request: ActionRequest, ctx: GateContext, mask: np.ndarray) -> tuple[RuleResult, np.ndarray] | None:
    """Return a not-applicable result if the policy is inactive or does not cover this action type."""
    if not policy_active(ctx, rule_id):
        return not_applicable(rule_id, name, mask, "policy inactive")
    if not policy_covers(ctx, rule_id, request.action_type):
        return not_applicable(rule_id, name, mask)
    return None


def deny_result(
    rule_id: str,
    name: str,
    ctx: GateContext,
    mask: np.ndarray,
    denied: np.ndarray,
    detail_pass: str,
    detail_fail: str,
    counts: dict[str, int] | None = None,
) -> tuple[RuleResult, np.ndarray]:
    """Build a deny-effect result from a denied mask (denied is clipped to mask)."""
    denied = np.asarray(denied, dtype=bool) & mask
    k = int(denied.sum())
    c = {"checked": int(mask.sum()), "denied": k}
    if counts:
        c.update({key: int(v) for key, v in counts.items()})
    passed = k == 0
    return (
        RuleResult(
            rule_id=rule_id,
            name=name,
            applies=True,
            passed=passed,
            effect=effect_of(ctx, rule_id, "deny"),
            detail=detail_pass if passed else detail_fail,
            counts=c,
        ),
        denied,
    )


def request_level(
    rule_id: str, name: str, ctx: GateContext, mask: np.ndarray, ok: bool, detail_pass: str, detail_fail: str, counts: dict[str, int] | None = None
) -> tuple[RuleResult, np.ndarray]:
    """A whole-request deny rule: failing denies every target still allowed."""
    denied = np.zeros_like(mask, dtype=bool) if ok else mask.copy()
    return deny_result(rule_id, name, ctx, mask, denied, detail_pass, detail_fail, counts)


def approval_result(
    rule_id: str, name: str, ctx: GateContext, mask: np.ndarray, needs: bool, detail_pass: str, detail_fail: str, counts: dict[str, int] | None = None
) -> tuple[RuleResult, np.ndarray]:
    return (
        RuleResult(
            rule_id=rule_id,
            name=name,
            applies=True,
            passed=not needs,
            effect=effect_of(ctx, rule_id, "needs_approval"),
            detail=detail_fail if needs else detail_pass,
            counts={k: int(v) for k, v in (counts or {}).items()},
        ),
        np.zeros_like(mask, dtype=bool),
    )


def fmt_n(x: float) -> str:
    return f"{int(round(x)):,}"
