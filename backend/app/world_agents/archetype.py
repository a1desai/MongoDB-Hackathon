"""Archetype agents (PLAN §11.1): one FAST-model call per affected household group.

Each group's output (override hazard, offer acceptance, EV-pause acceptance) is clamped and
written to world.archetype_params, which sim/behavior.py reads for the rule-based followers.
Fallback: the archetype's defaults. These agents are the environment, not governed agents.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from ..config import get_settings
from ..llm.client import complete_json, load_prompt
from ..models import ArchetypeOutput
from ..sim.world_state import World
from ..timeutil import hhmm
from . import _common as common

log = logging.getLogger(__name__)

EVENT_TYPES = ("heat_warning", "precool_start", "setback_start", "sms_offer", "nearby_outage",
               "contingency_news", "restore")
PARAM_KEYS = ("override_rate_per_hour", "offer_acceptance", "ev_pause_acceptance")
LIMITS = {"override_rate_per_hour": (0.0, 0.8), "offer_acceptance": (0.0, 1.0),
          "ev_pause_acceptance": (0.0, 1.0)}
FALLBACK_LINES = {
    "heat_warning": "Residents notice the heat warning and keep their AC running.",
    "precool_start": "Homes cool down ahead of the evening; most residents barely notice.",
    "setback_start": "Thermostats ease up; some residents check their settings.",
    "sms_offer": "Residents read the CoolShift offer; reactions follow the usual pattern.",
    "nearby_outage": "News of a nearby outage makes residents more cautious with their AC.",
    "contingency_news": "Residents hear about the generator trip on local news.",
    "restore": "Power and normal settings return; residents settle back in.",
}

# run_id -> archetype id -> the defaults captured before any LLM update
_defaults: dict[str, dict[str, dict[str, float]]] = {}


class ArchetypeLLMOutput(BaseModel):
    """What the LLM returns; ranges are enforced by clamping, not by rejecting the reply."""

    override_rate_per_hour: float = Field(description="0 to 0.8")
    offer_acceptance: float = Field(description="0 to 1")
    ev_pause_acceptance: float = Field(description="0 to 1")
    mood: str = ""
    one_line: str = Field(default="", description="under 140 characters")


def clamp(raw: BaseModel | dict[str, Any]) -> ArchetypeOutput:
    """Clamp an LLM/fallback output into ArchetypeOutput's ranges."""
    d = raw.model_dump() if isinstance(raw, BaseModel) else dict(raw)
    vals: dict[str, Any] = {}
    for k, (lo, hi) in LIMITS.items():
        try:
            v = float(d.get(k, lo))
        except (TypeError, ValueError):
            v = lo
        vals[k] = lo if np.isnan(v) else min(hi, max(lo, v))
    vals["mood"] = common.clip_text(d.get("mood") or "", 40)
    vals["one_line"] = common.clip_text(d.get("one_line") or "", 160)
    return ArchetypeOutput(**vals)


def defaults_for(world: World, archetype_id: str) -> dict[str, float]:
    """The archetype's original defaults (snapshotted the first time this run reacts)."""
    snap = _defaults.setdefault(world.run_id, {})
    if archetype_id not in snap:
        cur = world.archetype_params.get(archetype_id, {})
        snap[archetype_id] = {k: float(cur.get(k, LIMITS[k][1] / 4)) for k in PARAM_KEYS}
    return snap[archetype_id]


def event_type(event: dict[str, Any]) -> str:
    """Normalise engine/world events to one of EVENT_TYPES."""
    payload = event.get("payload") or {}
    for cand in (event.get("event"), event.get("kind"), payload.get("event"), payload.get("kind"),
                 event.get("type")):
        if cand in EVENT_TYPES:
            return cand
        if cand in ("contingency", "generator_trip"):
            return "contingency_news"
        if cand in ("feeder_outage", "uncontrolled_trip", "outage"):
            return "nearby_outage"
        if cand in ("thermostat_restore", "feeder_restore"):
            return "restore"
        if cand == "precool":
            return "precool_start"
        if cand == "thermostat_setback":
            return "setback_start"
        if cand == "send_sms":
            return "sms_offer"
    return "heat_warning"


def _event_mask(world: World, event: dict[str, Any]) -> np.ndarray | None:
    """Households the event touches, or None for zone-wide events."""
    payload = event.get("payload") or {}
    hh_ids = event.get("household_ids") or payload.get("household_ids") or []
    sections = list(event.get("section_ids") or event.get("sections") or payload.get("section_ids") or [])
    for key in ("section_id",):
        sid = event.get(key) or payload.get(key)
        if sid:
            sections.append(sid)
    if not hh_ids and not sections:
        return None
    mask = np.zeros(world.n, dtype=bool)
    for hid in hh_ids:
        try:
            i = world.hh_index(str(hid))
            if 0 <= i < world.n:
                mask[i] = True
        except ValueError:
            continue
    for sid in sections:
        if sid in world.section_ids:
            mask |= world.section_idx == world.section_index(sid)
    return mask


def affected_archetypes(world: World, event: dict[str, Any]) -> list[str]:
    explicit = event.get("archetype_ids") or (event.get("payload") or {}).get("archetype_ids")
    if explicit:
        return [a for a in explicit if a in world.archetype_ids]
    mask = _event_mask(world, event)
    present = np.unique(world.archetype_idx if mask is None else world.archetype_idx[mask])
    return [world.archetype_ids[int(i)] for i in present if 0 <= int(i) < len(world.archetype_ids)]


def group_stats(world: World, archetype_id: str) -> dict[str, Any]:
    a = world.archetype_ids.index(archetype_id)
    m = world.archetype_idx == a
    n = int(m.sum())
    if n == 0:
        return {"homes_real": 0}

    def pct(arr: np.ndarray) -> float:
        return round(float(arr[m].mean()) * 100, 1)

    return {
        "homes_real": int(round(n * world.scale)),
        "mean_indoor_c": round(float(world.t_in[m].mean()), 1),
        "max_indoor_c": round(float(world.t_in[m].max()), 1),
        "pct_enrolled": pct(world.consent_thermostat),
        "pct_with_ev": pct(world.has_ev),
        "pct_overriding": pct(world.overridden),
        "pct_setback_active": pct(world.setback_active),
        "pct_precool_active": pct(world.precool_active),
        "pct_without_power": round(float((~world.power_on[m]).mean()) * 100, 1),
    }


def _outdoor_c(world: World) -> float | None:
    try:
        from ..sim.weather import outdoor_c

        return round(float(outdoor_c(world, world.sim_now)), 1)
    except Exception:  # noqa: BLE001
        return None


async def _react_one(world: World, archetype_id: str, etype: str, summary: str,
                     description: str) -> tuple[str, ArchetypeOutput, str, str | None]:
    defaults = defaults_for(world, archetype_id)

    def fallback() -> ArchetypeLLMOutput:
        return ArchetypeLLMOutput(**defaults, mood="steady", one_line=FALLBACK_LINES[etype])

    payload = {
        "run_id": world.run_id,
        "now": hhmm(world.sim_now),
        "outdoor_c": _outdoor_c(world),
        "archetype": {"id": archetype_id, "description": description, "defaults": defaults},
        "group": group_stats(world, archetype_id),
        "event": {"type": etype, "summary": summary},
    }
    agent = f"sim:archetype:{archetype_id}"
    res = await complete_json(
        agent=agent, model=get_settings().llm_model_fast, system=load_prompt("archetype"),
        payload=payload, schema=ArchetypeLLMOutput, fallback=fallback,
        metadata={"run_id": world.run_id, "sim_ts": common.now_ts(world), "agent_id": agent,
                  "tags": ["world", "archetype"], "effort": "low"},
    )
    return archetype_id, clamp(res.output), res.source, res.langsmith_run_id


async def react(world: World, event: dict[str, Any], *, log_ledger: bool = True
                ) -> dict[str, ArchetypeOutput]:
    """Let every archetype touched by `event` react; updates world.archetype_params in place.

    `event`: {type|event|kind, summary?, archetype_ids?, household_ids?, section_ids?|section_id?,
    payload?}. Returns archetype id -> clamped ArchetypeOutput. Never raises.
    """
    try:
        etype = event_type(event)
        summary = common.clip_text(event.get("summary") or etype.replace("_", " "), 240)
        targets = affected_archetypes(world, event)
        if not targets:
            return {}
        for a in world.archetype_ids:  # snapshot all defaults before the first update
            defaults_for(world, a)
        descs = await common.archetypes()
        results = await asyncio.gather(*[
            _react_one(world, a, etype, summary, str(descs.get(a, {}).get("description") or a))
            for a in targets
        ])
    except Exception:  # noqa: BLE001 - world agents must never crash the sim
        log.exception("archetype.react failed")
        return {}

    out: dict[str, ArchetypeOutput] = {}
    sources: dict[str, int] = {}
    trace_id = None
    for a, o, source, tid in results:
        params = world.archetype_params.setdefault(a, {})
        params.update({k: float(getattr(o, k)) for k in PARAM_KEYS})  # floats only (behavior.py)
        out[a] = o
        sources[source] = sources.get(source, 0) + 1
        trace_id = trace_id or tid

    if log_ledger and out:
        mean_override = sum(o.override_rate_per_hour for o in out.values()) / len(out)
        mean_offer = sum(o.offer_acceptance for o in out.values()) / len(out)
        await common.ledger_append(
            world.run_id, "world_event", "sim:archetypes",
            f"{len(out)} household groups reacted to {etype.replace('_', ' ')}: "
            f"mean override {mean_override:.2f}/h, offer acceptance {mean_offer:.0%}",
            sim_ts=common.now_ts(world),
            payload={"event": etype, "sources": sources,
                     "groups": {a: o.model_dump() for a, o in out.items()}},
            langsmith_run_id=trace_id,
        )
    return out
