"""Static world for the map, and featured residents (PLAN §13, CONTRACTS "Engine and API").

GET /world/static returns
  {sections, facilities, neighbourhoods: FeatureCollection,
   households: {ids, lon, lat, vulnerable, featured: {res_id: idx}}, residents: [...]}
with households in fixed `idx` order, the same order as the map buffers. It reads Atlas and falls
back to data/processed/*.json when Atlas has no world yet; the result is cached per process.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from fastapi import APIRouter, HTTPException

from ..config import get_settings
from ..db import FACILITIES, HOUSEHOLDS, MESSAGES, NEIGHBOURHOODS, RESIDENTS, RUNS, SECTIONS, db
from ..observations import load_observations
from .common import J, latest_live_run_id, live_engine

log = logging.getLogger("cooldown.api.world")

router = APIRouter(tags=["world"])

_static: dict[str, Any] | None = None
_static_lock = asyncio.Lock()
SIMPLIFY_DEG = 0.0004  # ~40 m: neighbourhood outlines only need to look right at city zoom
HIDDEN_RESIDENT_FIELDS = {"injection_actor"}  # scenario plumbing, never shown in the UI


def _round(coords: Any, nd: int = 5) -> Any:
    if isinstance(coords, (list, tuple)):
        if coords and isinstance(coords[0], (int, float)):
            return [round(float(c), nd) for c in coords]
        return [_round(c, nd) for c in coords]
    return coords


def _simplify(geom: dict[str, Any] | None) -> dict[str, Any] | None:
    if not geom:
        return geom
    try:
        from shapely.geometry import mapping, shape

        g = shape(geom).simplify(SIMPLIFY_DEG, preserve_topology=True)
        if not g.is_empty:
            geom = json.loads(json.dumps(mapping(g)))
    except Exception:  # shapely missing or a bad ring: send it as is
        pass
    return {"type": geom.get("type"), "coordinates": _round(geom.get("coordinates"))}


def _fc(features: list[dict[str, Any]]) -> dict[str, Any]:
    return {"type": "FeatureCollection", "features": features}


def _feature(geom: dict[str, Any] | None, props: dict[str, Any], fid: str) -> dict[str, Any]:
    return {"type": "Feature", "id": fid, "geometry": geom, "properties": props}


async def _docs(coll: str, file_key: str, projection: dict | None = None, sort: str | None = None) -> list[dict]:
    try:
        cur = db()[coll].find({}, projection)
        if sort:
            cur = cur.sort(sort, 1)
        docs = await cur.to_list(None)
        if docs:
            return docs
    except Exception as exc:
        log.warning("reading %s from Atlas failed: %s", coll, exc)
    path = get_settings().data_dir / "processed" / f"{file_key}.json"
    if not path.exists():
        return []
    docs = json.loads(path.read_text())
    return sorted(docs, key=lambda d: d.get(sort, 0)) if sort else docs


async def build_static() -> dict[str, Any]:
    sections, facilities, hoods, households, residents = await asyncio.gather(
        _docs(SECTIONS, "sections", sort="_id"),
        _docs(FACILITIES, "facilities"),
        _docs(NEIGHBOURHOODS, "neighbourhoods", {"attrs": 0}),
        _docs(HOUSEHOLDS, "households", {"_id": 1, "idx": 1, "location": 1, "vulnerable": 1}, sort="idx"),
        _docs(RESIDENTS, "residents", sort="_id"),
    )
    sec_fc = _fc([
        _feature(_round(s.get("geometry")), {
            "id": s["_id"], "households": s.get("households"), "vulnerable": s.get("vulnerable"),
            "income_quintile": s.get("income_quintile"), "protected": bool(s.get("protected")),
            "protected_reasons": s.get("protected_reasons") or [],
            "centroid": (s.get("centroid") or {}).get("coordinates"),
        }, s["_id"]) for s in sections
    ])
    fac_fc = _fc([
        _feature(f.get("location"), {
            "id": f["_id"], "kind": f.get("kind"), "name": f.get("name"), "designated": bool(f.get("designated")),
            "section_id": f.get("section_id"), "address": f.get("address"), "hours": f.get("hours"),
        }, f["_id"]) for f in facilities
    ])
    hood_fc = _fc([
        _feature(_simplify(h.get("geometry")), {"id": h["_id"], "name": h.get("name"),
                                                "income_quintile": h.get("income_quintile")}, h["_id"])
        for h in hoods
    ])
    ids, lon, lat, vul = [], [], [], []
    for h in households:
        c = (h.get("location") or {}).get("coordinates") or [None, None]
        ids.append(h["_id"])
        lon.append(round(float(c[0]), 6) if c[0] is not None else None)
        lat.append(round(float(c[1]), 6) if c[1] is not None else None)
        vul.append(1 if h.get("vulnerable") else 0)
    index = {hid: i for i, hid in enumerate(ids)}
    featured = {r["_id"]: index[r["household_id"]] for r in residents if r.get("household_id") in index}
    res_out = [{k: v for k, v in r.items() if k not in HIDDEN_RESIDENT_FIELDS} for r in residents]
    for r in res_out:
        r["idx"] = index.get(r.get("household_id"))
    return {"sections": sec_fc, "facilities": fac_fc, "neighbourhoods": hood_fc,
            "households": {"ids": ids, "lon": lon, "lat": lat, "vulnerable": vul, "featured": featured},
            "residents": res_out}


@router.get("/world/static")
async def world_static(refresh: bool = False) -> Any:
    global _static
    async with _static_lock:
        if _static is None or refresh or not _static["households"]["ids"]:
            _static = await build_static()
    return J(_static)


@router.get("/world/observations")
def world_observations() -> Any:
    observations = load_observations()
    if not observations:
        raise HTTPException(404, "Historical observations are unavailable. Run scripts.fetch_data and scripts.build_world.")
    return J(observations)


@router.get("/world/hydro-lines")
def world_hydro_lines() -> Any:
    lines = load_observations("hydro-lines.geojson")
    if not lines:
        raise HTTPException(404, "Ontario line data is unavailable. Run scripts.fetch_data and scripts.build_world.")
    return J(lines)


# ---------------------------------------------------------------- residents

async def _resident(resident_id: str) -> dict[str, Any]:
    doc = await db()[RESIDENTS].find_one({"_id": resident_id})
    if doc is None:
        path = get_settings().data_dir / "processed" / "residents.json"
        if path.exists():
            doc = next((r for r in json.loads(path.read_text()) if r.get("_id") == resident_id), None)
    if doc is None:
        raise HTTPException(404, f"resident {resident_id} not found")
    return doc


async def _run_for(run_id: str | None) -> str | None:
    if run_id:
        return run_id
    live = latest_live_run_id()
    if live:
        return live
    doc = await db()[RUNS].find_one({}, {"_id": 1}, sort=[("created_wall_ts", -1)])
    return doc["_id"] if doc else None


def _home_state(run_id: str | None, household_id: str) -> dict[str, Any] | None:
    eng = live_engine(run_id) if run_id else None
    if eng is None:
        return None
    w = eng.world
    try:
        i = w.hh_index(household_id)
        s = int(w.section_idx[i])
        return {
            "run_id": run_id, "sim_ts": eng.tick_msg()["sim_ts"],
            "t_in": round(float(w.t_in[i]), 2), "setpoint_c": round(float(w.setpoint_base[i] + w.offset_c[i]), 2),
            "offset_c": round(float(w.offset_c[i]), 2), "power_on": bool(w.power_on[i]),
            "precool": bool(w.precool_active[i]), "setback": bool(w.setback_active[i]),
            "overridden": bool(w.overridden[i]), "sms_accepted": bool(w.sms_accepted[i]),
            "sms_stopped": bool(w.sms_stopped[i]), "ev_charging": bool(w.ev_charging[i]),
            "ev_paused": bool(w.ev_paused[i]), "battery_soc": round(float(w.battery_soc[i]), 3),
            "battery_discharging": bool(w.battery_discharging[i]), "notified_cool_space": bool(w.notified_cool_space[i]),
            "outage_minutes": int(w.outage_minutes[i]), "over_t_safe_minutes": int(w.over_t_safe_minutes[i]),
            "section_id": w.section_ids[s], "section_cut": bool(w.section_cut[s]),
            "kw": round(float(w.p_kw[i]), 3),
        }
    except Exception as exc:
        log.debug("home state for %s failed: %s", household_id, exc)
        return None


@router.get("/residents/{resident_id}")
async def resident(resident_id: str, run_id: str | None = None) -> Any:
    doc = {k: v for k, v in (await _resident(resident_id)).items() if k not in HIDDEN_RESIDENT_FIELDS}
    hh_id = doc.get("household_id")
    if hh_id:
        hh = await db()[HOUSEHOLDS].find_one({"_id": hh_id}, {"location": 0})
        if hh:
            doc["home"] = hh
    rid = await _run_for(run_id)
    doc["state"] = _home_state(rid, hh_id) if hh_id else None
    doc["run_id"] = rid
    return J(doc)


@router.get("/residents/{resident_id}/thread")
async def resident_thread(resident_id: str, run_id: str | None = None) -> Any:
    doc = await _resident(resident_id)
    rid = await _run_for(run_id)
    hh_id = doc.get("household_id")
    msgs: list[dict] = []
    if rid and hh_id:
        msgs = await db()[MESSAGES].find({"run_id": rid, "thread_id": hh_id}).sort("sim_ts", 1).to_list(200)
    return J({"resident_id": resident_id, "household_id": hh_id, "run_id": rid, "messages": msgs})
