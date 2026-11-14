"""The Verifier (PLAN §10.3): decides whether a sent command actually took effect.

"Sent" is never "took effect". On every tick after a command was sent, the verifier reads the
mock system's own state back (batch/device counts) and the world arrays:
- verified: >= 90% of addressed targets applied
- partial:  some applied, below 90%, once nothing is pending or 3 ticks have passed
- failed:   nothing applied after 3 ticks (or the system lost the command)
It computes delivered_mw (real-scale MW of relief, from the world arrays), updates the
executions doc and the action_request, and logs execution_verified / execution_failed.
A verified undo marks the original execution and its request "rolled_back".
"""

import logging
from typing import Any

import numpy as np

from ..db import ACTION_REQUESTS, EXECUTIONS, db
from ..mock_systems import command_status, get_command
from ..mock_systems.common import APPLIED, Command
from ..models import Execution, Verification
from ..sim.world_state import World
from ..timeutil import sim_iso
from .executor import ledger_append, set_request_status

log = logging.getLogger(__name__)

VERIFIED_RATIO = 0.90
TIMEOUT_TICKS = 3
EV_KW = 7.2
SMS_SAVED_FRACTION = 0.3 / 0.7  # accepted offer: AC duty x0.7, so saved = duty * 0.3/0.7 of ac_kw
DEVICE_SYSTEMS = ("thermlink", "chargeflow", "powercell")


# ---------------------------------------------------------------- delivered MW (from world arrays)

def _real_mw(world: World, kw_sim: float) -> float:
    """Simulated-home kW -> real-scale MW."""
    return float(kw_sim) * float(world.scale) / 1000.0


def _owned_applied(world: World, cmd: Command) -> np.ndarray:
    """Household indices where this command applied and still owns the device."""
    owner = world.mock[cmd.system]["owner"]
    keep = (cmd.state == APPLIED) & (owner[cmd.idx] == cmd.handle)
    return cmd.idx[keep]


def _thermostat_kw(world: World, idx: np.ndarray, offset_c: float) -> float:
    """kW relief of an offset on these homes, measured against their base setpoint.

    Uses sim/thermal.kw_saved_estimate (the same estimate the Targeter uses for expected_mw),
    with the batch's own offset taken out so the comparison is against the base setpoint.
    """
    if idx.size == 0 or offset_c == 0:
        return 0.0
    try:
        from ..sim.thermal import kw_saved_estimate
    except ImportError:  # pragma: no cover - sim is another workstream
        return 0.0
    saved_offset = world.offset_c[idx].copy()
    try:
        world.offset_c[idx] = 0.0
        mask = np.zeros(world.n, dtype=bool)
        mask[idx] = True
        return float(kw_saved_estimate(world, mask, offset_c).sum())
    except Exception:
        log.exception("thermostat kW estimate failed")
        return 0.0
    finally:
        world.offset_c[idx] = saved_offset


def delivered_mw(world: World, cmd: Command, exe: dict[str, Any]) -> float:
    """Real-scale MW this command is delivering (or will deliver over its window) right now."""
    try:
        if cmd.system == "thermlink":
            if cmd.kind != "event" or cmd.phase == "ended":
                return 0.0
            idx = _owned_applied(world, cmd)
            idx = idx[~world.overridden[idx]]
            return _real_mw(world, _thermostat_kw(world, idx, float(cmd.params.get("offset_c") or 0.0)))
        if cmd.system == "chargeflow":
            if cmd.kind != "pause" or cmd.phase == "ended":
                return 0.0
            idx = _owned_applied(world, cmd)
            idx = idx[world.ev_paused[idx] & world.power_on[idx]]
            hour = world.sim_now.hour + world.sim_now.minute / 60.0
            arrived = np.nan_to_num(world.ev_arrival_h[idx], nan=np.inf) <= hour
            wants = arrived & (world.ev_kwh_needed[idx] > 0)
            return _real_mw(world, EV_KW * float(wants.sum()))
        if cmd.system == "powercell":
            if cmd.kind != "dispatch" or cmd.phase == "ended":
                return 0.0
            idx = _owned_applied(world, cmd)
            if cmd.phase == "active":
                on = world.battery_discharging[idx] & world.power_on[idx]
                return _real_mw(world, float(world.battery_kw[idx][on].sum()))
            # Scheduled: the units that accepted and still have charge above the floor.
            ready = world.battery_soc[idx] > 0.2
            return _real_mw(world, float(cmd.params.get("kw") or 0.0) * float(ready.sum()))
        if cmd.system == "textline":
            if exe.get("action_type") == "business_curtail_request":
                mw = 0.0
                for m, st in zip(cmd.extra.get("messages", []), cmd.state, strict=False):
                    b = world.businesses.get(m.get("to", ""))
                    if b is not None and st == APPLIED:
                        mw += float(b.get("curtailed_mw") or b.get("accepted_mw") or 0.0)
                return mw
            hh = cmd.idx[(cmd.state == APPLIED) & (cmd.idx >= 0)]
            hh = hh[world.sms_accepted[hh] & world.power_on[hh]]
            return _real_mw(world, float((world.ac_kw[hh] * world.duty[hh]).sum()) * SMS_SAVED_FRACTION)
        if cmd.system == "gridops":
            if cmd.kind != "open":
                return 0.0
            sidx = int(cmd.idx[0])
            if not world.section_cut[sidx]:
                return 0.0
            pre = exe.get("pre_state") or {}
            return float(pre.get("demand_mw") or world.section_demand_mw[sidx] or 0.0)
    except Exception:
        log.exception("delivered_mw failed for %s", cmd.id)
    return 0.0


# ---------------------------------------------------------------- verification

def _assess(world: World, exe: dict[str, Any], st: dict[str, Any], cmd: Command) -> tuple[str | None, Verification]:
    """(final status or None while still waiting, verification counts)."""
    total = int(st.get("total", 0))
    addressed = max(total, int(exe.get("targets") or 0))
    applied = int(st.get("applied", 0))
    pending = int(st.get("pending", 0))
    if cmd.system == "gridops" and applied:
        # Read back the breaker itself, not just the switch acknowledgement.
        sidx = int(cmd.idx[0])
        took = bool(world.section_cut[sidx]) if cmd.kind == "open" else not bool(world.section_cut[sidx])
        applied = 1 if took else 0
    ver = Verification(
        applied=applied,
        pending=pending,
        offline=int(st.get("offline", 0)),
        overridden=int(st.get("overridden", 0)),
        failed=int(st.get("failed", 0)) + int(st.get("ignored", 0)) + int(st.get("rejected", 0)),
        delivered_mw=round(delivered_mw(world, cmd, exe), 3),
    )
    ticks = world.tick - int(exe.get("sent_tick", world.tick))
    if pending and ticks < TIMEOUT_TICKS:
        return None, ver
    ratio = applied / addressed if addressed else 0.0
    if ratio >= VERIFIED_RATIO:
        return "verified", ver
    return ("partial" if applied else "failed"), ver


async def verify_pending(world: World) -> list[Execution]:
    """Check every "sent" execution of this run; finalize those that resolved. Never raises."""
    out: list[Execution] = []
    try:
        docs = await db()[EXECUTIONS].find({"run_id": world.run_id, "status": "sent"}).to_list(None)
    except Exception:
        log.exception("verifier could not read executions")
        return out
    for exe in docs:
        try:
            done = await _verify_one(world, exe)
        except Exception:
            log.exception("verifier failed on %s", exe.get("_id"))
            continue
        if done is not None:
            out.append(done)
    return out


async def _verify_one(world: World, exe: dict[str, Any]) -> Execution | None:
    cid = exe.get("command_id") or (exe.get("response") or {}).get("batch_id")
    cmd = get_command(world, cid) if cid else None
    st = command_status(world, cid) if cid else None
    if cmd is None or st is None:
        ticks = world.tick - int(exe.get("sent_tick", world.tick))
        if ticks < TIMEOUT_TICKS:
            return None
        status, ver = "failed", Verification()
    else:
        status, ver = _assess(world, exe, st, cmd)
        if status is None:
            if ver.model_dump() != (exe.get("verification") or {}):  # progress for the UI
                await db()[EXECUTIONS].update_one({"_id": exe["_id"]}, {"$set": {"verification": ver.model_dump()}})
            return None
    return await _finalize(world, exe, status, ver)


async def _finalize(world: World, exe: dict[str, Any], status: str, ver: Verification) -> Execution:
    ts = sim_iso(world.sim_now)
    await db()[EXECUTIONS].update_one(
        {"_id": exe["_id"], "status": "sent"},
        {"$set": {"status": status, "verification": ver.model_dump(), "verified_sim_ts": ts, "sim_ts": ts}},
    )
    exe.update(status=status, verification=ver.model_dump(), verified_sim_ts=ts, sim_ts=ts)
    req = await db()[ACTION_REQUESTS].find_one({"_id": exe["request_id"]}) or {}
    if req.get("status") not in ("cancelled", "rolled_back"):
        await set_request_status(exe["request_id"], status, ts)

    undo_of = exe.get("undo_of") or req.get("undo_of")
    if undo_of and status in ("verified", "partial"):
        orig = await db()[EXECUTIONS].find_one_and_update(
            {"_id": undo_of}, {"$set": {"status": "rolled_back", "rolled_back_by": exe["_id"], "sim_ts": ts}}
        )
        if orig:
            await set_request_status(orig["request_id"], "rolled_back", ts)

    system = exe.get("system", "")
    addressed = max(ver.applied + ver.pending + ver.offline + ver.failed, int(exe.get("targets") or 0))
    pct = f"{ver.applied / addressed:.0%}" if addressed else "0%"
    parts = [f"{ver.applied:,}/{addressed:,} applied ({pct})"]
    if ver.offline:
        parts.append(f"{ver.offline:,} offline")
    if ver.failed:
        parts.append(f"{ver.failed:,} failed/ignored")
    if ver.overridden:
        parts.append(f"{ver.overridden:,} overridden")
    if ver.pending:
        parts.append(f"{ver.pending:,} still pending after {TIMEOUT_TICKS} ticks")
    if system in DEVICE_SYSTEMS and world.scale != 1:
        parts.append(f"~{int(round(ver.applied * world.scale)):,} real homes")
    mw = f"; delivering {ver.delivered_mw:.1f} MW" if ver.delivered_mw else ""
    rb = f"; rolled back {undo_of}" if undo_of and status in ("verified", "partial") else ""
    word = {"verified": "Verified", "partial": "Partially verified", "failed": "Failed"}[status]
    await ledger_append(
        exe["run_id"],
        "execution_failed" if status == "failed" else "execution_verified",
        "agent:executor",
        f"{word} {exe['request_id']} on {system} by read-back: " + ", ".join(parts) + mw + rb,
        sim_ts=ts,
        acting_for=req.get("acting_for"),
        refs={
            "request_id": exe["request_id"],
            "decision_id": req.get("decision_id"),
            "approval_id": req.get("approval_id"),
            "execution_id": exe["_id"],
            "plan_version": req.get("plan_version"),
        },
        payload={"status": status, "verification": ver.model_dump(), "command_id": exe.get("command_id"),
                 "system": system, "undo_of": undo_of},
        langsmith_run_id=req.get("langsmith_run_id"),
    )
    return Execution.model_validate(exe)


async def refresh_delivered(world: World) -> float:
    """Recompute delivered_mw for this run's verified/partial executions whose command is still live.

    Optional helper for the Watcher (which compares delivered with expected MW). Returns the total.
    """
    total = 0.0
    try:
        docs = await db()[EXECUTIONS].find(
            {"run_id": world.run_id, "status": {"$in": ["verified", "partial"]}, "command_id": {"$ne": None}},
            {"command_id": 1, "pre_state": 1, "action_type": 1, "verification": 1},
        ).to_list(None)
    except Exception:
        log.exception("refresh_delivered could not read executions")
        return total
    for exe in docs:
        cmd = get_command(world, exe["command_id"])
        if cmd is None:
            continue
        mw = round(delivered_mw(world, cmd, exe), 3)
        total += mw
        if mw != (exe.get("verification") or {}).get("delivered_mw"):
            await db()[EXECUTIONS].update_one({"_id": exe["_id"]}, {"$set": {"verification.delivered_mw": mw}})
    return total
