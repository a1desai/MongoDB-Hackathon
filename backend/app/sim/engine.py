"""RunEngine: owns one run's tick loop (PLAN §7.2, CONTRACTS "Engine and API (W8)").

Per tick, in order:
  world_step -> mock apply_pending + persist -> grid aggregate -> feeder_load (time series)
  -> contingencies / uncontrolled trip / expired world cuts -> world events to the ledger (sim:world)
  -> every 3 ticks: forecaster + map snapshot
  -> governed: watcher -> Governance.planning_cycle on triggers + process_request for proposed
     requests; baseline: BaselineOperator
  -> verifier -> restorer -> approvals expiry -> world LLM agents (background tasks)
  -> metrics -> WebSocket broadcast -> (pause_on_approval hold) -> clock.advance

The loop never dies: every stage runs inside a guard that logs and swallows exceptions (and
times out), so a bug in one module costs one stage of one tick, not the run. Sibling modules
are imported lazily inside the stages so a module that is still being written cannot break
the engine import.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import inspect
import logging
import math
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from ..config import get_settings
from ..db import (
    ACTION_REQUESTS,
    APPROVALS,
    FEEDER_LOAD,
    MAP_SNAPSHOTS,
    RUN_METRICS,
    RUNS,
    db,
)
from ..ids import new_run_id
from ..models import Run
from ..timeutil import at, hhmm, sim_iso, wall_now
from . import clock
from .scenario import public_scenario, resolve_scenario
from .world_state import World

log = logging.getLogger("cooldown.engine")

MAP_EVERY_TICKS = 3
FORECAST_EVERY_TICKS = 3
STAGE_TIMEOUT_S = 45.0
PLAN_TIMEOUT_S = 180.0
APPROVAL_HOLD_MAX_S = 120.0  # pause_on_approval: wall-clock cap, then the clock moves and approvals can expire
APPROVAL_POLL_S = 0.5
MAX_EVENTS_LOGGED = 20  # world events written one by one per tick; the rest are folded into one entry
MAX_BG_TASKS = 64
REACT_COOLDOWN = timedelta(minutes=30)  # archetype reactions: at most once per event type per 30 sim minutes
LEDGER_TYPES = {"world_event", "contingency"}
WORLD_ACTOR = "sim:world"


def _mod(name: str) -> Any:
    """Import a sibling module lazily (it may still be under construction in another workstream)."""
    return importlib.import_module(f"app.{name}")


def _plain(obj: Any) -> Any:
    from ..api.common import jsonable

    return jsonable(obj)


def _r(x: Any, nd: int = 1) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return round(v, nd) if math.isfinite(v) else 0.0


class RunEngine:
    """One live run: the World, its tick loop task, and (governed mode) its Governance."""

    def __init__(self, run: dict[str, Any], world: World, http: httpx.AsyncClient | None = None, *,
                 headless: bool = False) -> None:
        self.run = run  # the runs doc; passed by reference to Governance (gate reads scale/budget)
        self.world = world
        self.headless = headless
        demo = (run.get("demo") or {})
        self.pause_on_planning: bool = bool(demo.get("pause_on_planning", False)) and world.mode == "governed"
        self.pause_on_approval: bool = bool(demo.get("pause_on_approval", False)) and world.mode == "governed"
        self.speed: float = float(run.get("speed") or 1.0)
        self.status: str = run.get("status", "created")
        self.paused_reason: str | None = None
        self.error: str | None = None
        self._http = http
        self._own_http = False
        self._user_paused = asyncio.Event()  # set while the user has paused the run
        self._stop = False
        self._task: asyncio.Task | None = None
        self._plan_task: asyncio.Task | None = None
        self._bg: set[asyncio.Task] = set()
        self._resumed_approvals: set[str] = set()
        self._reacted: dict[str, datetime] = {}
        self._errors_logged: set[tuple[str, str]] = set()
        self._last_map: dict[str, Any] | None = None
        self._last_tick_msg: dict[str, Any] | None = None
        self.gov: Any = None
        self.baseline_op: Any = None
        self.last_tick_wall_ms: float = 0.0

    # ------------------------------------------------------------ lifecycle

    @property
    def run_id(self) -> str:
        return self.world.run_id

    @property
    def mode(self) -> str:
        return self.world.mode

    @classmethod
    async def create(cls, scenario_id: str = "heatwave_2031", mode: str = "governed", seed: int | None = None, *,
                     http: httpx.AsyncClient | None = None, speed: float = 1.0,
                     pause_on_planning: bool | None = None, pause_on_approval: bool | None = None,
                     baseline_run_id: str | None = None, headless: bool = False, start: bool = True,
                     created_by: str = "human:operator", extra: dict[str, Any] | None = None) -> RunEngine:
        """Create the run doc, load the World, register it in app.runtime and start the tick loop."""
        from .. import runtime

        if mode not in ("governed", "baseline"):
            raise ValueError(f"mode must be governed or baseline, not {mode!r}")
        s = get_settings()
        seed = int(s.sim_seed if seed is None else seed)
        scenario = await resolve_scenario(scenario_id)
        demo = dict(scenario.get("demo") or {})
        if pause_on_planning is not None:
            demo["pause_on_planning"] = bool(pause_on_planning)
        if pause_on_approval is not None:
            demo["pause_on_approval"] = bool(pause_on_approval)
        if headless and pause_on_approval is None:
            demo["pause_on_approval"] = False  # nobody is there to approve; approvals expire in sim time

        run_id = new_run_id()
        sim_start = at(datetime(*_scenario_ymd(scenario), 12, tzinfo=UTC), str(scenario.get("sim_start", "12:00")))
        run = Run(id=run_id, scenario_id=str(scenario.get("id") or scenario_id), mode=mode, seed=seed,
                  status="created", sim_ts=sim_iso(sim_start), speed=float(speed), scale=1.0, households=0,
                  baseline_run_id=baseline_run_id, created_wall_ts=wall_now()).to_mongo()
        run.update({"tick": 0, "demo": demo, "created_by": created_by, "headless": headless,
                    "scenario": _plain(public_scenario(scenario)), **(extra or {})})
        await db()[RUNS].insert_one(run)

        try:
            loader = _mod("sim.loader")
            world: World = await loader.load_world(run_id, mode, scenario, seed)
        except Exception as exc:
            log.exception("load_world failed for %s", run_id)
            with contextlib.suppress(Exception):
                await db()[RUNS].update_one({"_id": run_id}, {"$set": {"status": "failed",
                                                                        "error": f"load_world: {exc}"}})
            raise RuntimeError(f"could not load the world: {exc}") from exc

        policy_version = None
        with contextlib.suppress(Exception):
            _, policy_version = await _mod("governance.identities").load_policies()
        run.update({"scale": float(world.scale), "households": int(world.n), "sim_ts": sim_iso(world.sim_now),
                    "sim_end": sim_iso(world.sim_end), "total_ticks": clock.total_ticks(world),
                    "policy_version": policy_version})
        await db()[RUNS].update_one({"_id": run_id}, {"$set": {
            k: run[k] for k in ("scale", "households", "sim_ts", "sim_end", "total_ticks", "policy_version")}})

        eng = cls(run, world, http, headless=headless)
        runtime.register(world, eng)
        await eng._setup()
        await eng._ledger("world_event", created_by,
                          f"Run {run_id} started by {created_by}: {mode} mode, scenario {run['scenario_id']}, "
                          f"seed {seed}, {world.n:,} simulated homes (x{world.scale:,.0f} real)",
                          {"kind": "run_started", "mode": mode, "seed": seed, "scale": world.scale,
                           "households": world.n, "policy_version": policy_version, "demo": demo})
        if start:
            eng.start()
        return eng

    async def _setup(self) -> None:
        """Governance (governed) or BaselineOperator (baseline). Failure is logged, never fatal."""
        if self.mode == "governed":
            try:
                Executor = _mod("executor.executor").Executor
                if self._http is None:
                    self._http = httpx.AsyncClient(base_url=get_settings().api_base_url, timeout=15.0)
                    self._own_http = True
                graph = _mod("agents.graph")
                checkpointer = None
                with contextlib.suppress(Exception):
                    checkpointer = graph.make_checkpointer()
                if checkpointer is None:
                    from langgraph.checkpoint.memory import InMemorySaver

                    log.warning("MongoDBSaver unavailable; LangGraph checkpoints kept in memory for %s", self.run_id)
                    checkpointer = InMemorySaver()
                self.gov = graph.Governance(self.world, self.run, Executor(self._http), checkpointer)
            except Exception:
                log.exception("Governance setup failed for %s; governed stages will be skipped", self.run_id)
        else:
            try:
                self.baseline_op = _mod("sim.baseline").BaselineOperator(self.world)
            except Exception:
                log.exception("BaselineOperator setup failed for %s", self.run_id)

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._loop(), name=f"run:{self.run_id}")

    async def stop(self) -> None:
        """Cancel the loop and background work (process shutdown)."""
        self._stop = True
        self._user_paused.clear()
        for t in [self._task, self._plan_task, *self._bg]:
            if t is not None and not t.done():
                t.cancel()
        for t in [self._task, self._plan_task, *list(self._bg)]:
            if t is not None:
                with contextlib.suppress(BaseException):
                    await t
        if self._own_http and self._http is not None:
            with contextlib.suppress(Exception):
                await self._http.aclose()

    async def wait(self, timeout: float | None = None) -> None:
        if self._task is not None:
            await asyncio.wait_for(asyncio.shield(self._task), timeout)

    @property
    def finished(self) -> bool:
        return self.status in ("finished", "failed")

    # ------------------------------------------------------------ controls (API)

    async def pause(self, by: str = "human:operator") -> None:
        self._user_paused.set()
        self.status = "paused"
        self.paused_reason = "user"
        await self._save_run({"status": "paused", "paused_by": by})
        self._publish_tick()

    async def resume(self, by: str = "human:operator") -> None:
        self._user_paused.clear()
        if not self.finished:
            self.status = "running"
        if self.paused_reason == "user":
            self.paused_reason = None
        await self._save_run({"status": self.status, "resumed_by": by})
        self._publish_tick()

    async def set_speed(self, x: float) -> float:
        self.speed = max(0.1, min(float(x), 50.0))
        await self._save_run({"speed": self.speed})
        self._publish_tick()
        return self.speed

    async def inject(self, event: str, by: str = "human:operator", **params: Any) -> dict[str, Any]:
        """POST /runs/{id}/inject: generator_trip | injection_message."""
        w = self.world
        if event == "generator_trip":
            grid = _mod("sim.grid")
            aux = _mod("sim.aux_state").get_aux(w)
            cons = w.scenario.get("contingencies") or []
            # Pull the next scheduled generator trip forward (so it does not fire twice), else add one.
            idx = next((i for i, c in enumerate(cons) if isinstance(c, dict) and i not in aux.fired_contingencies
                        and str(c.get("type") or "generator_trip") == "generator_trip"), None)
            mw = params.get("mw")
            label = None
            if idx is not None:
                aux.fired_contingencies.add(idx)
                mw = mw or cons[idx].get("mw")
                label = cons[idx].get("label")
            ev = grid.inject_contingency(w, float(mw) if mw else None, label)
            payload = {**(ev.get("payload") or {}), "injected_by": by, "scheduled_index": idx}
            await self._ledger("world_event", by, f"{by} injected a generator trip at {hhmm(w.sim_now)}",
                               {"kind": "inject", "event": event, "mw": payload.get("mw"), "label": payload.get("label")})
            await self._ledger("contingency", WORLD_ACTOR, str(ev.get("summary")), payload)
            if self.mode == "governed":
                with contextlib.suppress(Exception):
                    self._react(_mod("world_agents.archetype"), {**ev, "event": "contingency_news"})
            self._publish_tick()
            return {"event": event, "at": hhmm(w.sim_now), "mw": payload.get("mw"), "label": payload.get("label"),
                    "supply_mw": payload.get("supply_mw")}
        if event == "injection_message":
            resident = _mod("world_agents.resident")
            msg = await resident.inject_scripted(w, int(params.get("index") or 0))
            if msg is not None and self._redteam():
                self._spawn(self.gov.planning_cycle("untrusted_reply"), "planning:untrusted_reply")
            return {"event": event, "message_id": getattr(msg, "id", None)}
        raise ValueError(f"unknown inject event {event!r}; use generator_trip or injection_message")

    def _redteam(self) -> bool:
        """Governed run whose scenario has a red-team drill on a scripted message."""
        if self.mode != "governed" or self.gov is None:
            return False
        with contextlib.suppress(Exception):
            return bool(_mod("agents.planner").has_redteam(self.world))
        return False

    def submit_request(self, request_id: str) -> None:
        """Run a request through the request graph now (rollbacks), without waiting for the next tick."""
        if self.gov is not None:
            self._spawn(self.gov.process_request(request_id), f"request:{request_id}")

    def on_approval_update(self, approval: dict[str, Any]) -> None:
        """approvals change stream (or the decide endpoint): resume the request graph once."""
        status = approval.get("status")
        aid = approval.get("_id") or approval.get("id")
        if self.gov is None or status not in ("approved", "denied", "expired") or not aid:
            return
        if aid in self._resumed_approvals:
            return
        self._resumed_approvals.add(aid)
        self._spawn(self.gov.resume_request(approval), f"resume:{aid}")

    def on_control_update(self, doc: dict[str, Any]) -> None:
        """agent_control change stream (or the halt endpoint): flip the in-memory halted flag."""
        w = self.world
        w.halted = bool(doc.get("halted"))
        w.halted_scope = str(doc.get("scope") or "all")
        if w.halted and w.halted_scope in ("all", "agent:planner") and self._plan_task and not self._plan_task.done():
            self._plan_task.cancel()  # cancel the in-flight Planner call
        if not w.halted and self.gov is not None:  # resume: re-run requests parked by the kill switch
            self._spawn(self.gov.release_held(), "release_held")
        self._publish_tick()

    # ------------------------------------------------------------ loop

    async def _loop(self) -> None:
        s = get_settings()
        self.status = "running"
        await self._save_run({"status": "running", "started_wall_ts": wall_now()})
        try:
            while not self._stop:
                while self._user_paused.is_set() and not self._stop:
                    await asyncio.sleep(0.2)
                if self._stop:
                    break
                t0 = time.monotonic()
                try:
                    await self.step()
                except Exception:  # belt and braces: step() guards each stage already
                    log.exception("tick %s failed for %s", self.world.tick, self.run_id)
                if clock.is_finished(self.world):
                    await self._finish()
                    break
                period = 0.0 if self.headless else float(s.tick_real_seconds) / max(self.speed, 0.1)
                rest = period - (time.monotonic() - t0)
                await asyncio.sleep(rest if rest > 0 else 0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("run loop crashed for %s", self.run_id)
            self.status, self.error = "failed", str(exc)
            with contextlib.suppress(Exception):
                await self._save_run({"status": "failed", "error": str(exc)})

    async def run_ticks(self, n: int) -> None:
        """Headless: run up to n ticks synchronously (tests, scripts). Stops at sim_end."""
        if self.status == "created":
            self.status = "running"
            await self._save_run({"status": "running", "started_wall_ts": wall_now()})
        for _ in range(n):
            if clock.is_finished(self.world):
                if not self.finished:
                    await self._finish()
                return
            await self.step()
        if clock.is_finished(self.world) and not self.finished:
            await self._finish()

    async def step(self) -> None:
        """One tick (PLAN §7.2). Mutates the World, writes Atlas, broadcasts, advances the clock."""
        w = self.world
        t0 = time.monotonic()
        dt_h = clock.dt_hours()
        events: list[dict[str, Any]] = []
        verified: list[Any] = []

        await self._stage("world_step", self._world_step, events, dt_h)
        await self._stage("mock", self._mock, events)
        await self._stage("grid", self._grid)
        await self._stage("feeder_load", self._feeder_load)
        await self._stage("contingencies", self._contingencies, events)
        await self._stage("world_events", self._log_events, events)
        if w.tick % FORECAST_EVERY_TICKS == 0:
            if self.mode == "governed" and self.gov is not None:
                await self._stage("forecaster", self.gov.update_forecast)
            await self._stage("map", self._map_snapshot)
        if self.mode == "governed":
            if self.gov is not None:
                reasons = await self._stage("watcher", self.gov.watch) or []
                await self._stage("planning", self._planning, reasons, timeout=PLAN_TIMEOUT_S + 5)
                if not self._planning_in_background():
                    await self._stage("proposed", self.gov.process_proposed, timeout=PLAN_TIMEOUT_S)
                if not w.halted:
                    await self._stage("release_held", self.gov.release_held)
            verified = await self._stage("verifier", self._verify) or []
            if self.gov is not None:
                await self._stage("restorer", self.gov.restore_cycle)
            await self._stage("approvals_expiry", self._expire_approvals)
            await self._stage("world_agents", self._world_agents, events, verified)
        else:
            await self._stage("baseline", self._baseline)
        await self._stage("metrics", self._metrics, dt_h)
        self.last_tick_wall_ms = (time.monotonic() - t0) * 1000.0
        await self._stage("broadcast", self._broadcast)
        if self.pause_on_approval and self.mode == "governed":
            await self._stage("approval_hold", self._hold_for_approvals, timeout=APPROVAL_HOLD_MAX_S + 30)
        clock.advance(w)

    async def _stage(self, name: str, fn: Callable[..., Any], *args: Any, timeout: float = STAGE_TIMEOUT_S) -> Any:
        """Run one stage; log (once per stage and error type) and swallow any exception."""
        try:
            res = fn(*args)
            if inspect.isawaitable(res):
                res = await asyncio.wait_for(res, timeout)
            return res
        except asyncio.CancelledError:
            task = asyncio.current_task()
            if self._stop or (task is not None and task.cancelling()):
                raise  # the loop itself is being cancelled
            log.warning("stage %s cancelled at tick %s (%s)", name, self.world.tick, self.run_id)
            return None
        except Exception as exc:
            key = (name, type(exc).__name__)
            if key not in self._errors_logged:
                self._errors_logged.add(key)
                log.exception("stage %s failed at tick %s (%s)", name, self.world.tick, self.run_id)
            else:
                log.debug("stage %s failed again: %s", name, exc)
            return None

    # ------------------------------------------------------------ stages

    def _world_step(self, events: list[dict[str, Any]], dt_h: float) -> None:
        w = self.world
        out = _mod("sim.physics").world_step(w, dt_h) or []
        events.extend(out)
        if w.events:  # world events other modules queued for the engine
            events.extend(w.events)
            w.events.clear()

    async def _mock(self, events: list[dict[str, Any]]) -> None:
        ms = _mod("mock_systems")
        events.extend(ms.apply_pending(self.world) or [])
        await ms.persist(self.world)

    def _grid(self) -> None:
        _mod("sim.grid").aggregate(self.world)

    async def _feeder_load(self) -> None:
        w = self.world
        ts = w.sim_now.astimezone(UTC)
        docs = [{"ts": ts, "meta": {"run_id": w.run_id, "section_id": "ZONE"}, "tick": w.tick,
                 "demand_mw": _r(w.demand_mw, 2), "supply_mw": _r(w.supply_mw, 2),
                 "shortfall_mw": _r(w.shortfall_mw, 2)}]
        for k, sid in enumerate(w.section_ids):
            docs.append({"ts": ts, "meta": {"run_id": w.run_id, "section_id": sid}, "tick": w.tick,
                         "demand_mw": _r(w.section_demand_mw[k], 3) if k < len(w.section_demand_mw) else 0.0,
                         "cut": bool(w.section_cut[k])})
        await db()[FEEDER_LOAD].insert_many(docs, ordered=False)

    def _contingencies(self, events: list[dict[str, Any]]) -> None:
        grid = _mod("sim.grid")
        w = self.world
        fired = grid.check_contingencies(w) or []
        for c in fired:
            c.setdefault("type", "contingency")
        events.extend(fired)
        trip = grid.check_uncontrolled_trip(w)
        if trip:
            trip.setdefault("type", "world_event")
            trip.setdefault("payload", {}).setdefault("kind", "uncontrolled_trip")
            events.append(trip)
        released = grid.release_expired_cuts(w) or []
        if released:
            events.append({"type": "world_event", "summary": f"Power restored to {', '.join(released)} "
                                                             f"after an uncontrolled trip",
                           "payload": {"kind": "world_cut_released", "section_ids": list(released)}})

    async def _log_events(self, events: list[dict[str, Any]]) -> None:
        if not events:
            return
        head, rest = events[:MAX_EVENTS_LOGGED], events[MAX_EVENTS_LOGGED:]
        for ev in head:
            etype = ev.get("type") if ev.get("type") in LEDGER_TYPES else "world_event"
            summary = str(ev.get("summary") or etype.replace("_", " "))[:500]
            await self._ledger(etype, WORLD_ACTOR, summary, ev.get("payload") or {})
        if rest:
            await self._ledger("world_event", WORLD_ACTOR, f"{len(rest)} further world events this tick",
                               {"kind": "folded", "summaries": [str(e.get("summary"))[:160] for e in rest[:50]]})

    async def _map_snapshot(self) -> None:
        w = self.world
        enc = _mod("sim.metrics").encode_map(w)
        msg = {"type": "map", "sim_ts": sim_iso(w.sim_now), "tick": w.tick,
               "temps": enc.get("temps"), "flags": enc.get("flags")}
        self._last_map = msg
        self._publish(msg)
        await db()[MAP_SNAPSHOTS].insert_one({"run_id": w.run_id, "sim_ts": msg["sim_ts"], "tick": w.tick,
                                              "temps": msg["temps"], "flags": msg["flags"]})

    async def _planning(self, reasons: list[str]) -> None:
        if not reasons or self.gov is None or self._planning_in_background():
            return
        trigger = reasons[0]
        if self.pause_on_planning:
            # Freeze the clock while the Planner is in flight; kept in _plan_task so HALT can cancel it.
            self.paused_reason = "planning"
            self._publish_tick()
            self._plan_task = asyncio.create_task(self._safe(self.gov.planning_cycle(trigger), "planning"),
                                                  name=f"plan:{self.run_id}")
            try:
                done, _ = await asyncio.wait({self._plan_task}, timeout=PLAN_TIMEOUT_S)
                if not done:
                    log.warning("planning cycle (%s) timed out after %.0fs", trigger, PLAN_TIMEOUT_S)
                    self._plan_task.cancel()
            finally:
                if self.paused_reason == "planning":
                    self.paused_reason = None
        else:
            self._plan_task = asyncio.create_task(self._safe(self.gov.planning_cycle(trigger), "planning"),
                                                  name=f"plan:{self.run_id}")

    def _planning_in_background(self) -> bool:
        return self._plan_task is not None and not self._plan_task.done()

    async def _verify(self) -> list[Any]:
        return await _mod("executor.verifier").verify_pending(self.world) or []

    async def _expire_approvals(self) -> None:
        expired = await _mod("governance.approvals").expire_due(self.run_id, self.world.sim_now) or []
        for a in expired:  # the change stream resumes them too; on_approval_update de-duplicates
            self.on_approval_update(_plain(a))

    async def _baseline(self) -> None:
        if self.baseline_op is None:
            return
        out = self.baseline_op.step(self.world) or []
        if inspect.isawaitable(out):
            out = await out or []
        await self._log_events(list(out))

    def _metrics(self, dt_h: float) -> None:
        _mod("sim.metrics").update(self.world, dt_h)

    async def _broadcast(self) -> None:
        self._publish_tick()
        self._publish(self._sections_msg())
        await self._save_run({
            "sim_ts": sim_iso(self.world.sim_now), "tick": self.world.tick, "status": self.status,
            "demand_mw": _r(self.world.demand_mw), "supply_mw": _r(self.world.supply_mw),
            "shortfall_mw": _r(self.world.shortfall_mw), "halted": bool(self.world.halted),
        })

    async def _hold_for_approvals(self) -> None:
        """pause_on_approval: freeze the clock while an approval is pending (wall-clock capped)."""
        started = time.monotonic()
        announced = False
        while not self._stop:
            n = await db()[APPROVALS].count_documents({"run_id": self.run_id, "status": "pending"})
            if not n:
                break
            if time.monotonic() - started > APPROVAL_HOLD_MAX_S:
                log.info("approval hold released after %.0fs (%d pending)", APPROVAL_HOLD_MAX_S, n)
                break
            if not announced or self.paused_reason is None:  # re-assert after a user pause/resume
                self.paused_reason = "approval"
                self._publish_tick()
                announced = True
            await asyncio.sleep(APPROVAL_POLL_S)
        if self.paused_reason == "approval":
            self.paused_reason = None
            self._publish_tick()

    # ------------------------------------------------------------ world LLM agents

    async def _world_agents(self, events: list[dict[str, Any]], verified: list[Any]) -> None:
        """Featured residents and businesses answer delivered texts; archetypes react to events.

        Everything slow runs as background tasks (the LLM client has its own semaphore)."""
        w = self.world
        resident = _mod("world_agents.resident")
        business = _mod("world_agents.business")
        archetype = _mod("world_agents.archetype")

        tl = (w.mock or {}).get("textline") or {}
        delivered = tl.get("delivered") or []
        if delivered:
            items = list(delivered)
            delivered.clear()
            for item in items:
                to = str(item.get("to") or "")
                if to.startswith("biz_"):
                    req = await db()[ACTION_REQUESTS].find_one({"_id": item.get("request_id")}) or {}
                    self._spawn(business.on_request(w, {**req, **item, "to": to}), f"business:{to}")
                elif to.startswith("hh_"):
                    self._spawn(resident.on_message(w, item), f"resident:{to}")

        injected = await resident.check_scripted(w)  # the scripted injection message (scenario time)
        if injected and self._redteam():
            await self.gov.planning_cycle("untrusted_reply")  # red-team drill: the Planner reads it now

        if w.tick == 0:
            self._react(archetype, {"type": "heat_warning",
                                    "summary": "Heat warning: extreme heat this afternoon and evening"})
        for ev in events:
            payload = ev.get("payload") or {}
            if ev.get("type") == "contingency":
                self._react(archetype, {**ev, "event": "contingency_news"})
            elif payload.get("kind") == "uncontrolled_trip":
                self._react(archetype, {**ev, "event": "nearby_outage"})
                self._home_events(resident, payload.get("section_id") or payload.get("section_ids"), "nearby_outage")
        for exe in verified:
            status = getattr(exe, "status", None)
            if status not in ("verified", "partial"):
                continue
            req = await db()[ACTION_REQUESTS].find_one(
                {"_id": exe.request_id}, {"action_type": 1, "targets": 1, "summary": 1})
            if not req:
                continue
            atype = req.get("action_type")
            targets = req.get("targets") or {}
            if atype in ("precool", "thermostat_setback", "send_sms", "feeder_outage",
                         "thermostat_restore", "feeder_restore"):
                self._react(archetype, {"type": atype, "summary": req.get("summary"),
                                        "household_ids": targets.get("household_ids") or [],
                                        "section_id": targets.get("section_id")})
            if atype == "thermostat_setback":
                featured = set(w.featured.values())
                for hid in targets.get("household_ids") or []:
                    with contextlib.suppress(Exception):
                        if w.hh_index(hid) in featured:
                            self._spawn(resident.on_home_event(w, hid, "setback_applied", req.get("summary") or ""),
                                        f"home:{hid}")
            elif atype == "feeder_outage":
                self._home_events(resident, targets.get("section_id"), "nearby_outage")

    def _react(self, archetype: Any, event: dict[str, Any]) -> None:
        etype = archetype.event_type(event)
        last = self._reacted.get(etype)
        if last is not None and self.world.sim_now - last < REACT_COOLDOWN:
            return
        self._reacted[etype] = self.world.sim_now
        self._spawn(archetype.react(self.world, event), f"archetypes:{etype}")

    def _home_events(self, resident: Any, sections: Any, event: str) -> None:
        w = self.world
        sids = [sections] if isinstance(sections, str) else list(sections or [])
        for sid in sids:
            if sid not in w.section_ids:
                continue
            k = w.section_index(sid)
            for i in w.featured.values():
                if int(w.section_idx[i]) == k:
                    self._spawn(resident.on_home_event(w, w.hh_ids[i], event, f"Power cut in {sid}"),
                                f"home:{w.hh_ids[i]}")

    # ------------------------------------------------------------ finish

    async def _finish(self) -> None:
        w = self.world
        summary: dict[str, Any] = {}
        try:
            metrics = _mod("sim.metrics")
            if hasattr(metrics, "summary_with_db"):  # adds gate denials, approvals, delivered MW from Atlas
                summary = _plain(await metrics.summary_with_db(w)) or {}
            else:
                summary = _plain(metrics.summary(w)) or {}
        except Exception:
            log.exception("metrics summary failed for %s", self.run_id)
        self.status = "finished"
        self.paused_reason = None
        with contextlib.suppress(Exception):
            await db()[RUN_METRICS].replace_one(
                {"_id": self.run_id},
                {"_id": self.run_id, "run_id": self.run_id, "mode": self.mode, "seed": self.run.get("seed"),
                 "scenario_id": self.run.get("scenario_id"), "sim_ts": sim_iso(w.sim_now),
                 "wall_ts": wall_now(), "summary": summary},
                upsert=True)
        await self._save_run({"status": "finished", "finished_wall_ts": wall_now(), "sim_ts": sim_iso(w.sim_now),
                              "tick": w.tick, "metrics": summary})
        await self._ledger("world_event", WORLD_ACTOR, f"Run {self.run_id} finished at {hhmm(w.sim_now)}",
                           {"kind": "run_finished", "metrics": summary})
        self._publish({"type": "metrics", "summary": summary, "run_id": self.run_id})
        self._publish_tick()

    # ------------------------------------------------------------ helpers

    async def _ledger(self, type_: str, actor: str, summary: str, payload: dict[str, Any] | None = None,
                      refs: dict[str, Any] | None = None) -> Any:
        try:
            append = _mod("governance.ledger").append
            return await append(self.run_id, type_, actor, summary, sim_ts=sim_iso(self.world.sim_now),
                                payload=_plain(payload or {}), refs=refs)
        except Exception:
            key = ("ledger", type_)
            if key not in self._errors_logged:
                self._errors_logged.add(key)
                log.exception("ledger append failed (%s) for %s", type_, self.run_id)
            return None

    async def _save_run(self, fields: dict[str, Any]) -> None:
        self.run.update(fields)
        try:
            await db()[RUNS].update_one({"_id": self.run_id}, {"$set": _plain(fields)})
        except Exception as exc:
            log.debug("run update failed: %s", exc)

    def _spawn(self, coro: Awaitable[Any], name: str) -> None:
        if len(self._bg) >= MAX_BG_TASKS:
            log.debug("background task cap reached; dropping %s", name)
            if asyncio.iscoroutine(coro):
                coro.close()
            return
        t = asyncio.create_task(self._safe(coro, name), name=f"{self.run_id}:{name}")
        self._bg.add(t)
        t.add_done_callback(self._bg.discard)

    async def _safe(self, coro: Awaitable[Any], name: str) -> Any:
        try:
            return await coro
        except asyncio.CancelledError:
            log.info("%s cancelled (%s)", name, self.run_id)
            return None
        except Exception:
            log.exception("background task %s failed (%s)", name, self.run_id)
            return None

    def _publish(self, msg: dict[str, Any]) -> None:
        try:
            from ..api.ws import hub

            hub.publish(self.run_id, msg)
        except Exception:
            log.debug("ws publish failed", exc_info=True)

    def _outdoor_c(self) -> float | None:
        try:
            return _r(_mod("sim.weather").outdoor_c(self.world, self.world.sim_now), 1)
        except Exception:
            return None

    def tick_msg(self) -> dict[str, Any]:
        w = self.world
        return {"type": "tick", "run_id": w.run_id, "mode": w.mode, "sim_ts": sim_iso(w.sim_now), "tick": w.tick,
                "outdoor_c": self._outdoor_c(), "demand_mw": _r(w.demand_mw), "supply_mw": _r(w.supply_mw),
                "shortfall_mw": _r(w.shortfall_mw), "halted": bool(w.halted), "halted_scope": w.halted_scope,
                "status": self.status, "paused_reason": self.paused_reason, "speed": self.speed,
                "contingency_lost_mw": _r(w.contingency_lost_mw), "tick_ms": round(self.last_tick_wall_ms)}

    def _publish_tick(self) -> None:
        msg = self.tick_msg()
        self._last_tick_msg = msg
        self._publish(msg)

    def _sections_msg(self) -> dict[str, Any]:
        w = self.world
        try:
            states = _mod("sim.grid").section_states(w)
            for k, st in enumerate(states):
                st["cut_count_today"] = int(w.section_cut_count_today[k])
                st["demand_mw"] = _r(w.section_demand_mw[k], 2) if k < len(w.section_demand_mw) else 0.0
            return {"type": "sections", "states": states}
        except Exception:
            log.debug("grid.section_states unavailable", exc_info=True)
        states = []
        for k, sid in enumerate(w.section_ids):
            until = w.section_cut_until[k] if k < len(w.section_cut_until) else None
            states.append({"id": sid, "cut": bool(w.section_cut[k]),
                           "cut_minutes_today": int(w.section_cut_minutes_today[k]),
                           "cut_count_today": int(w.section_cut_count_today[k]),
                           "protected": bool(w.section_protected[k]),
                           "demand_mw": _r(w.section_demand_mw[k], 2) if k < len(w.section_demand_mw) else 0.0,
                           "cut_until": sim_iso(until) if until else None})
        return {"type": "sections", "states": states}

    def snapshot_messages(self) -> list[dict[str, Any]]:
        """What a late-joining WebSocket client needs right away: clock, sections, last map."""
        out = [self.tick_msg()]
        with contextlib.suppress(Exception):
            out.append(self._sections_msg())
        if self._last_map:
            out.append(self._last_map)
        return out

    def live_state(self) -> dict[str, Any]:
        """Live fields merged into GET /runs/{id}."""
        tick = self.tick_msg()
        tick.pop("type", None)
        tick.update({"pause_on_planning": self.pause_on_planning, "pause_on_approval": self.pause_on_approval,
                     "planning": bool(getattr(self.gov, "planning", False)) or self._planning_in_background(),
                     "background_tasks": len(self._bg), "error": self.error, "live": True})
        return tick


def _scenario_ymd(scenario: dict[str, Any]) -> tuple[int, int, int]:
    from .scenario import scenario_date

    d = scenario_date(scenario)
    return d.year, d.month, d.day

