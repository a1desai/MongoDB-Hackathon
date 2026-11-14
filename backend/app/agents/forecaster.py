"""Forecaster (PLAN §8.2, code; FAST LLM for a one-line explanation only).

Every 3 ticks it copies the world, simulates 6 hours forward with the weather curve plus
N(0, 0.5) noise, the commands currently in effect and the *planned* supply only (it cannot
see contingencies before they happen), then stores a forecasts doc and logs a world_event.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import hashlib
import logging
from datetime import datetime, timedelta
from typing import Any

import numpy as np

from ..config import get_settings
from ..db import FORECASTS, db
from ..ids import new_id
from ..models import Forecast, ForecastExplanation, HourlyPoint, ShortfallWindow
from ..sim.world_state import World
from ..timeutil import hhmm, sim_iso
from .common import ACTING_FOR, outdoor_at, plain, r1

log = logging.getLogger("cooldown.agents.forecaster")

AGENT_ID = "agent:forecaster"
HORIZON = timedelta(hours=6)
NOISE_SIGMA_C = 0.5
MERGE_GAP_MIN = 15
MIN_WINDOW_MW = 0.5

FORECAST_SYSTEM = (
    "You are the Forecaster for Lakeshore Grid Ops. In one plain sentence (max 200 characters), explain the "
    "forecast you are given: when demand exceeds supply and by how much. No advice, no health content. "
    "Output only JSON matching the schema."
)

_last_signature: dict[str, tuple[str, str]] = {}  # run_id -> (window signature, explanation)


def copy_world(world: World, seed: int) -> World:
    """An independent copy for look-ahead: arrays and mutable state copied, own RNG."""
    clone = copy.copy(world)
    for f in dataclasses.fields(world):
        v = getattr(world, f.name)
        if isinstance(v, np.ndarray):
            setattr(clone, f.name, v.copy())
    clone.section_cut_until = list(world.section_cut_until)
    clone.businesses = copy.deepcopy(world.businesses)
    clone.archetype_params = copy.deepcopy(world.archetype_params)
    try:
        clone.mock = copy.deepcopy(world.mock)
    except Exception:  # noqa: BLE001 - mock state is optional for look-ahead
        clone.mock = {}
    clone.metrics = {}
    clone.events = []
    clone.rng = np.random.default_rng(seed)
    return clone


def _tick_minutes() -> int:
    try:
        from ..sim.clock import tick_minutes

        return int(tick_minutes())
    except Exception:  # noqa: BLE001
        return int(get_settings().tick_sim_minutes)


async def planned_restores(world: World) -> dict[int, datetime]:
    """Section index -> planned re-energise time for sections the agents have cut (the outage
    request's end: R09 caps a cut at 60 min, so the Restorer closes the breaker then)."""
    out: dict[int, datetime] = {}
    cut = [world.section_ids[k] for k in np.flatnonzero(np.asarray(world.section_cut, dtype=bool))]
    if not cut:
        return out
    try:
        from ..db import ACTION_REQUESTS
        from ..timeutil import parse_sim

        cur = db()[ACTION_REQUESTS].find(
            {"run_id": world.run_id, "action_type": "feeder_outage", "targets.section_id": {"$in": cut},
             "status": {"$in": ["executing", "verified", "partial"]}},
            {"targets.section_id": 1, "params.end": 1}).sort("created_sim_ts", 1)
        async for d in cur:
            end = (d.get("params") or {}).get("end")
            if end:
                out[world.section_index(d["targets"]["section_id"])] = parse_sim(str(end))
    except Exception as exc:  # noqa: BLE001 - without it the forecast assumes cuts persist
        log.debug("planned restores unavailable: %s", exc)
    return out


def simulate(world: World, seed: int, horizon: timedelta = HORIZON,
             restores: dict[int, datetime] | None = None) -> list[tuple[datetime, float, float]]:
    """Run the copy forward; returns [(t, demand_mw, planned_supply_mw)] per tick. CPU only.

    Agent cuts are re-energised at their planned restore time (`restores`) and uncontrolled-trip
    cuts when their 90 minutes are up, so the forecast shows the load that comes back."""
    from ..sim import grid, thermal

    try:
        from ..sim import behavior
    except Exception:  # noqa: BLE001
        behavior = None  # type: ignore[assignment]

    w = copy_world(world, seed)
    step = timedelta(minutes=_tick_minutes())
    dt_h = step.total_seconds() / 3600
    end = min(world.sim_now + horizon, world.sim_end)
    hours = int(horizon.total_seconds() // 3600) + 2
    noise = w.rng.normal(0.0, NOISE_SIGMA_C, size=hours)  # hourly forecast error, N(0, 0.5) °C
    series: list[tuple[datetime, float, float]] = []
    t = world.sim_now
    while t < end:
        t = t + step
        w.sim_now = t
        w.tick += 1
        for k, t_restore in (restores or {}).items():
            if t >= t_restore and bool(w.section_cut[k]):
                grid.set_section_power(w, k, True, None, source="agent")
        grid.release_expired_cuts(w)
        hour_i = int((t - world.sim_now).total_seconds() // 3600)
        t_out = outdoor_at(w, t) + float(noise[min(hour_i, hours - 1)])
        thermal.step_thermal(w, dt_h, t_out)
        if behavior is not None:
            try:
                behavior.step_behavior(w, dt_h)
            except Exception as exc:  # noqa: BLE001 - follower behaviour is a refinement
                log.debug("forecast behaviour step skipped: %s", exc)
                behavior = None  # type: ignore[assignment]
        agg = grid.aggregate(w) or {}
        demand = float(agg.get("demand_mw", w.demand_mw))
        try:
            supply = float(grid.supply_mw(w, t))  # planned supply: only contingencies already fired
        except Exception:  # noqa: BLE001
            supply = float(agg.get("supply_mw", w.supply_mw))
        series.append((t, demand, supply))
    return series


def summarise(series: list[tuple[datetime, float, float]], step_min: int) -> tuple[list[HourlyPoint], list[ShortfallWindow]]:
    """Hourly peak demand vs supply, and merged windows where demand exceeds supply."""
    hourly: dict[str, list[float]] = {}
    for t, d, s in series:
        key = f"{t.hour:02d}:00"
        h = hourly.setdefault(key, [0.0, float("inf"), 0.0])
        h[0] = max(h[0], d)
        h[1] = min(h[1], s)
        h[2] = max(h[2], d - s)
    points = [HourlyPoint(hour=k, demand_mw=r1(v[0]), supply_mw=r1(v[1]), shortfall_mw=r1(max(v[2], 0.0)))
              for k, v in hourly.items()]

    runs: list[list[Any]] = []  # [start, end, peak]
    for t, d, s in series:
        short = d - s
        if short <= 0:
            continue
        if runs and (t - runs[-1][1]) <= timedelta(minutes=MERGE_GAP_MIN):
            runs[-1][1] = t
            runs[-1][2] = max(runs[-1][2], short)
        else:
            runs.append([t, t, short])
    windows = [
        ShortfallWindow(start=hhmm(a - timedelta(minutes=step_min)), end=hhmm(b), peak_short_mw=r1(p))
        for a, b, p in runs if p >= MIN_WINDOW_MW
    ]
    return points, windows


def _deterministic_text(windows: list[ShortfallWindow], horizon_end: str) -> str:
    if not windows:
        return f"No shortfall expected before {horizon_end}: planned supply covers forecast demand."
    w = max(windows, key=lambda x: x.peak_short_mw)
    extra = f" (+{len(windows) - 1} more window)" if len(windows) > 1 else ""
    return f"Demand exceeds planned supply {w.start}-{w.end}, peaking {w.peak_short_mw:.0f} MW short{extra}."[:200]


async def _explain(world: World, windows: list[ShortfallWindow], hourly: list[HourlyPoint], horizon_end: str) -> tuple[str, str]:
    """FAST LLM one-liner, only when the windows change materially (else reuse the last text)."""
    text = _deterministic_text(windows, horizon_end)
    sig = "|".join(f"{w.start}-{w.end}:{round(w.peak_short_mw / 25)}" for w in windows) or "none"
    prev = _last_signature.get(world.run_id)
    if prev and prev[0] == sig:
        return prev[1], "fallback"
    source = "fallback"
    try:
        from ..llm.client import complete_json

        res = await complete_json(
            agent=AGENT_ID, model=get_settings().llm_model_fast, system=FORECAST_SYSTEM,
            payload=plain({"now": hhmm(world.sim_now), "windows": [w.model_dump() for w in windows],
                           "hourly": [h.model_dump() for h in hourly]}),
            schema=ForecastExplanation, fallback=lambda: ForecastExplanation(explanation=text),
            metadata={"run_id": world.run_id, "sim_ts": sim_iso(world.sim_now), "agent_id": AGENT_ID},
        )
        text, source = res.output.explanation, res.source
    except Exception as exc:  # noqa: BLE001
        log.debug("forecast explanation fallback: %s", exc)
    _last_signature[world.run_id] = (sig, text)
    return text, source


async def update(world: World) -> Forecast:
    """Copies state, simulates 6 h forward; stores forecasts doc; ledger world_event."""
    from ..governance.ledger import append

    seed_src = f"{world.run_id}:{world.tick}".encode()
    seed = int.from_bytes(hashlib.sha256(seed_src).digest()[:4], "big")
    step_min = _tick_minutes()
    try:
        restores = await planned_restores(world)
        series = await asyncio.to_thread(simulate, world, seed, HORIZON, restores)
    except Exception as exc:  # noqa: BLE001 - never stall the sim: fall back to "now persists"
        log.exception("forecast simulation failed, using persistence forecast: %s", exc)
        series = [(world.sim_now + timedelta(minutes=step_min * k), world.demand_mw, world.supply_mw)
                  for k in range(1, int(HORIZON.total_seconds() // 60 // step_min) + 1)
                  if world.sim_now + timedelta(minutes=step_min * k) <= world.sim_end]
    hourly, windows = summarise(series, step_min)
    horizon_end = hhmm(series[-1][0]) if series else hhmm(world.sim_now)
    explanation, source = await _explain(world, windows, hourly, horizon_end)

    fc = Forecast(id=new_id("fc_", world.run_id), run_id=world.run_id, sim_ts=sim_iso(world.sim_now),
                  hourly=hourly, windows=windows, explanation=explanation, source=source)  # type: ignore[arg-type]
    peak = max((w.peak_short_mw for w in windows), default=0.0)
    doc = fc.to_mongo()
    doc.update({"horizon_end": horizon_end, "peak_short_mw": peak, "noise_sigma_c": NOISE_SIGMA_C,
                "created_by": AGENT_ID, "acting_for": ACTING_FOR})
    await db()[FORECASTS].insert_one(doc)
    fc = Forecast.model_validate(doc)  # carries horizon_end / peak_short_mw as extra fields

    if windows:
        w = max(windows, key=lambda x: x.peak_short_mw)
        summary = f"Forecast {fc.id}: shortfall {w.start}-{w.end}, peak {w.peak_short_mw:.0f} MW (to {horizon_end})"
    else:
        summary = f"Forecast {fc.id}: no shortfall through {horizon_end}"
    await append(
        world.run_id, "world_event", AGENT_ID, summary, sim_ts=fc.sim_ts, acting_for=ACTING_FOR,
        payload=plain({"kind": "forecast", "forecast_id": fc.id, "windows": [w.model_dump() for w in windows],
                       "peak_short_mw": peak, "horizon_end": horizon_end, "explanation": explanation,
                       "source": source}),
    )
    return fc
