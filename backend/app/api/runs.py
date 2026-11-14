"""Runs: create/control a run, read its ledger, approvals, requests, plans, metrics (PLAN §13)."""

from __future__ import annotations

import logging
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..db import (
    ACTION_REQUESTS,
    APPROVALS,
    BUSINESSES,
    DECISIONS,
    EXECUTIONS,
    FORECASTS,
    HOUSEHOLDS,
    LEDGER,
    MESSAGES,
    PLANS,
    RUN_METRICS,
    RUNS,
    db,
)
from .common import J, http_error, jsonable, live_engine, require_engine

log = logging.getLogger("cooldown.api.runs")

router = APIRouter(tags=["runs"])

COMPARE_KEYS = [
    ("uncontrolled_trips", "Uncontrolled trips", "lower"),
    ("outage_home_minutes", "Outage home-minutes", "lower"),
    ("outage_home_minutes_vulnerable", "Outage home-minutes (vulnerable)", "lower"),
    ("home_minutes_over_t_safe", "Home-minutes over T_safe", "lower"),
    ("home_minutes_over_t_safe_vulnerable", "Home-minutes over T_safe (vulnerable)", "lower"),
    ("quintile_outage_gap_min", "Income-quintile outage gap (min)", "lower"),
    ("peak_shortfall_mw", "Peak shortfall (MW)", "lower"),
    ("voluntary_mw_total", "Voluntary MW delivered", "higher"),
    ("rebound_peak_mw", "Rebound peak (MW)", "lower"),
]


class CreateRunBody(BaseModel):
    scenario_id: str = "heatwave_2031"
    mode: Literal["governed", "baseline"] = "governed"
    seed: int | None = None
    speed: float = 1.0
    by: str = "human:operator"
    pause_on_planning: bool | None = None
    pause_on_approval: bool | None = None
    with_baseline: bool = False  # also start a fast headless baseline run (same scenario + seed) for /compare
    defer_baseline: bool = False
    baseline_run_id: str | None = None


class SpeedBody(BaseModel):
    x: float = Field(gt=0)


class InjectBody(BaseModel):
    event: Literal["generator_trip", "intertie_loss", "injection_message"]
    by: str = "human:operator"
    mw: float | None = Field(default=None, gt=0, le=20000)
    index: int = Field(default=0, ge=0)


class ByBody(BaseModel):
    by: str = "human:operator"


# ---------------------------------------------------------------- runs

@router.post("/runs")
async def create_run(body: CreateRunBody, request: Request) -> Any:
    from ..sim.engine import RunEngine

    if not body.by.startswith("human:"):
        raise HTTPException(403, f"{body.by} may not start runs")
    http = getattr(request.app.state, "http", None)
    try:
        eng = await RunEngine.create(body.scenario_id, body.mode, body.seed, http=http, speed=body.speed,
                                     pause_on_planning=body.pause_on_planning,
                                     pause_on_approval=body.pause_on_approval,
                                     baseline_run_id=body.baseline_run_id, created_by=body.by)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except Exception as exc:
        log.exception("create run failed")
        raise HTTPException(503, f"could not start the run: {exc}") from exc
    out: dict[str, Any] = {"run_id": eng.run_id, "mode": eng.mode, "households": eng.world.n,
                           "scale": eng.world.scale, "sim_ts": eng.run.get("sim_ts")}
    if body.with_baseline and body.mode == "governed":
        async def start_baseline() -> str:
            base = await RunEngine.create(body.scenario_id, "baseline", eng.run.get("seed"), http=http,
                                          headless=True, start=False, created_by=body.by,
                                          extra={"companion_of": eng.run_id})
            await eng._save_run({"baseline_run_id": base.run_id})
            base.start()
            return base.run_id

        if body.defer_baseline:
            eng._spawn(start_baseline(), "comparison baseline")
        else:
            try:
                out["baseline_run_id"] = await start_baseline()
            except Exception:
                log.exception("companion baseline run failed to start")
    return J(out)


@router.get("/runs")
async def list_runs(limit: int = Query(20, ge=1, le=200), mode: str | None = None, include_tests: bool = False) -> Any:
    q: dict[str, Any] = {} if include_tests else {"test": {"$ne": True}}
    if mode:
        q["mode"] = mode
    docs = await db()[RUNS].find(q, {"scenario": 0, "metrics": 0}).sort("created_wall_ts", -1).limit(limit).to_list(None)
    for d in docs:
        if live_engine(d["_id"]) is not None:
            d["live"] = True
    return J(docs)


@router.get("/runs/{run_id}")
async def get_run(run_id: str) -> Any:
    doc = await db()[RUNS].find_one({"_id": run_id}, {
        "scenario.designer.prompt": 0, "scenario.scripted_messages": 0,
    })
    eng = live_engine(run_id)
    if doc is None and eng is None:
        raise HTTPException(404, f"run {run_id} not found")
    doc = dict(doc or eng.run)
    scenario = doc.pop("scenario", None) or {}
    doc["scenario_title"] = scenario.get("title")
    if eng is not None:
        doc.update(eng.live_state())
    else:
        doc["live"] = False
    return J(doc)


@router.post("/runs/{run_id}/pause")
async def pause_run(run_id: str, body: ByBody | None = None) -> Any:
    eng = require_engine(run_id)
    await eng.pause((body or ByBody()).by)
    return J(eng.live_state())


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, body: ByBody | None = None) -> Any:
    eng = require_engine(run_id)
    await eng.resume((body or ByBody()).by)
    return J(eng.live_state())


@router.post("/runs/{run_id}/speed")
async def set_speed(run_id: str, body: SpeedBody) -> Any:
    eng = require_engine(run_id)
    return J({"run_id": run_id, "speed": await eng.set_speed(body.x)})


@router.post("/runs/{run_id}/inject")
async def inject(run_id: str, body: InjectBody) -> Any:
    eng = require_engine(run_id)
    if not body.by.startswith("human:"):
        raise HTTPException(403, f"{body.by} may not inject events")
    if eng.finished:
        raise HTTPException(409, "This simulation has ended.")
    try:
        return J({"run_id": run_id, **await eng.inject(body.event, body.by, mw=body.mw, index=body.index)})
    except Exception as exc:
        raise http_error(exc) from exc


# ---------------------------------------------------------------- run-scoped reads

@router.get("/runs/{run_id}/ledger")
async def ledger(run_id: str, after_seq: int = 0, limit: int = Query(1000, ge=1, le=5000),
                 type: str | None = None) -> Any:
    q: dict[str, Any] = {"run_id": run_id, "seq": {"$gt": after_seq}}
    if type:
        q["type"] = {"$in": type.split(",")}
    entries = await db()[LEDGER].find(q, {"_id": 0}).sort("seq", 1).limit(limit).to_list(None)
    return J({"run_id": run_id, "entries": entries,
              "last_seq": entries[-1]["seq"] if entries else after_seq})


@router.get("/runs/{run_id}/approvals")
async def approvals(run_id: str, status: str | None = None) -> Any:
    q: dict[str, Any] = {"run_id": run_id}
    if status:
        q["status"] = {"$in": status.split(",")}
    docs = await db()[APPROVALS].find(q).sort("requested_sim_ts", 1).to_list(500)
    return J({"run_id": run_id, "approvals": docs})


@router.get("/runs/{run_id}/requests")
async def requests(run_id: str, status: str | None = None, limit: int = Query(300, ge=1, le=1000)) -> Any:
    """Action requests joined with their decision, approval and execution (for the console cards)."""
    q: dict[str, Any] = {"run_id": run_id}
    if status:
        q["status"] = {"$in": status.split(",")}
    reqs = await db()[ACTION_REQUESTS].find(q).sort("_id", -1).limit(limit).to_list(None)
    reqs.reverse()
    ids = [r["_id"] for r in reqs]
    decisions = {d["request_id"]: d async for d in db()[DECISIONS].find({"request_id": {"$in": ids}}).sort("sim_ts", 1)}
    apprs = {a["request_id"]: a async for a in db()[APPROVALS].find({"request_id": {"$in": ids}}).sort("requested_sim_ts", 1)}
    exes = {e["request_id"]: e async for e in db()[EXECUTIONS].find({"request_id": {"$in": ids}}, {"pre_state": 0}).sort("sent_tick", 1)}
    out = []
    for r in reqs:
        out.append(_card(r, decisions.get(r["_id"]), apprs.get(r["_id"]), exes.get(r["_id"])))
    return J({"run_id": run_id, "requests": out})


def _card(req: dict[str, Any], decision: dict | None, approval: dict | None, execution: dict | None) -> dict[str, Any]:
    """A request as the console renders it: big id lists trimmed to counts."""
    r = dict(req)
    targets = dict(r.get("targets") or {})
    hh = targets.get("household_ids") or []
    rc = targets.get("recipient_ids") or []
    r["targets_count"] = len(hh) or len(rc) or (1 if targets.get("section_id") or targets.get("business_id") else 0)
    if len(hh) > 50:
        targets["household_ids"] = hh[:50]
    if len(rc) > 50:
        targets["recipient_ids"] = rc[:50]
    r["targets"] = targets
    if len(r.get("messages") or []) > 5:
        r["messages_count"] = len(r["messages"])
        r["messages"] = r["messages"][:5]
    if decision is not None:
        d = dict(decision)
        allowed = d.pop("allowed_household_ids", None) or []
        d["allowed_household_count"] = len(allowed)
        r["decision"] = d
    if approval is not None:
        r["approval"] = approval
    if execution is not None:
        e = dict(execution)
        body = dict(e.get("body") or {})
        for k, v in list(body.items()):
            if isinstance(v, list) and len(v) > 20:
                body[k] = v[:20]
                body[f"{k}_count"] = len(v)
        e["body"] = body
        r["execution"] = e
    return r


async def request_card(request_id: str, *, run_id: str | None = None, execution_id: str | None = None) -> dict[str, Any] | None:
    """One joined request card (used by the action_requests change stream → WS)."""
    query = {"_id": request_id}
    if run_id is not None:
        query["run_id"] = run_id
    req = await db()[ACTION_REQUESTS].find_one(query)
    if req is None:
        return None
    dec = await db()[DECISIONS].find_one({"request_id": request_id}, sort=[("sim_ts", -1)])
    apr = await db()[APPROVALS].find_one({"request_id": request_id}, sort=[("requested_sim_ts", -1)])
    execution_query = {"request_id": request_id}
    if execution_id:
        execution_query["_id"] = execution_id
    exe = await db()[EXECUTIONS].find_one(execution_query, {"pre_state": 0}, sort=[("sent_tick", -1)])
    card = _card(req, dec, apr, exe)
    if run_id is None:
        return card
    targets = req.get("targets") or {}
    if targets.get("section_id"):
        card["target_sections"] = [targets["section_id"]]
    elif targets.get("business_id"):
        business = await db()[BUSINESSES].find_one({"_id": targets["business_id"]}, {"section_id": 1})
        if business and business.get("section_id"):
            card["target_sections"] = [business["section_id"]]
    elif targets.get("household_ids") or targets.get("recipient_ids"):
        house_ids = (targets.get("household_ids") or []) + (targets.get("recipient_ids") or [])
        card["target_sections"] = sorted(sid for sid in await db()[HOUSEHOLDS].distinct(
            "section_id", {"_id": {"$in": house_ids}}) if sid)
    return card


@router.get("/runs/{run_id}/requests/{request_id}")
async def get_request(run_id: str, request_id: str, execution_id: str | None = None) -> Any:
    card = await request_card(request_id, run_id=run_id, execution_id=execution_id)
    if card is None:
        raise HTTPException(404, f"request {request_id} not found in {run_id}")
    card["decision_path"] = await db()[LEDGER].find(
        {"run_id": run_id, "refs.request_id": request_id},
        {"_id": 0, "seq": 1, "type": 1, "sim_ts": 1, "payload.verdict": 1},
    ).sort("seq", 1).limit(100).to_list(None)
    return J(card)


@router.get("/runs/{run_id}/plans")
async def plans(run_id: str) -> Any:
    docs = await db()[PLANS].find({"run_id": run_id}).sort("version", 1).to_list(200)
    return J({"run_id": run_id, "plans": docs})


@router.get("/runs/{run_id}/forecast")
async def latest_forecast(run_id: str) -> Any:
    doc = await db()[FORECASTS].find_one({"run_id": run_id}, sort=[("sim_ts", -1)])
    if doc is None:
        raise HTTPException(404, f"no forecast yet for {run_id}")
    return J(doc)


@router.get("/runs/{run_id}/messages")
async def messages(run_id: str, thread_id: str | None = None, limit: int = Query(500, ge=1, le=2000)) -> Any:
    q: dict[str, Any] = {"run_id": run_id}
    if thread_id:
        q["thread_id"] = thread_id
    docs = await db()[MESSAGES].find(q).sort("sim_ts", 1).limit(limit).to_list(None)
    return J({"run_id": run_id, "messages": docs})


# ---------------------------------------------------------------- metrics + compare

async def run_summary(run_id: str) -> dict[str, Any] | None:
    """PLAN §7.9 summary: live from the World if the run is in this process, else run_metrics."""
    eng = live_engine(run_id)
    if eng is not None and not eng.finished:
        try:
            from ..sim import metrics

            if hasattr(metrics, "summary_with_db"):
                return jsonable(await metrics.summary_with_db(eng.world))
            return jsonable(metrics.summary(eng.world))
        except Exception as exc:
            log.warning("live metrics for %s failed: %s", run_id, exc)
    doc = await db()[RUN_METRICS].find_one({"_id": run_id}) or await db()[RUN_METRICS].find_one({"run_id": run_id})
    if doc is not None:
        return doc.get("summary") or {}
    run = await db()[RUNS].find_one({"_id": run_id}, {"metrics": 1})
    return (run or {}).get("metrics")


@router.get("/runs/{run_id}/metrics")
async def get_metrics(run_id: str) -> Any:
    summary = await run_summary(run_id)
    if summary is None:
        raise HTTPException(404, f"no metrics for {run_id}")
    eng = live_engine(run_id)
    return J({"run_id": run_id, "final": eng is None or eng.finished, **summary})


@router.get("/runs/{run_id}/compare")
async def compare(run_id: str, baseline_run_id: str | None = None) -> Any:
    run = await db()[RUNS].find_one({"_id": run_id}, {"scenario": 0, "metrics": 0})
    if run is None:
        raise HTTPException(404, f"run {run_id} not found")
    if run.get("mode") == "baseline" and not baseline_run_id:
        raise HTTPException(400, "compare a governed run against a baseline run")
    bid = baseline_run_id or run.get("baseline_run_id")
    if not bid:  # latest baseline run of the same scenario (prefer same seed, then finished)
        cands = await db()[RUNS].find(
            {"mode": "baseline", "scenario_id": run.get("scenario_id"), "test": {"$ne": True}},
            {"_id": 1, "seed": 1, "status": 1, "created_wall_ts": 1},
        ).sort("created_wall_ts", -1).limit(50).to_list(None)
        cands.sort(key=lambda c: (c.get("seed") != run.get("seed"), c.get("status") != "finished"))
        bid = cands[0]["_id"] if cands else None
    governed = await run_summary(run_id) or {}
    baseline = await run_summary(bid) if bid else None
    rows = []
    for key, label, better in COMPARE_KEYS:
        g = _metric(governed, key)
        b = _metric(baseline or {}, key)
        rows.append({"key": key, "label": label, "governed": g, "baseline": b, "better": better})
    return J({"run_id": run_id, "baseline_run_id": bid, "governed": governed, "baseline": baseline,
              "rows": rows, "final": (live_engine(run_id) is None or live_engine(run_id).finished)})


def _metric(summary: dict[str, Any], key: str) -> Any:
    """Look a metric up flat or one level down (summary layouts vary: {"trips": {...}} etc.)."""
    if not summary:
        return None
    if key in summary:
        v = summary[key]
        return v.get("all", v.get("total", v)) if isinstance(v, dict) else v
    for v in summary.values():
        if isinstance(v, dict) and key in v:
            return v[key]
    return None
