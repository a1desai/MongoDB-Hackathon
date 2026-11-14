"""LangGraph orchestration of the governed pipeline (CONTRACTS decision 1, PLAN §8.2 / §10).

Two compiled StateGraphs, both checkpointed in Atlas by MongoDBSaver
(collections langgraph_checkpoints / langgraph_checkpoint_writes):

  plan graph     gather_context -> planner -> targeter -> END
                 (Governance.planning_cycle then fans each proposed request out to the request graph)

  request graph  thread_id = request_id
                 gate --deny--------------------------------------------------> END
                      --allow / partial_allow--> execute --sent--------------> END
                      --needs_approval--> request_approval -> await_approval (interrupt())
                                            --approved--> execute
                                            --denied / expired--> close ----> END
                 execute --refused by the kill switch (R15)--> hold (interrupt()) -> execute

Graph state holds only ids and plain JSON (never the World), so checkpoints stay small and
serialisable. Every invoke gets a fresh LangSmith root run id (config["run_id"]) plus run/agent
metadata, and that id is stamped on the plan / requests where they have none.
"""

from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta
from functools import lru_cache
from typing import Any, Protocol, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..config import get_settings
from ..db import (
    ACTION_REQUESTS,
    APPROVALS,
    DECISIONS,
    FORECASTS,
    LANGGRAPH_CHECKPOINTS,
    LANGGRAPH_WRITES,
    PLANS,
    db,
)
from ..models import ActionRequest, Approval, Decision, Forecast, Plan
from ..sim.world_state import World
from ..timeutil import sim_iso
from . import forecaster, planner, restorer, targeter, watcher
from .common import is_halted

log = logging.getLogger("cooldown.agents.graph")

REPLAN_MIN_GAP = timedelta(minutes=15)  # PLAN §8.2: at most once per 15 sim minutes ...
REPLAN_EXEMPT = {"contingency", "trip_risk", "rotation_due", "untrusted_reply"}  # ... except right after a contingency (and while a trip looms)
# Triggers that mean the last forecast is stale (it was made before supply dropped / the gap grew):
# re-run the Forecaster before planning so the plan is sized for the real gap.
REFRESH_FORECAST = {"contingency", "trip_risk", "new_shortfall", "rotation_due", "untrusted_reply"}
URGENT_WAIT_S = 60.0  # how long an urgent replan waits for a plan already in flight
FORECAST_EVERY_TICKS = 3
APPROVAL_WAIT_S = 2.0  # resume_request: how long to wait for a graph to reach its interrupt()
# Checkpoint only when a run parks in interrupt() or exits (incl. errors): ~2 Atlas round trips per
# request instead of one per node. Nodes are idempotent, so a crash simply re-runs the last node.
DURABILITY = "exit"


# ---------------------------------------------------------------- LangSmith + checkpointer

def tracing_enabled() -> bool:
    """LangGraph traces natively when LANGSMITH_* is in os.environ; .env alone is not exported."""
    s = get_settings()
    if not (s.langsmith_tracing and s.langsmith_api_key):
        return False
    os.environ.setdefault("LANGSMITH_API_KEY", s.langsmith_api_key)
    os.environ.setdefault("LANGSMITH_PROJECT", s.langsmith_project)
    os.environ.setdefault("LANGSMITH_TRACING", "true")
    return True


@lru_cache
def make_checkpointer() -> Any:
    """MongoDBSaver on a sync pymongo.MongoClient (the one allowed exception, CONTRACTS decision 1).

    LangGraph calls its async a* methods, which run the sync driver in an executor.
    """
    from langgraph.checkpoint.mongodb import MongoDBSaver
    from pymongo import MongoClient

    s = get_settings()
    client: MongoClient = MongoClient(s.mongodb_uri, appname="cooldown-langgraph", tz_aware=True,
                                      serverSelectionTimeoutMS=15000)
    return MongoDBSaver(client, db_name=s.mongodb_db, checkpoint_collection_name=LANGGRAPH_CHECKPOINTS,
                        writes_collection_name=LANGGRAPH_WRITES)


# ---------------------------------------------------------------- graph state

class PlanState(TypedDict, total=False):
    run_id: str
    trigger: str
    forecast_id: str | None
    context: dict[str, Any]
    plan_id: str
    plan_version: int
    request_ids: list[str]
    langsmith_run_id: str | None


class RequestState(TypedDict, total=False):
    run_id: str
    request_id: str
    verdict: str
    decision_id: str
    approval_id: str
    approval_status: str  # approved | denied | expired
    approved_by: str | None
    execution_id: str
    held: bool  # refused by the kill switch (or scheduled for later), waiting in `hold`
    scheduled_for: str | None  # feeder_outage: sim time the breaker may open (after the R12 notice)
    status: str  # request status after the last node
    langsmith_run_id: str | None


class RequestSteps(Protocol):
    """The side-effecting steps of the request graph. Governance implements them against
    Atlas and the other workstreams' modules; tests pass in-memory fakes."""

    async def gate(self, state: RequestState) -> dict[str, Any]: ...  # {verdict, decision_id, status}
    async def request_approval(self, state: RequestState) -> dict[str, Any]: ...  # {approval_id, status}
    async def approval_status(self, state: RequestState) -> str | None: ...  # decided status, or None if pending
    async def execute(self, state: RequestState) -> dict[str, Any]: ...  # {execution_id, held, status}
    async def close(self, state: RequestState) -> dict[str, Any]: ...  # approval denied/expired -> {status}


class PlanSteps(Protocol):
    async def gather_context(self, state: PlanState) -> dict[str, Any]: ...
    async def planner(self, state: PlanState) -> dict[str, Any]: ...
    async def targeter(self, state: PlanState) -> dict[str, Any]: ...


# ---------------------------------------------------------------- graph builders

def route_verdict(state: RequestState) -> str:
    v = state.get("verdict")
    if v in ("allow", "partial_allow"):
        return "execute"
    if v == "needs_approval":
        return "request_approval"
    return END  # deny (or no decision)


def route_approval(state: RequestState) -> str:
    return "execute" if state.get("approval_status") == "approved" else "close"


def route_execution(state: RequestState) -> str:
    return "hold" if state.get("held") else END


def build_request_graph(steps: RequestSteps, checkpointer: Any) -> Any:
    """gate -> [needs_approval: request_approval -> interrupt()] -> execute (-> hold on R15)."""

    async def await_approval(state: RequestState) -> dict[str, Any]:
        # A decision that landed before we got here (e.g. auto-expiry) needs no interrupt.
        decided = await steps.approval_status(state)
        if decided and decided != "pending":
            return {"approval_status": decided}
        answer = interrupt({"kind": "approval", "request_id": state.get("request_id"),
                            "approval_id": state.get("approval_id")})
        if isinstance(answer, dict):
            return {"approval_status": str(answer.get("status", "denied")), "approved_by": answer.get("by")}
        return {"approval_status": str(answer)}

    def hold(state: RequestState) -> dict[str, Any]:
        kind = "scheduled" if state.get("scheduled_for") else "halted"
        interrupt({"kind": kind, "request_id": state.get("request_id"), "scheduled_for": state.get("scheduled_for")})
        return {"held": False}

    g = StateGraph(RequestState)
    g.add_node("gate", steps.gate)
    g.add_node("request_approval", steps.request_approval)
    g.add_node("await_approval", await_approval)
    g.add_node("execute", steps.execute)
    g.add_node("hold", hold)
    g.add_node("close", steps.close)
    g.add_edge(START, "gate")
    g.add_conditional_edges("gate", route_verdict, ["execute", "request_approval", END])
    g.add_edge("request_approval", "await_approval")
    g.add_conditional_edges("await_approval", route_approval, ["execute", "close"])
    g.add_conditional_edges("execute", route_execution, ["hold", END])
    g.add_edge("hold", "execute")
    g.add_edge("close", END)
    return g.compile(checkpointer=checkpointer, name="request_graph")


def build_plan_graph(steps: PlanSteps, checkpointer: Any) -> Any:
    g = StateGraph(PlanState)
    g.add_node("gather_context", steps.gather_context)
    g.add_node("planner", steps.planner)
    g.add_node("targeter", steps.targeter)
    g.add_edge(START, "gather_context")
    g.add_edge("gather_context", "planner")
    g.add_edge("planner", "targeter")
    g.add_edge("targeter", END)
    return g.compile(checkpointer=checkpointer, name="plan_graph")


def graph_config(thread_id: str, name: str, metadata: dict[str, Any], tags: list[str]) -> tuple[dict, str | None]:
    """RunnableConfig with a fresh LangSmith root run id; returns (config, id if tracing is on)."""
    ls_id = uuid.uuid4()
    cfg = {
        "configurable": {"thread_id": thread_id},
        "run_id": ls_id,
        "run_name": name,
        "tags": tags,
        "metadata": {k: v for k, v in metadata.items() if v is not None},
        "recursion_limit": 25,
    }
    return cfg, (str(ls_id) if tracing_enabled() else None)


def waiting_on(snapshot: Any) -> str | None:
    """'approval' / 'halted' when the thread is parked in an interrupt(), else None."""
    nxt = tuple(getattr(snapshot, "next", ()) or ())
    if "await_approval" in nxt:
        return "approval"
    if "hold" in nxt:
        return "halted"
    return None


# ---------------------------------------------------------------- Governance

class Governance:
    """Drives the governed agents for one run through the two LangGraph graphs."""

    def __init__(self, world: World, run: dict, executor: Any, checkpointer: Any | None = None) -> None:
        self.world = world
        self.run = run
        self.executor = executor
        self.checkpointer = checkpointer if checkpointer is not None else make_checkpointer()
        self.request_graph = build_request_graph(_RealRequestSteps(self), self.checkpointer)
        self.plan_graph = build_plan_graph(_RealPlanSteps(self), self.checkpointer)
        self.forecast: Forecast | None = None
        self.plan: Plan | None = None
        self.planning = False  # engine: pause_on_planning freezes the clock while True
        self.last_plan_at: datetime | None = None
        self._plan_seq = 0
        self._locks: dict[str, asyncio.Lock] = {}
        self._held: set[str] = set()
        self._scheduled: dict[str, datetime] = {}  # request id -> sim time it may execute (outage start)
        self._done: set[str] = set()  # request ids whose graph reached END in this process
        tracing_enabled()

    @property
    def run_id(self) -> str:
        return self.world.run_id

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = self._locks[key] = asyncio.Lock()
        return lock

    def _meta(self, agent_id: str, **extra: Any) -> dict[str, Any]:
        return {"run_id": self.run_id, "sim_ts": sim_iso(self.world.sim_now), "tick": self.world.tick,
                "agent_id": agent_id, "acting_for": "human:supervisor", **extra}

    # ------------------------------------------------------------ agents outside the graphs

    async def update_forecast(self) -> Forecast | None:
        """Forecaster (every 3 ticks)."""
        try:
            self.forecast = await forecaster.update(self.world)
        except Exception:  # noqa: BLE001 - the sim must never crash on an agent
            log.exception("forecaster failed")
        return self.forecast

    async def watch(self) -> list[str]:
        """Watcher (every tick): replan reasons; also expires approvals."""
        try:
            return await watcher.check(self.world, {"plan": self.plan, "forecast": self.forecast})
        except Exception:  # noqa: BLE001
            log.exception("watcher failed")
            return []

    async def restore_cycle(self) -> list[str]:
        """Restorer proposals due now, each sent through the request graph."""
        if is_halted(self.world, restorer.AGENT_ID):
            return []
        try:
            proposals = await restorer.propose(self.world)
        except Exception:  # noqa: BLE001
            log.exception("restorer failed")
            return []
        return [await self.process_request(r.id) for r in proposals]

    async def on_tick(self) -> dict[str, Any]:
        """Optional one-call tick hook: forecast (every 3 ticks) -> watcher -> replan -> restorer ->
        any other proposed requests (e.g. rollbacks) -> release held requests after a resume."""
        out: dict[str, Any] = {"plan": None, "reasons": []}
        if self.world.tick % FORECAST_EVERY_TICKS == 0 or self.forecast is None:
            await self.update_forecast()
        out["reasons"] = await self.watch()
        if out["reasons"]:
            out["plan"] = await self.planning_cycle(out["reasons"][0])
        await self.restore_cycle()
        await self.process_proposed()
        if not self.world.halted:
            await self.release_held()
        return out

    # ------------------------------------------------------------ plan graph

    async def planning_cycle(self, trigger: str) -> Plan | None:
        """plan graph: context -> planner -> targeter, then fan out process_request (in order, so
        cool-space pre-notifications are executed before the outage they precede is gated)."""
        now = self.world.sim_now
        if is_halted(self.world, planner.AGENT_ID):
            log.info("planning skipped: planner halted")
            return None
        if (trigger not in REPLAN_EXEMPT and self.last_plan_at is not None
                and now - self.last_plan_at < REPLAN_MIN_GAP):
            log.info("planning skipped (%s): last plan %s", trigger, self.last_plan_at)
            return None
        if self.planning:
            if trigger not in REPLAN_EXEMPT:
                return None
            # An urgent trigger (contingency, trip risk, ...) must not be lost to a plan already in
            # flight (e.g. one started by an injected message): wait for it, then plan again.
            for _ in range(int(URGENT_WAIT_S / 0.05)):
                await asyncio.sleep(0.05)
                if not self.planning:
                    break
            else:
                log.warning("planning (%s) dropped: another plan is still in flight", trigger)
                return None
            now = self.world.sim_now
        self.planning = True
        self.last_plan_at = now
        self._plan_seq += 1
        if trigger in REFRESH_FORECAST:
            await self.update_forecast()
        thread = f"plan:{self.run_id}:{self._plan_seq:03d}:t{self.world.tick}"
        cfg, ls_id = graph_config(thread, "plan_graph", self._meta(planner.AGENT_ID, trigger=trigger),
                                  ["governed", "plan", self.run_id])
        try:
            final = await self.plan_graph.ainvoke(
                {"run_id": self.run_id, "trigger": trigger, "langsmith_run_id": ls_id}, cfg, durability=DURABILITY)
        except Exception:  # noqa: BLE001 - a failed planning cycle must not stall the sim
            log.exception("plan graph failed (%s)", trigger)
            return None
        finally:
            self.planning = False
        plan_doc = await db()[PLANS].find_one({"_id": final.get("plan_id")})
        if plan_doc:
            self.plan = Plan.model_validate(plan_doc)
        for rid in final.get("request_ids") or []:
            await self.process_request(rid)
        return self.plan

    async def _latest_forecast(self) -> Forecast | None:
        if self.forecast is not None:
            return self.forecast
        doc = await db()[FORECASTS].find_one({"run_id": self.run_id}, sort=[("sim_ts", -1)])
        return Forecast.model_validate(doc) if doc else None

    # ------------------------------------------------------------ request graph

    async def process_request(self, request_id: str) -> str:
        """Request graph (thread_id = request_id); returns the request's status afterwards.

        A thread that already ran is not restarted: a parked one is resumed only by
        resume_request / release_held, and a finished one just reports its status.
        """
        async with self._lock(request_id):
            cfg, ls_id = graph_config(request_id, "request_graph",
                                      self._meta("system:gate", request_id=request_id),
                                      ["governed", "request", self.run_id])
            try:
                snap = await self.request_graph.aget_state(cfg)
                if snap.values:
                    parked = waiting_on(snap)
                    if parked == "halted":
                        self._held.add(request_id)
                    if parked or not snap.next:
                        return await self._status(request_id, snap.values)
                    final = await self.request_graph.ainvoke(None, cfg, durability=DURABILITY)  # retry a node that crashed
                else:
                    final = await self.request_graph.ainvoke(
                        {"run_id": self.run_id, "request_id": request_id, "langsmith_run_id": ls_id}, cfg, durability=DURABILITY)
            except Exception:  # noqa: BLE001 - one bad request must not stall the pipeline
                log.exception("request graph failed for %s", request_id)
                return await self._status(request_id, {})
            return await self._after(request_id, cfg, final)

    async def resume_request(self, approval: Approval | dict) -> str:
        """Approval decided (change stream): Command(resume={"status", "by"}) on its request thread."""
        apr = approval if isinstance(approval, Approval) else Approval.model_validate(approval)
        rid = apr.request_id
        if apr.status == "pending":
            return await self._status(rid, {})
        async with self._lock(rid):
            cfg, ls_id = graph_config(rid, "request_graph.resume",
                                      self._meta("human:supervisor", request_id=rid, approval_id=apr.id,
                                                 approval_status=apr.status),
                                      ["governed", "request", "approval", self.run_id])
            try:
                snap = await self.request_graph.aget_state(cfg)
                waited = 0.0
                while waiting_on(snap) != "approval" and snap.next and waited < APPROVAL_WAIT_S:
                    await asyncio.sleep(0.1)  # graph still on its way to interrupt()
                    waited += 0.1
                    snap = await self.request_graph.aget_state(cfg)
                if waiting_on(snap) != "approval":
                    return await self._status(rid, snap.values)
                final = await self.request_graph.ainvoke(
                    Command(resume={"status": apr.status, "by": apr.decided_by, "approval_id": apr.id}), cfg, durability=DURABILITY)
            except Exception:  # noqa: BLE001
                log.exception("resume failed for %s", rid)
                return await self._status(rid, {})
            return await self._after(rid, cfg, final)

    async def release_held(self) -> list[str]:
        """After a kill-switch resume: re-run execute for requests parked in hold."""
        if self.world.halted:
            return []
        out = []
        for rid in sorted(self._held):
            due = self._scheduled.get(rid)
            if due is not None and due > self.world.sim_now:
                continue  # an approved outage waiting for its start time
            async with self._lock(rid):
                cfg, _ = graph_config(rid, "request_graph.release", self._meta("system:gate", request_id=rid),
                                      ["governed", "request", "resume", self.run_id])
                try:
                    snap = await self.request_graph.aget_state(cfg)
                    if waiting_on(snap) != "halted":
                        self._held.discard(rid)
                        continue
                    final = await self.request_graph.ainvoke(Command(resume={"status": "resumed"}), cfg, durability=DURABILITY)
                except Exception:  # noqa: BLE001
                    log.exception("release failed for %s", rid)
                    continue
                out.append(await self._after(rid, cfg, final))
        return out

    async def process_proposed(self) -> list[str]:
        """Every request still 'proposed' with no graph run yet (restores, rollbacks, ...)."""
        cur = db()[ACTION_REQUESTS].find({"run_id": self.run_id, "status": "proposed"}, {"_id": 1}).sort("_id", 1)
        ids = [d["_id"] async for d in cur if d["_id"] not in self._done]
        return [await self.process_request(rid) for rid in ids]

    async def _after(self, request_id: str, cfg: dict, final: Any) -> str:
        snap = await self.request_graph.aget_state(cfg)
        parked = waiting_on(snap)
        if parked == "halted":
            self._held.add(request_id)
        else:
            self._held.discard(request_id)
        if not parked and not snap.next:
            self._done.add(request_id)
        return await self._status(request_id, final if isinstance(final, dict) else snap.values)

    async def _status(self, request_id: str, values: dict) -> str:
        try:
            doc = await db()[ACTION_REQUESTS].find_one({"_id": request_id}, {"status": 1})
            if doc and doc.get("status"):
                return str(doc["status"])
        except Exception as exc:  # noqa: BLE001
            log.debug("status lookup failed: %s", exc)
        return str((values or {}).get("status") or "proposed")

    # ------------------------------------------------------------ loaders

    async def load_request(self, request_id: str) -> ActionRequest:
        doc = await db()[ACTION_REQUESTS].find_one({"_id": request_id})
        if doc is None:
            raise KeyError(f"action_request {request_id} not found")
        return ActionRequest.model_validate(doc)

    async def load_decision(self, decision_id: str | None, request_id: str) -> Decision | None:
        q = {"_id": decision_id} if decision_id else {"request_id": request_id}
        doc = await db()[DECISIONS].find_one(q, sort=[("sim_ts", -1)])
        return Decision.model_validate(doc) if doc else None


SWITCHING = timedelta(minutes=5)  # GridOps: switching takes 1 tick


def execute_after(req: ActionRequest, world: World) -> datetime | None:
    """For a feeder_outage whose start is still ahead: the sim time to call GridOps (start - 1 tick)."""
    if req.action_type != "feeder_outage":
        return None
    raw = (req.params or {}).get("start")
    if not raw:
        return None
    try:
        from ..timeutil import parse_sim

        due = parse_sim(str(raw)) - SWITCHING
    except Exception:  # noqa: BLE001
        return None
    return due if due > world.sim_now else None


# ---------------------------------------------------------------- real steps

class _RealPlanSteps:
    def __init__(self, gov: Governance) -> None:
        self.gov = gov

    async def gather_context(self, state: PlanState) -> dict[str, Any]:
        """Forecast, resources computed in code, similar_events ($vectorSearch), latest 10 untrusted replies."""
        fc = await self.gov._latest_forecast()
        ctx = await planner.build_context(self.gov.world, fc, state.get("trigger", ""))
        return {"context": ctx, "forecast_id": fc.id if fc else None}

    async def planner(self, state: PlanState) -> dict[str, Any]:
        fc = await self.gov._latest_forecast()
        plan = await planner.plan_from_context(self.gov.world, fc, state.get("context") or {},
                                               state.get("trigger", ""),
                                               langsmith_run_id=state.get("langsmith_run_id"))
        return {"plan_id": plan.id, "plan_version": plan.version}

    async def targeter(self, state: PlanState) -> dict[str, Any]:
        doc = await db()[PLANS].find_one({"_id": state.get("plan_id")})
        if doc is None:
            return {"request_ids": []}
        plan = Plan.model_validate(doc)
        if is_halted(self.gov.world, targeter.AGENT_ID):
            return {"request_ids": []}
        reqs = await targeter.to_requests(plan, self.gov.world, langsmith_run_id=state.get("langsmith_run_id"))
        return {"request_ids": [r.id for r in reqs]}


class _RealRequestSteps:
    def __init__(self, gov: Governance) -> None:
        self.gov = gov

    async def gate(self, state: RequestState) -> dict[str, Any]:
        """app.governance.gate.decide: stores the decision, updates status, logs `decision`.
        Idempotent on replay: an existing decision is reused, never re-decided."""
        rid = state["request_id"]
        req = await self.gov.load_request(rid)
        if not req.langsmith_run_id and state.get("langsmith_run_id"):
            await db()[ACTION_REQUESTS].update_one({"_id": rid, "langsmith_run_id": None},
                                                   {"$set": {"langsmith_run_id": state["langsmith_run_id"]}})
            req.langsmith_run_id = state["langsmith_run_id"]
        dec = await self.gov.load_decision(req.decision_id, rid) if req.decision_id else None
        if dec is None:
            from ..governance.gate import decide

            dec = await decide(req, self.gov.world, self.gov.run)
            if dec.id and not req.decision_id:
                await db()[ACTION_REQUESTS].update_one({"_id": rid, "decision_id": None},
                                                       {"$set": {"decision_id": dec.id}})
        return {"verdict": dec.verdict, "decision_id": dec.id, "status": await self.gov._status(rid, {})}

    async def request_approval(self, state: RequestState) -> dict[str, Any]:
        """app.governance.approvals.request_approval (10-minute expiry, ledger approval_requested)."""
        rid = state["request_id"]
        req = await self.gov.load_request(rid)
        if req.approval_id:  # replayed node
            return {"approval_id": req.approval_id, "status": req.status}
        dec = await self.gov.load_decision(state.get("decision_id"), rid)
        if dec is None:
            raise RuntimeError(f"no decision for {rid}")
        from ..governance.approvals import request_approval

        apr = await request_approval(req, dec, self.gov.world)
        await db()[ACTION_REQUESTS].update_one({"_id": rid}, {"$set": {"approval_id": apr.id}})
        return {"approval_id": apr.id, "status": await self.gov._status(rid, {})}

    async def approval_status(self, state: RequestState) -> str | None:
        apr_id = state.get("approval_id")
        if not apr_id:
            return None
        doc = await db()[APPROVALS].find_one({"_id": apr_id}, {"status": 1})
        return (doc or {}).get("status")

    async def execute(self, state: RequestState) -> dict[str, Any]:
        """Executor.execute; a kill-switch refusal (R15) parks the thread in `hold`."""
        rid = state["request_id"]
        req = await self.gov.load_request(rid)
        due = execute_after(req, self.gov.world)
        if due is not None:
            # GridOps opens a breaker as soon as it is called, so an approved outage waits here until
            # one tick (the switching time) before its start: the cut lands when the R12 notice says.
            self.gov._scheduled[rid] = due
            await db()[ACTION_REQUESTS].update_one({"_id": rid}, {"$set": {"scheduled_exec_sim_ts": sim_iso(due)}})
            return {"held": True, "scheduled_for": sim_iso(due), "status": await self.gov._status(rid, {})}
        self.gov._scheduled.pop(rid, None)
        dec = await self.gov.load_decision(req.decision_id or state.get("decision_id"), rid)
        if dec is None:
            raise RuntimeError(f"no decision for {rid}")
        exe = await self.gov.executor.execute(req, dec, self.gov.world)
        resp = exe.response or {}
        held = bool(resp.get("refused")) and resp.get("rule_id") == "R15"
        return {"execution_id": exe.id, "held": held, "scheduled_for": None, "status": await self.gov._status(rid, {})}

    async def close(self, state: RequestState) -> dict[str, Any]:
        """Approval denied or expired: the request ends denied (the approvals module logs the decision)."""
        rid = state["request_id"]
        await db()[ACTION_REQUESTS].update_one(
            {"_id": rid, "status": {"$in": ["proposed", "needs_approval"]}},
            {"$set": {"status": "denied", "sim_ts": sim_iso(self.gov.world.sim_now),
                      "approval_outcome": state.get("approval_status")}},
        )
        return {"status": await self.gov._status(rid, {})}
