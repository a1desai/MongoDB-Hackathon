"""Helpers shared by the governed agents: JSON-safe values, simulated-time windows, MW estimates.

Cross-workstream modules (app.sim.*, app.governance.*) are imported lazily inside functions so
the agents stay importable (and unit-testable) on their own.
"""

from __future__ import annotations

import logging
import math
from datetime import date, datetime, timedelta
from typing import Any

import numpy as np
from pydantic import BaseModel

from ..models import Evidence, Window
from ..sim.world_state import World
from ..timeutil import TZ, at, hhmm, sim_iso

log = logging.getLogger("cooldown.agents")

ACTING_FOR = "human:supervisor"
MAX_BATCH = 2000  # devices or recipients per action_request (PLAN §8.2)
EV_KW = 7.2
BATTERY_KW = 5.0
SMS_DUTY_CUT = 0.30  # an accepted incentive cuts AC duty by 30% (PLAN §7.4)


# ---------------------------------------------------------------- JSON-safe values

def plain(obj: Any) -> Any:
    """Recursively convert numpy / pydantic / datetime values into plain JSON-able Python.

    LangGraph's checkpoint serializer and Mongo both reject numpy scalars.
    """
    if obj is None or isinstance(obj, (str, bool, int)):
        return obj
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, np.bool_):
        return bool(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        v = float(obj)
        return v if math.isfinite(v) else None
    if isinstance(obj, np.ndarray):
        return [plain(x) for x in obj.tolist()]
    if isinstance(obj, BaseModel):
        return plain(obj.model_dump(by_alias=True, exclude_none=True))
    if isinstance(obj, datetime):
        return sim_iso(obj)
    if isinstance(obj, date):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {str(k): plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [plain(x) for x in obj]
    return str(obj)


def r1(x: float) -> float:
    return round(float(x), 1)


# ---------------------------------------------------------------- simulated time

def minutes(hh_mm: str) -> int:
    """'17:40' -> 1060 (minutes since local midnight)."""
    h, m = (int(x) for x in hh_mm.split(":")[:2])
    return h * 60 + m


def hhmm_of(mins: int) -> str:
    mins = max(0, min(int(mins), 24 * 60 - 1))
    return f"{mins // 60:02d}:{mins % 60:02d}"


def window_dt(world: World, window: Window | dict) -> tuple[datetime, datetime]:
    """A Window ('HH:MM' strings) as timezone-aware datetimes on the simulated day."""
    w = window if isinstance(window, Window) else Window(**window)
    start = at(world.sim_now, w.start)
    end = at(world.sim_now, w.end)
    if end <= start:
        end = start + timedelta(minutes=5)
    return start, end


def clamp_window(world: World, start: datetime, end: datetime) -> Window:
    end = min(end, world.sim_end)
    return Window(start=hhmm(start), end=hhmm(max(end, start + timedelta(minutes=5))))


def ceil_tick(world: World, t: datetime) -> datetime:
    """Round a time up to the next 5-minute tick boundary."""
    step = 5
    extra = (-(t.minute % step)) % step
    return (t + timedelta(minutes=extra)).replace(second=0, microsecond=0)


# ---------------------------------------------------------------- physics-based estimates

def outdoor_at(world: World, t: datetime) -> float:
    """Outdoor °C from the sim's weather module, else a synthetic heat-wave curve."""
    try:
        from ..sim import weather

        return float(weather.outdoor_c(world, t))
    except Exception:  # noqa: BLE001 - sim/weather may not be importable in unit tests
        delta = float((world.scenario.get("weather") or {}).get("delta_c", 0.0))
        h = t.astimezone(TZ).hour + t.astimezone(TZ).minute / 60
        # ~24 °C overnight, 36 °C at 16:00 (PLAN §5.1 synthetic fallback), plus scenario delta
        return 30.0 + delta + 6.0 * math.cos((h - 16.0) / 24 * 2 * math.pi)


def ac_present(world: World) -> np.ndarray:
    return (world.ac_type > 0) & (world.k_ac > 0)


def duty_at(world: World, t_out: float, offset_c: float | np.ndarray = 0.0) -> np.ndarray:
    """Steady-state AC duty per home for an outdoor temperature (PLAN §7.3 duty_hold)."""
    k_ac = np.where(world.k_ac > 0, world.k_ac, 1.0)
    s = world.setpoint_base + offset_c
    duty = np.clip(world.k_env * (t_out - s) / k_ac, 0.0, 1.0)
    return np.where(ac_present(world), duty, 0.0)


def kw_per_c(world: World, t_out: float) -> np.ndarray:
    """kW shed per home per +1 °C of setpoint while AC is running (from the duty equation)."""
    k_ac = np.where(world.k_ac > 0, world.k_ac, 1.0)
    running = duty_at(world, t_out) > 0
    return np.where(running & ac_present(world), world.ac_kw * world.k_env / k_ac, 0.0)


def per_masked(arr: Any, mask: np.ndarray) -> np.ndarray:
    """Normalise a sim helper's result to one value per masked home."""
    a = np.asarray(arr, dtype=float)
    if a.ndim == 0:
        return np.full(int(mask.sum()), float(a))
    if a.shape[0] == mask.shape[0] and a.shape[0] != int(mask.sum()):
        return a[mask]
    return a


def kw_saved(world: World, mask: np.ndarray, offset_c: float, t_out: float) -> np.ndarray:
    """kW saved per masked home by a setpoint offset (thermal.kw_saved_estimate when available)."""
    if not mask.any():
        return np.zeros(0)
    try:
        from ..sim import thermal

        est = per_masked(thermal.kw_saved_estimate(world, mask, offset_c), mask)
        if est.shape[0] == int(mask.sum()) and np.isfinite(est).all():
            return np.abs(est)
    except Exception as exc:  # noqa: BLE001
        log.debug("kw_saved_estimate unavailable, using closed form: %s", exc)
    return kw_per_c(world, t_out)[mask] * abs(offset_c)


def predicted_max_tin(
    world: World, mask: np.ndarray, offset_c: float, start: datetime, end: datetime, t_out: float
) -> np.ndarray:
    """Predicted max indoor °C over a window per masked home (thermal.predict_max_tin when available)."""
    if not mask.any():
        return np.zeros(0)
    try:
        from ..sim import thermal

        est = per_masked(thermal.predict_max_tin(world, mask, offset_c, start, end), mask)
        if est.shape[0] == int(mask.sum()) and np.isfinite(est).all():
            return est
    except Exception as exc:  # noqa: BLE001
        log.debug("predict_max_tin unavailable, using closed form: %s", exc)
    # AC holds the shifted setpoint unless it saturates; homes without AC drift toward T_out.
    hold = world.setpoint_base[mask] + offset_c + 0.3
    hours = max((end - start).total_seconds() / 3600, 0.0)
    drift = world.t_in[mask] + (t_out - world.t_in[mask]) * (1 - np.exp(-world.k_env[mask] * hours))
    return np.where(ac_present(world)[mask], np.maximum(hold, world.t_in[mask]), drift)


def to_mw(kw: float | np.ndarray, scale: float) -> float:
    """Sum of simulated-home kW -> real-scale MW."""
    return float(np.sum(kw)) * float(scale) / 1000.0


# ---------------------------------------------------------------- evidence

def evidence_kind(eid: str) -> str:
    if eid.startswith("fc_"):
        return "forecast"
    if eid.startswith("mem_"):
        return "memory"
    if eid.startswith("msg_"):
        return "message"
    if eid.startswith("plan_"):
        return "plan"
    if eid.startswith("seq:"):
        return "ledger"
    if eid.startswith(("pd_", "R")):
        return "policy"
    return "metric"


async def resolve_evidence(run_id: str, ids: list[str]) -> list[Evidence]:
    """Turn cited ids into Evidence, marking inbound public messages as untrusted."""
    out: list[Evidence] = []
    seen: set[str] = set()
    msg_ids = [i for i in ids if i.startswith("msg_")]
    trust_by_id: dict[str, str] = {}
    if msg_ids:
        try:
            from ..db import MESSAGES, db

            async for m in db()[MESSAGES].find({"_id": {"$in": msg_ids}}, {"trust": 1}):
                trust_by_id[m["_id"]] = m.get("trust", "untrusted")
        except Exception as exc:  # noqa: BLE001
            log.warning("could not resolve message trust: %s", exc)
    for eid in ids:
        if not eid or eid in seen:
            continue
        seen.add(eid)
        kind = evidence_kind(eid)
        trust = trust_by_id.get(eid, "untrusted") if kind == "message" else "system"
        out.append(Evidence(id=eid, kind=kind, trust=trust))  # type: ignore[arg-type]
    return out


def is_halted(world: World, agent_id: str) -> bool:
    return bool(world.halted) and world.halted_scope in ("all", agent_id)


def now_iso(world: World) -> str:
    return sim_iso(world.sim_now)
