"""Business agents (PLAN §11.3): six fictional businesses answer curtailment requests.

One FAST-model call per request -> {accept, mw_offered, start, end, conditions, reply_text},
clamped by code (max MW, notice period, protected sites). The reply is stored as an untrusted
message and logged as ledger `message_in`; an acceptance records committed capacity on
world.businesses[id] (status "accepted", accepted_mw, active_from/active_until).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from pydantic import BaseModel, Field

from ..config import get_settings
from ..ids import new_id
from ..llm.client import complete_json, load_prompt
from ..models import ActionRequest, Message
from ..sim.world_state import World
from ..timeutil import at, hhmm
from . import _common as common

log = logging.getLogger(__name__)

REPLY_MAX = 240


class BusinessLLMOutput(BaseModel):
    """What the LLM returns; MW, notice and window are enforced in code afterwards."""

    accept: bool
    mw_offered: float = 0
    start: str | None = Field(default=None, description='"HH:MM"')
    end: str | None = Field(default=None, description='"HH:MM"')
    conditions: str = ""
    reply_text: str = Field(default="", description="at most 240 characters")


def _as_dict(request: Any) -> dict[str, Any]:
    if isinstance(request, BaseModel):
        return request.model_dump(by_alias=True)
    return dict(request or {})


def _business_id(req: dict[str, Any]) -> str | None:
    targets = req.get("targets") or {}
    if targets.get("business_id"):
        return str(targets["business_id"])
    for rid in targets.get("recipient_ids") or []:
        if str(rid).startswith("biz_"):
            return str(rid)
    to = req.get("to") or req.get("thread_id")
    return str(to) if to and str(to).startswith("biz_") else None


def _parse_hhmm(world: World, value: Any) -> datetime | None:
    try:
        return at(world.sim_now, str(value)[:5]) if value else None
    except (ValueError, TypeError):
        return None


def _profile(world: World, bid: str, static: dict[str, Any]) -> dict[str, Any]:
    live = world.businesses.get(bid, {})
    merged = {**static, **live}
    return {
        "id": bid,
        "name": str(merged.get("name") or bid),
        "max_mw": float(merged.get("max_mw") or 0),
        "notice_min": int(merged.get("notice_min") or 0),
        "protected": bool(merged.get("protected", False)),
        "constraints": merged.get("constraints") or "",
    }


def fallback_offer(world: World, prof: dict[str, Any], asked_mw: float,
                   start: datetime, end: datetime) -> BusinessLLMOutput:
    """Deterministic replies that follow each business's documented constraints (PLAN §5.2)."""
    name = prof["name"].lower()
    max_mw = prof["max_mw"]
    if prof["protected"] or max_mw <= 0:
        return BusinessLLMOutput(
            accept=False, mw_offered=0, conditions="protected site, full service required",
            reply_text=f"{prof['name']} must stay at full service during the heat event, so we "
                       "can't curtail. Thanks for checking.")
    ask = min(max_mw, asked_mw) if asked_mw > 0 else max_mw
    if "data cent" in name:
        mw = min(max_mw, 15.0)
        return BusinessLLMOutput(
            accept=True, mw_offered=mw, start=hhmm(start), end=hhmm(end),
            conditions=f"batch load shift only; needs {prof['notice_min'] or 30} min notice",
            reply_text=f"We can shift {mw:.0f} MW of batch compute from {hhmm(start)} to {hhmm(end)} "
                       f"with {prof['notice_min'] or 30} min notice.")
    if "mall" in name:
        mw = min(max_mw, 2.0)
        return BusinessLLMOutput(
            accept=True, mw_offered=mw, start=hhmm(start), end=hhmm(end),
            conditions="lighting dimmed only; cooling stays on (designated cool space)",
            reply_text=f"We'll dim lighting for {mw:.0f} MW, {hhmm(start)}-{hhmm(end)}. Cooling stays "
                       "on because we're a cool space.")
    if "office" in name or "tower" in name:
        mw = min(max_mw, 3.0)
        return BusinessLLMOutput(
            accept=True, mw_offered=mw, start=hhmm(start), end=hhmm(end),
            conditions="pre-cool the floors first, then set back",
            reply_text=f"We'll pre-cool and then set back for {mw:.0f} MW, {hhmm(start)}-{hhmm(end)}.")
    if "cold storage" in name:
        mw = min(max_mw, 4.0)
        coast_end = min(end, start + timedelta(minutes=90))
        return BusinessLLMOutput(
            accept=True, mw_offered=mw, start=hhmm(start), end=hhmm(coast_end),
            conditions="pre-chill then coast, 90 min maximum",
            reply_text=f"We can pre-chill and coast for 90 min: {mw:.0f} MW from {hhmm(start)} to "
                       f"{hhmm(coast_end)}.")
    return BusinessLLMOutput(
        accept=True, mw_offered=ask, start=hhmm(start), end=hhmm(end), conditions="",
        reply_text=f"We can take {ask:.0f} MW off from {hhmm(start)} to {hhmm(end)}.")


def _clamp(world: World, out: BusinessLLMOutput, prof: dict[str, Any], earliest: datetime,
           req_start: datetime, req_end: datetime) -> tuple[BusinessLLMOutput, datetime, datetime]:
    if prof["protected"] or prof["max_mw"] <= 0:
        out.accept, out.mw_offered = False, 0.0
    out.mw_offered = max(0.0, min(prof["max_mw"], float(out.mw_offered or 0)))
    if out.mw_offered <= 0:
        out.accept = False
    start = _parse_hhmm(world, out.start) or req_start
    end = _parse_hhmm(world, out.end) or req_end
    start = max(start, earliest)
    end = min(end, world.sim_end)
    if end <= start:
        end = min(world.sim_end, start + timedelta(minutes=60))
    out.start, out.end = hhmm(start), hhmm(end)
    out.reply_text = common.clip_text(out.reply_text or ("Accepted." if out.accept else "Declined."), REPLY_MAX)
    out.conditions = common.clip_text(out.conditions, 200)
    return out, start, end


async def on_request(world: World, request: ActionRequest | dict[str, Any]) -> Message | None:
    """A business answers a delivered business_curtail_request. Returns the stored reply. Never raises."""
    try:
        req = _as_dict(request)
        bid = _business_id(req)
        if not bid:
            return None
        static = (await common.businesses()).get(bid, {})
        prof = _profile(world, bid, static)
        params = req.get("params") or {}
        asked = float(params.get("mw_target") or req.get("expected_mw") or prof["max_mw"] or 0)
        window = req.get("window") or {}
        earliest = world.sim_now + timedelta(minutes=prof["notice_min"])
        req_start = max(_parse_hhmm(world, window.get("start")) or world.sim_now, earliest)
        req_end = _parse_hhmm(world, window.get("end")) or (req_start + timedelta(minutes=120))
        if req_end <= req_start:
            req_end = req_start + timedelta(minutes=60)
        msgs = req.get("messages") or []
        body = next((m.get("body") for m in msgs if m.get("to") == bid), None) or req.get("body") \
            or req.get("summary") or ""

        payload = {
            "run_id": world.run_id,
            "now": hhmm(world.sim_now),
            "business": prof,
            "request": {"message": body, "mw_asked": round(asked, 1),
                        "window": {"start": hhmm(req_start), "end": hhmm(req_end)}},
        }
        agent = f"sim:business:{bid}"
        res = await complete_json(
            agent=agent, model=get_settings().llm_model_fast, system=load_prompt("business"),
            payload=payload, schema=BusinessLLMOutput,
            fallback=lambda: fallback_offer(world, prof, asked, req_start, req_end),
            metadata={"run_id": world.run_id, "sim_ts": common.now_ts(world), "agent_id": agent,
                      "tags": ["world", "business"], "effort": "low",
                      "request_ids": [req["_id"]] if req.get("_id") else []},
        )
        out, start, end = _clamp(world, res.output, prof, earliest, req_start, req_end)  # type: ignore[arg-type]
        decision = ("decline" if not out.accept else
                    "accept" if out.mw_offered >= 0.95 * asked and out.start == hhmm(req_start) else
                    "counter")

        live = world.businesses.setdefault(bid, {"name": prof["name"], "max_mw": prof["max_mw"]})
        live["status"] = "accepted" if out.accept else "declined"
        live["accepted_mw"] = out.mw_offered if out.accept else 0.0
        live["active_from"] = start if out.accept else None
        live["active_until"] = end if out.accept else None
        live["offer"] = {"mw_offered": out.mw_offered, "start": out.start, "end": out.end,
                         "conditions": out.conditions, "decision": decision}

        offer = {"type": "curtailment", "mw_offered": out.mw_offered, "start": out.start,
                 "end": out.end, "conditions": out.conditions}
        msg = Message(
            id=new_id("msg_", world.run_id), run_id=world.run_id, thread_id=bid, from_=agent,
            to="agent:negotiator", body=out.reply_text, direction="in", trust="untrusted",
            sim_ts=common.now_ts(world), request_id=req.get("_id"), offer=offer, decision=decision,
        )
        await common.store_message(msg)
        what = (f"offers {out.mw_offered:.0f} MW {out.start}-{out.end}" if out.accept else "declines")
        await common.ledger_append(
            world.run_id, "message_in", agent,
            f"Untrusted reply from {prof['name']}: {decision}, {what}",
            sim_ts=msg.sim_ts, refs={"message_id": msg.id, "request_id": msg.request_id},
            payload={"thread_id": bid, "trust": "untrusted", "decision": decision, **offer,
                     "source": res.source},
            langsmith_run_id=res.langsmith_run_id,
        )
        return msg
    except Exception:  # noqa: BLE001 - world agents must never crash the sim
        log.exception("business.on_request failed")
        return None
