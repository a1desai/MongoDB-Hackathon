"""Negotiator agent (PLAN §8.2): writes the outbound texts for send_sms and
business_curtail_request action requests. It only proposes: every send is a gated request
(rule R06 MESSAGING) and only the Executor talks to TextLine.

- business_curtail_request: one FAST-model message per business.
- send_sms to featured residents: one FAST-model message per resident.
- everyone else: a template (no LLM), so a 2,000-home batch costs nothing.

Every body is <= 320 chars, ends "Reply STOP to opt out", carries only energy offers, event
timing and public cool-space locations (no health advice), and incentives are capped at
10 CAD. LLM text that breaks a rule is replaced by the template.
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from ..config import get_settings
from ..db import POLICIES, db
from ..llm.client import complete_json, current_trace_id, load_prompt, traced, wrap_untrusted
from ..models import ActionRequest, OutboundMessage
from ..sim.world_state import World
from ..timeutil import hhmm
from ..world_agents import _common as common

log = logging.getLogger(__name__)

AGENT = "agent:negotiator"
R06_DEFAULTS: dict[str, Any] = {
    "max_chars": 320,
    "max_incentive_cad": 10,
    "denylist": ["doctor", "medication", "symptom", "heatstroke", "dehydration"],
    "required_suffix": "Reply STOP to opt out",
}
# Stricter screen for LLM-written text only (templates are fixed by code).
EXTRA_DENY = ("health", "medical", "medicine", "hydrat", "illness", "sick", "heat stroke",
              "heat exhaustion", "symptom", "doctor", "nurse", "hospital", "911", "emergency room")
DEFAULT_INCENTIVE_CAD = 5.0

TEMPLATES = {
    "reduce_cooling": ("CoolShift: high power demand is expected {start}-{end} today. Ease your AC "
                       "by about 2°C in that window and earn a ${amount:.0f} bill credit. "
                       "Reply YES to join."),
    "cool_space_notice": ("CoolShift: a planned power interruption may affect your area from {start} "
                          "for up to {duration} min. {cool_space} is open as a public cool space "
                          "nearby. We will text you when power is back."),
    "ev_pause_notice": ("CoolShift: your EV charging may pause {start}-{end} to ease peak demand and "
                        "will resume after. Reply NO to charge as usual."),
    "event_notice": ("CoolShift: high power demand is expected {start}-{end} today. Using less "
                     "electricity in that window helps keep the grid stable. Thank you."),
    "correction": "CoolShift: please disregard our earlier message about {start}-{end}. No action is needed.",
    "curtail": ("Lakeshore Grid Ops (CoolShift program): we forecast a supply shortfall {start}-{end}. "
                "Could {name} reduce load by up to {mw:.0f} MW in that window? We can give {notice} min "
                "notice. Please reply with what you can offer."),
}

_r06_cache: dict[str, Any] = {}


class NegotiatorLLMOutput(BaseModel):
    """What the LLM returns; the body is screened and finalised by code, the offer is code's."""

    recipient_id: str = ""
    body: str = Field(description="at most 300 characters, plain text")
    offer: dict[str, Any] = {}


async def r06_params() -> dict[str, Any]:
    """R06 MESSAGING params from the policies collection (cached), or the seeded defaults."""
    if _r06_cache:
        return _r06_cache
    params = dict(R06_DEFAULTS)
    try:
        doc = await asyncio.wait_for(db()[POLICIES].find_one({"_id": "R06"}), common.DB_TIMEOUT_S)
        if doc and isinstance(doc.get("params"), dict):
            params.update({k: v for k, v in doc["params"].items() if v is not None})
            _r06_cache.update(params)
    except Exception as e:  # noqa: BLE001
        log.warning("loading R06 params failed, using defaults: %s", e)
    return params


def _strip_suffix(text: str, suffix: str) -> str:
    pattern = re.compile(r"[\s.,;:!-]*" + re.escape(suffix).replace(r"\ ", r"\s+") + r"[\s.!]*",
                         re.IGNORECASE)
    return pattern.sub(" ", text).strip()


def violates(text: str, denylist: list[str], strict: bool = False) -> bool:
    low = text.lower()
    words = list(denylist) + (list(EXTRA_DENY) if strict else [])
    return any(w.lower() in low for w in words)


def finalize_body(core: str, fallback_core: str, params: dict[str, Any], *, strict: bool = False) -> str:
    """<= max_chars, ends with the required suffix, denylist-clean (else the fallback text)."""
    suffix = str(params.get("required_suffix") or R06_DEFAULTS["required_suffix"])
    max_chars = int(params.get("max_chars") or R06_DEFAULTS["max_chars"])
    deny = list(params.get("denylist") or R06_DEFAULTS["denylist"])
    text = " ".join(str(core or "").split())
    text = _strip_suffix(text, suffix)
    if not text or violates(text, deny, strict) or "http" in text.lower():
        text = _strip_suffix(" ".join(fallback_core.split()), suffix)
    room = max_chars - len(suffix) - 1
    if len(text) > room:
        cut = text[: room - 1]
        cut = cut[: cut.rfind(" ")] if " " in cut else cut
        text = cut.rstrip(" ,;:-") + "…"
    if text and text[-1] not in ".!?…":
        text += "."
    return f"{text} {suffix}"


def offer_type(request: ActionRequest) -> str:
    if request.action_type == "business_curtail_request":
        return "curtail"
    p = request.params or {}
    raw = str(p.get("offer_type") or p.get("purpose") or p.get("message_type") or p.get("kind") or "").lower()
    if raw in TEMPLATES and raw != "curtail":
        return raw
    if "incentive" in raw or "reduce" in raw:
        return "reduce_cooling"
    if any(k in raw for k in ("cool_space", "pre_notify", "prenotify", "notify", "outage")):
        return "cool_space_notice"
    if "correct" in raw or request.undo_of:
        return "correction"
    if "ev" in raw.split("_"):
        return "ev_pause_notice"
    if p.get("amount_cad"):
        return "reduce_cooling"
    return "event_notice"


def _window(request: ActionRequest, world: World) -> tuple[str, str]:
    p = request.params or {}
    if request.window:
        return request.window.start, request.window.end
    start = str(p.get("start") or hhmm(world.sim_now))[:5]
    end = str(p.get("end") or hhmm(world.sim_end))[:5]
    return start, end


def nearest_cool_spaces(world: World, idx: np.ndarray) -> list[dict[str, Any] | None]:
    """Nearest designated cool space for each household index (equirectangular distance)."""
    spaces = [f for f in world.facilities if f.get("kind") == "cool_space" and f.get("designated")]
    if not spaces:
        spaces = [f for f in world.facilities if f.get("kind") == "cool_space"]
    if not spaces or len(idx) == 0:
        return [None] * len(idx)
    flon = np.array([float(f.get("lon", 0)) for f in spaces])
    flat = np.array([float(f.get("lat", 0)) for f in spaces])
    lon, lat = world.lon[idx][:, None], world.lat[idx][:, None]
    d2 = ((lon - flon[None, :]) * np.cos(np.radians(43.7))) ** 2 + (lat - flat[None, :]) ** 2
    return [spaces[int(j)] for j in d2.argmin(axis=1)]


def _offer(otype: str, request: ActionRequest, start: str, end: str, max_incentive: float,
           mw: float = 0.0, cool_space: dict[str, Any] | None = None) -> dict[str, Any]:
    if otype == "reduce_cooling":
        amount = float((request.params or {}).get("amount_cad") or DEFAULT_INCENTIVE_CAD)
        amount = max(0.0, min(float(max_incentive), amount))
        return {"type": "reduce_cooling", "amount_cad": amount, "ask": f"reduce AC by 2°C {start}-{end}"}
    if otype == "curtail":
        return {"type": "curtailment", "amount_cad": 0, "ask": f"up to {mw:.0f} MW {start}-{end}"}
    offer: dict[str, Any] = {"type": otype, "amount_cad": 0, "ask": "none"}
    if cool_space:
        offer["cool_space_id"] = cool_space.get("_id")
        offer["cool_space_name"] = cool_space.get("name")
    return offer


def _template(otype: str, **kw: Any) -> str:
    try:
        return TEMPLATES[otype].format(**kw)
    except (KeyError, ValueError, IndexError):
        return TEMPLATES["event_notice"].format(start=kw.get("start", ""), end=kw.get("end", ""))


async def _llm_body(*, world: World, request: ActionRequest, recipient_id: str, kind: str,
                    recipient: dict[str, Any], offer: dict[str, Any], window: dict[str, str],
                    context: dict[str, Any], fallback_core: str) -> tuple[str, str | None]:
    payload = {"run_id": world.run_id, "kind": kind, "recipient_id": recipient_id,
               "recipient": recipient, "offer": offer, "window": window, "context": context}
    res = await complete_json(
        agent=AGENT, model=get_settings().llm_model_fast, system=load_prompt("negotiator"),
        payload=payload, schema=NegotiatorLLMOutput,
        fallback=lambda: NegotiatorLLMOutput(recipient_id=recipient_id, body=fallback_core, offer=offer),
        metadata={"run_id": world.run_id, "sim_ts": common.now_ts(world), "agent_id": AGENT,
                  "request_ids": [request.id], "tags": ["governed", "negotiator"], "effort": "low"},
    )
    return str(getattr(res.output, "body", "") or ""), res.langsmith_run_id


async def _business_messages(request: ActionRequest, world: World, params: dict[str, Any],
                             start: str, end: str) -> list[OutboundMessage]:
    bids = [request.targets.business_id] if request.targets.business_id else \
        [r for r in request.targets.recipient_ids if str(r).startswith("biz_")]
    static = await common.businesses()
    out: list[OutboundMessage] = []
    for bid in [b for b in bids if b]:
        b = {**static.get(bid, {}), **world.businesses.get(bid, {})}
        name = str(b.get("name") or bid)
        max_mw = float(b.get("max_mw") or 0)
        mw = float((request.params or {}).get("mw_target") or request.expected_mw or max_mw)
        mw = min(mw, max_mw) if max_mw > 0 else mw
        notice = int(b.get("notice_min") or 0)
        offer = _offer("curtail", request, start, end, params["max_incentive_cad"], mw=mw)
        fallback_core = _template("curtail", start=start, end=end, name=name, mw=mw, notice=notice)
        thread = await common.thread_tail(world.run_id, bid, 3)
        replies = [m for m in thread if m.get("direction") == "in"]
        body, _ = await _llm_body(
            world=world, request=request, recipient_id=bid, kind="business_request",
            recipient={"name": name, "max_mw": max_mw, "notice_min": notice,
                       "protected": bool(b.get("protected")), "constraints": b.get("constraints") or ""},
            offer=offer, window={"start": start, "end": end},
            context={"mw_asked": round(mw, 1),
                     "previous_replies": wrap_untrusted(replies) if replies else ""},
            fallback_core=fallback_core,
        )
        out.append(OutboundMessage(to=bid, body=finalize_body(body, fallback_core, params, strict=True),
                                   offer=offer))
    return out


async def _sms_messages(request: ActionRequest, world: World, params: dict[str, Any],
                        start: str, end: str) -> list[OutboundMessage]:
    recipients = list(dict.fromkeys(request.targets.recipient_ids or request.targets.household_ids))
    recipients = [r for r in recipients if str(r).startswith("hh_")]
    if not recipients:
        return []
    otype = offer_type(request)
    idx = np.array([world.hh_index(r) for r in recipients], dtype=int)
    spaces = nearest_cool_spaces(world, idx) if otype == "cool_space_notice" else [None] * len(idx)
    duration = int((request.params or {}).get("duration_min") or 60)
    featured = common.featured_by_index(world)
    personas = await common.residents() if any(int(i) in featured for i in idx) else {}

    async def one(k: int) -> OutboundMessage:
        hh, i, space = recipients[k], int(idx[k]), spaces[k]
        offer = _offer(otype, request, start, end, params["max_incentive_cad"], cool_space=space)
        core = _template(otype, start=start, end=end, amount=offer.get("amount_cad") or 0,
                         duration=duration,
                         cool_space=(space or {}).get("name") or "Your nearest community centre")
        rid = featured.get(i)
        if rid is None:
            return OutboundMessage(to=hh, body=finalize_body(core, core, params), offer=offer)
        p = personas.get(rid, {})
        first = str(p.get("name") or "there").split()[0]
        thread = await common.thread_tail(world.run_id, hh, 3)
        replies = [m for m in thread if m.get("direction") == "in"]
        body, _ = await _llm_body(
            world=world, request=request, recipient_id=hh, kind="resident_sms",
            recipient={"first_name": first, "persona": p.get("persona") or "",
                       "attitudes": p.get("attitudes") or ""},
            offer=offer, window={"start": start, "end": end},
            context={"cool_space": (space or {}).get("name"), "duration_min": duration,
                     "previous_replies": wrap_untrusted(replies) if replies else ""},
            fallback_core=f"Hi {first}! {core}",
        )
        return OutboundMessage(to=hh, body=finalize_body(body, core, params, strict=True), offer=offer)

    return list(await asyncio.gather(*[one(k) for k in range(len(recipients))]))


@traced(AGENT, tags=["governed", "negotiator"],
        process_inputs=lambda d: {"request_id": getattr(d.get("request"), "id", None),
                                  "action_type": getattr(d.get("request"), "action_type", None)})
async def compose(request: ActionRequest, world: World) -> list[OutboundMessage]:
    """Outbound messages for a send_sms or business_curtail_request request (one per recipient).

    Never raises: on any error every recipient gets the template text.
    """
    params = await r06_params()
    start, end = _window(request, world)
    try:
        if request.action_type == "business_curtail_request":
            msgs = await _business_messages(request, world, params, start, end)
        else:
            msgs = await _sms_messages(request, world, params, start, end)
    except Exception:  # noqa: BLE001 - the sim must never stall on the Negotiator
        log.exception("negotiator.compose failed; using templates")
        otype = offer_type(request)
        core = _template(otype, start=start, end=end, amount=DEFAULT_INCENTIVE_CAD, duration=60,
                         cool_space="Your nearest community centre", name="your site", mw=0, notice=0)
        to = request.targets.recipient_ids or request.targets.household_ids or \
            [b for b in [request.targets.business_id] if b]
        msgs = [OutboundMessage(to=r, body=finalize_body(core, core, params),
                                offer=_offer(otype, request, start, end, params["max_incentive_cad"]))
                for r in to]
    if request.langsmith_run_id is None:
        request.langsmith_run_id = current_trace_id()
    return msgs
