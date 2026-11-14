"""Build a run's in-memory World from the Atlas collections (households, sections, archetypes,
businesses, facilities, weather, neighbourhoods, residents).

`build_world` is the pure part (documents in, World out) so tests and the synthetic world can use
it without a database. It initialises dynamic state, warms indoor temperatures up from 06:00 so
12:00 starts in a realistic state, and calibrates `world.scale` (PLAN §5.2 item 8): the number of
real homes per simulated home that makes the uncontrolled zone peak equal the scenario's
target_peak_mw (see calibrate_scale).
"""

import copy
import dataclasses
import json
import logging
import math
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any

import numpy as np

from ..config import get_settings
from ..db import ARCHETYPES, BUSINESSES, FACILITIES, HOUSEHOLDS, NEIGHBOURHOODS, RESIDENTS, SECTIONS, WEATHER, db
from ..timeutil import TZ
from . import clock, grid, physics, profiles, thermal, weather
from . import scenario as scenario_mod
from .world_state import AC_TYPE, DWELLING, World

log = logging.getLogger("cooldown.sim.loader")

WARMUP_START = "06:00"
EV_KWH_RANGE = (5.0, 15.0)  # energy each EV still needs on arrival
BATTERY_SOC_RANGE = (0.85, 1.0)
BATTERY_KW = 5.0
DEFAULT_THERMAL = {  # PLAN §5.2 item 2 defaults by dwelling / AC type
    "k_env": {"house": 0.30, "apartment": 0.20, "highrise": 0.25},
    "k_ac": {"central": 4.5, "window": 2.5, "none": 0.0},
    "ac_kw": {"central": 3.0, "window": 1.0, "none": 0.0},
    "solar": {"house": 0.3, "apartment": 0.2, "highrise": 0.2},
}
COLLECTIONS = {
    "households": HOUSEHOLDS, "sections": SECTIONS, "archetypes": ARCHETYPES, "businesses": BUSINESSES,
    "facilities": FACILITIES, "neighbourhoods": NEIGHBOURHOODS, "residents": RESIDENTS,
}


# ---------------------------------------------------------------- helpers

def _f(v: Any, default: float) -> float:
    try:
        x = float(v)
        return x if math.isfinite(x) else default
    except (TypeError, ValueError):
        return default


def _lonlat(geo: Any) -> tuple[float, float]:
    try:
        c = geo["coordinates"]
        return float(c[0]), float(c[1])
    except (TypeError, KeyError, IndexError, ValueError):
        return float("nan"), float("nan")


def _hhmm_time(s: str) -> time:
    h, m = (int(x) for x in str(s).split(":")[:2])
    return time(h % 24, m)


def sim_bounds(scenario: dict[str, Any]) -> tuple[datetime, datetime]:
    day = scenario_mod.scenario_date(scenario)
    start = datetime.combine(day, _hhmm_time(scenario.get("sim_start", "12:00")), tzinfo=TZ)
    end = datetime.combine(day, _hhmm_time(scenario.get("sim_end", "21:30")), tzinfo=TZ)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _index(ids: list[str]) -> dict[str, int]:
    return {k: i for i, k in enumerate(ids)}


# ---------------------------------------------------------------- pure build

def build_world(run_id: str, mode: str, scenario: dict, seed: int | None, docs: dict[str, list[dict]], *,
                warmup: bool = True, calibrate: bool = True, scale: float | None = None) -> World:
    """Documents (as stored in Atlas / data/processed) -> a ready-to-run World at sim_start."""
    seed = int(seed if seed is not None else get_settings().sim_seed)
    scenario = scenario_mod.with_defaults(dict(scenario or {}))
    scenario["_seed"] = seed
    hh = sorted(docs.get("households") or [], key=lambda d: int(d.get("idx", 0)))
    if not hh:
        raise ValueError("no households to build a world from (run scripts.build_world and scripts.init_db)")
    n = len(hh)

    sec_docs = sorted(docs.get("sections") or [], key=lambda d: str(d["_id"]))
    section_ids = [str(d["_id"]) for d in sec_docs]
    for d in hh:
        sid = d.get("section_id")
        if sid and sid not in section_ids:
            section_ids.append(str(sid))
    if not section_ids:
        section_ids = ["TOR-S01"]
    sec_map = _index(section_ids)
    sec_by_id = {str(d["_id"]): d for d in sec_docs}

    arch_docs = sorted(docs.get("archetypes") or [], key=lambda d: str(d["_id"]))
    archetype_ids = [str(d["_id"]) for d in arch_docs]
    for d in hh:
        a = d.get("archetype_id")
        if a and a not in archetype_ids:
            archetype_ids.append(str(a))
    if not archetype_ids:
        archetype_ids = ["default"]
    arch_map = _index(archetype_ids)
    archetype_params: dict[str, dict[str, float]] = {}
    for aid in archetype_ids:
        d = next((x for x in arch_docs if str(x["_id"]) == aid), {})
        dfl = d.get("defaults") or {}
        archetype_params[aid] = {
            "override_rate_per_hour": _f(dfl.get("override_rate_per_hour"), 0.15),
            "offer_acceptance": _f(dfl.get("offer_acceptance"), 0.30),
            "ev_pause_acceptance": _f(dfl.get("ev_pause_acceptance"), 0.75),
        }

    nb_ids = sorted({str(d["_id"]) for d in docs.get("neighbourhoods") or []})
    for d in hh:
        nb = d.get("neighbourhood_id")
        if nb and nb not in nb_ids:
            nb_ids.append(str(nb))
    nb_map = _index(nb_ids)

    lon = np.empty(n)
    lat = np.empty(n)
    section_idx = np.zeros(n, dtype=np.int64)
    archetype_idx = np.zeros(n, dtype=np.int64)
    neighbourhood_idx = np.zeros(n, dtype=np.int64)
    quint = np.full(n, 3, dtype=np.int64)
    dwelling = np.zeros(n, dtype=np.int8)
    ac_type = np.zeros(n, dtype=np.int8)
    b = {k: np.zeros(n, dtype=bool) for k in (
        "has_thermostat", "has_ev", "has_battery", "vulnerable", "consent_thermostat", "consent_ev",
        "consent_battery", "sms_opt_in")}
    k_env = np.empty(n)
    k_ac = np.empty(n)
    ac_kw = np.empty(n)
    solar = np.empty(n)
    setpoint = np.empty(n)
    comfort = np.empty(n)
    base_scale = np.empty(n)
    ev_arr = np.full(n, np.nan)
    hh_ids: list[str] = []
    featured: dict[str, int] = {}

    for i, d in enumerate(hh):
        hh_ids.append(str(d.get("_id") or f"hh_{i + 1:05d}"))
        lon[i], lat[i] = _lonlat(d.get("location"))
        section_idx[i] = sec_map.get(str(d.get("section_id")), 0)
        archetype_idx[i] = arch_map.get(str(d.get("archetype_id")), 0)
        neighbourhood_idx[i] = nb_map.get(str(d.get("neighbourhood_id")), 0)
        quint[i] = int(_f(d.get("income_quintile"), 3))
        dw = str(d.get("dwelling") or "house")
        ac = str(d.get("ac_type") or "none")
        dwelling[i] = DWELLING.index(dw) if dw in DWELLING else 0
        ac_type[i] = AC_TYPE.index(ac) if ac in AC_TYPE else 0
        th = d.get("thermal") or {}
        k_env[i] = _f(th.get("k_env"), DEFAULT_THERMAL["k_env"].get(dw, 0.25))
        k_ac[i] = _f(th.get("k_ac"), DEFAULT_THERMAL["k_ac"].get(ac, 0.0))
        ac_kw[i] = _f(th.get("ac_kw"), DEFAULT_THERMAL["ac_kw"].get(ac, 0.0))
        solar[i] = _f(th.get("solar"), DEFAULT_THERMAL["solar"].get(dw, 0.2))
        if ac == "none":
            k_ac[i] = ac_kw[i] = 0.0
        setpoint[i] = _f(d.get("setpoint_base"), 23.5)
        comfort[i] = _f(d.get("comfort_max"), setpoint[i] + 3.0)
        base_scale[i] = _f(d.get("base_kw_scale"), 0.9)
        dev = d.get("devices") or {}
        con = d.get("consent") or {}
        b["has_thermostat"][i] = bool(dev.get("thermostat_id")) and ac == "central"
        b["has_ev"][i] = bool(d.get("has_ev"))
        b["has_battery"][i] = bool(d.get("has_battery"))
        b["vulnerable"][i] = bool(d.get("vulnerable"))
        b["consent_thermostat"][i] = bool(con.get("thermostat_program")) and b["has_thermostat"][i]
        b["consent_ev"][i] = bool(con.get("ev_program")) and b["has_ev"][i]
        b["consent_battery"][i] = bool(con.get("battery_program")) and b["has_battery"][i]
        b["sms_opt_in"][i] = bool(con.get("sms_opt_in"))
        if b["has_ev"][i]:
            ev_arr[i] = _f(d.get("ev_arrival_h"), 17.75)
        if d.get("featured_resident_id"):
            featured[str(d["featured_resident_id"])] = i

    injection = None
    for r in docs.get("residents") or []:
        rid = str(r.get("_id"))
        if rid not in featured and r.get("household_id"):
            try:
                idx = int(str(r["household_id"]).removeprefix("hh_")) - 1
                if 0 <= idx < n:
                    featured[rid] = idx
            except ValueError:
                pass
        if r.get("injection_actor"):
            injection = rid

    s = len(section_ids)
    sec_households = np.bincount(section_idx, minlength=s)[:s].astype(np.int64)
    sec_protected = np.array([bool(sec_by_id.get(sid, {}).get("protected", False)) for sid in section_ids])
    sec_quint = np.array([int(_f(sec_by_id.get(sid, {}).get("income_quintile"), 0)) for sid in section_ids],
                         dtype=np.int64)
    for j in range(s):  # dominant quintile from the homes when the section doc lacks it
        if sec_quint[j] <= 0:
            q = quint[section_idx == j]
            sec_quint[j] = int(np.bincount(q, minlength=6).argmax()) if q.size else 3

    businesses: dict[str, dict[str, Any]] = {}
    for d in docs.get("businesses") or []:
        base = _f(d.get("base_load_mw"), 0.0)
        businesses[str(d["_id"])] = {
            "id": str(d["_id"]), "name": d.get("name", str(d["_id"])), "kind": d.get("kind"),
            "max_mw": _f(d.get("max_mw"), 0.0), "notice_min": _f(d.get("notice_min"), 0.0),
            "max_duration_min": _f(d.get("max_duration_min"), 0.0), "protected": bool(d.get("protected")),
            "constraints": d.get("constraints", ""), "curtail_type": d.get("curtail_type"),
            "section_id": d.get("section_id"), "status": "available", "accepted_mw": 0.0,
            "active_from": None, "active_until": None, "base_load_mw": base, "load_mw": base, "curtailed_mw": 0.0,
        }

    facilities: list[dict[str, Any]] = []
    for d in docs.get("facilities") or []:
        flon, flat = _lonlat(d.get("location"))
        facilities.append({
            "_id": str(d["_id"]), "kind": d.get("kind"), "name": d.get("name"), "lon": flon, "lat": flat,
            "designated": bool(d.get("designated")), "section_id": d.get("section_id"), "hours": d.get("hours"),
        })

    wdocs = [w for w in docs.get("weather") or [] if w.get("scenario_id") in (None, scenario.get("id"))]
    scenario["_weather"] = weather.cache_from_docs(wdocs) or weather.synthetic_cache(scenario, 0.0, 24.0)

    start, end = sim_bounds(scenario)
    static = np.random.default_rng([int(seed), 7_002])  # static per-home draws, independent of world.rng
    has_ev, has_bat = b["has_ev"], b["has_battery"]

    def zf() -> np.ndarray:
        return np.zeros(n)

    def zb() -> np.ndarray:
        return np.zeros(n, dtype=bool)

    def zi() -> np.ndarray:
        return np.zeros(n, dtype=np.int64)

    world = World(
        run_id=run_id, mode=mode, scenario=scenario, scale=float(scale or 1.0),
        sim_start=start, sim_end=end, sim_now=start, tick=0, rng=np.random.default_rng(int(seed)),
        hh_ids=hh_ids, lon=lon, lat=lat, section_idx=section_idx, archetype_idx=archetype_idx,
        neighbourhood_idx=neighbourhood_idx, income_quintile=quint, dwelling=dwelling, ac_type=ac_type,
        has_thermostat=b["has_thermostat"], has_ev=has_ev, has_battery=has_bat, vulnerable=b["vulnerable"],
        consent_thermostat=b["consent_thermostat"], consent_ev=b["consent_ev"],
        consent_battery=b["consent_battery"], sms_opt_in=b["sms_opt_in"],
        k_env=k_env, k_ac=k_ac, ac_kw=ac_kw, solar_gain=solar, setpoint_base=setpoint, comfort_max=comfort,
        base_kw_scale=base_scale, ev_arrival_h=ev_arr, featured=featured, injection_resident_id=injection,
        t_in=setpoint.copy(), duty=zf(), p_kw=zf(), power_on=np.ones(n, dtype=bool), offset_c=zf(),
        precool_active=zb(), setback_active=zb(), setback_count_today=zi(), overridden=zb(),
        sms_accepted=zb(), sms_count_today=zi(), sms_stopped=zb(), ev_charging=zb(), ev_paused=zb(),
        ev_kwh_needed=np.where(has_ev, static.uniform(*EV_KWH_RANGE, n), 0.0),
        battery_soc=np.where(has_bat, static.uniform(*BATTERY_SOC_RANGE, n), 0.0),
        battery_discharging=zb(), battery_kw=np.where(has_bat, BATTERY_KW, 0.0),
        notified_cool_space=zb(), notified_sim_minute=np.full(n, -1, dtype=np.int64),
        over_t_safe_minutes=zi(), outage_minutes=zi(),
        section_ids=section_ids, section_protected=sec_protected, section_quintile=sec_quint,
        section_households=sec_households, section_cut=np.zeros(s, dtype=bool), section_cut_until=[None] * s,
        section_cut_minutes_today=np.zeros(s, dtype=np.int64), section_cut_count_today=np.zeros(s, dtype=np.int64),
        section_demand_mw=np.zeros(s), archetype_ids=archetype_ids, archetype_params=archetype_params,
        neighbourhood_ids=nb_ids, businesses=businesses, facilities=facilities,
    )
    if warmup:
        warm_up(world)
    if calibrate and scale is None:
        world.scale = calibrate_scale(world)
    grid.aggregate(world)
    world.metrics["calibration"] = dict(world.metrics.get("calibration", {}), scale=round(world.scale, 3))
    return world


# ---------------------------------------------------------------- warm-up and calibration

def warm_up(world: World, from_hhmm: str = WARMUP_START) -> None:
    """Run thermal physics from early morning to sim_start so indoor temperatures are settled.

    AC homes start at their setpoint; homes without AC start 1 °C above the outdoor temperature.
    Only temperatures and duty carry over; counters stay at zero.
    """
    t0 = datetime.combine(world.sim_start.date(), _hhmm_time(from_hhmm), tzinfo=TZ)
    if t0 >= world.sim_start:
        return
    no_ac = world.ac_type == 0
    world.t_in[no_ac] = weather.outdoor_c(world, t0) + 1.0
    dt_h = clock.dt_hours()
    step = timedelta(minutes=clock.tick_minutes())
    real_now = world.sim_now
    t = t0
    while t < world.sim_start:
        world.sim_now = t
        thermal.step_thermal(world, dt_h, weather.outdoor_c(world, t))
        t += step
    world.sim_now = real_now
    world.p_kw[:] = thermal.household_kw(world, world.sim_now)


def clone_world(world: World, seed: int | None = None) -> World:
    """Independent copy (arrays and mutable state) for look-ahead or calibration."""
    w = copy.copy(world)
    for f in dataclasses.fields(world):
        v = getattr(world, f.name)
        if isinstance(v, np.ndarray):
            setattr(w, f.name, v.copy())
    w.section_cut_until = list(world.section_cut_until)
    w.businesses = copy.deepcopy(world.businesses)
    w.archetype_params = copy.deepcopy(world.archetype_params)
    w.mock = {}
    w.metrics = {}
    w.events = []
    w.rng = np.random.default_rng(seed if seed is not None else [int(world.scenario.get("_seed", 0)), 9_001])
    return w


def peak_time(world: World) -> datetime:
    cal = profiles.calibration(world.scenario)
    return datetime.combine(world.sim_start.date(), _hhmm_time(cal.get("peak_time", "17:00")), tzinfo=TZ)


def uncontrolled_curve(world: World) -> list[tuple[datetime, float, float]]:
    """[(t, residential_sim_mw at scale 1, commercial MW)] per tick over the whole run, no actions."""
    w = clone_world(world)
    dt_h = clock.dt_hours()
    step = timedelta(minutes=clock.tick_minutes())
    out: list[tuple[datetime, float, float]] = []
    while w.sim_now <= w.sim_end:
        physics.world_step(w, dt_h)
        out.append((w.sim_now, float(w.p_kw.sum()) / 1000.0, grid.commercial_background_mw(w)))
        w.sim_now += step
    return out


def calibrate_scale(world: World) -> float:
    """Real homes per simulated home (PLAN §5.2 item 8).

    Default method "max": the uncontrolled zone peak over the run equals target_peak_mw, so the
    scenario's supply_mw (~3% below it) gives the modest evening shortfall PLAN §15 expects even
    when EV arrivals push the residential peak past 17:00. Method "at_peak_time" (scenario
    calibration.method) pins the demand at calibration.peak_time instead.
    """
    cal = profiles.calibration(world.scenario)
    target = _f(world.scenario.get("target_peak_mw"), 4000.0)
    biz = grid.businesses_base_mw(world) if world.businesses else _f(cal.get("businesses_base_mw"), 0.0)
    curve = uncontrolled_curve(world)
    method = str(cal.get("method", "max"))
    if method == "at_peak_time":
        when = peak_time(world)
        cands = [c for c in curve if c[0] >= when][:1] or curve[-1:]
    else:
        cands = curve
    best: tuple[float, datetime, float, float] | None = None
    for t, res, comm in cands:
        if res <= 0:
            continue
        s = (target - comm - biz) / res
        if best is None or s < best[0]:
            best = (s, t, res, comm)
    scale = best[0] if best else float("nan")
    if not math.isfinite(scale) or scale <= 0:
        log.warning("calibration failed (target %.0f MW, biz %.1f MW); using scale 1.0", target, biz)
        scale, best = 1.0, None
    world.metrics["calibration"] = {
        "method": method, "target_peak_mw": target, "businesses_mw": round(biz, 1), "scale": round(scale, 3),
        "peak_time": best[1].strftime("%H:%M") if best else None,
        "residential_sim_mw": round(best[2], 4) if best else None,
        "commercial_mw": round(best[3], 1) if best else None,
    }
    return float(scale)


# ---------------------------------------------------------------- Atlas / files

async def fetch_docs(scenario_id: str) -> dict[str, list[dict]]:
    """Read the world collections from Atlas (geometry is not needed by the sim)."""
    d = db()
    out: dict[str, list[dict]] = {}
    no_geo = {"geometry": 0}
    for key, coll in COLLECTIONS.items():
        proj = no_geo if key in ("sections", "neighbourhoods") else None
        out[key] = await d[coll].find({}, proj).to_list(None)
    out["weather"] = await d[WEATHER].find({"scenario_id": scenario_id}, {"_id": 0}).sort("ts", 1).to_list(None)
    return out


def file_docs(data_dir: Path | None = None) -> dict[str, list[dict]]:
    """The same documents from data/processed/*.json (offline fallback)."""
    base = (data_dir or get_settings().data_dir) / "processed"
    out: dict[str, list[dict]] = {}
    for key in (*COLLECTIONS, "weather"):
        p = base / f"{key}.json"
        if p.exists():
            with p.open() as f:
                out[key] = json.load(f)
        else:
            out[key] = []
    return out


async def load_world(run_id: str, mode: str, scenario: dict, seed: int | None) -> World:
    """Build the run's World from Atlas, falling back to data/processed if Atlas has no households."""
    sid = str(scenario.get("id") or scenario_mod.DEFAULT_SCENARIO_ID)
    docs: dict[str, list[dict]] = {}
    try:
        docs = await fetch_docs(sid)
    except Exception as exc:  # noqa: BLE001 - never block a run on the database
        log.warning("loading world from Atlas failed (%s); trying data/processed", exc)
    if not docs.get("households"):
        log.warning("no households in Atlas; building the world from data/processed/*.json")
        docs = file_docs()
    world = build_world(run_id, mode, scenario, seed, docs)
    log.info("world %s: %d homes, %d sections, scale %.1f", run_id, world.n, len(world.section_ids), world.scale)
    return world
