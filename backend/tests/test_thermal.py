"""PLAN §19 thermal tests plus headless checks of the sim (no Atlas, no data files).

- duty stays in [0, 1]
- an outage raises T_in toward T_out
- pre-cool lowers T_in
- a baseline run 12:00-21:30 shows a shortfall and visible outages, deterministically
- 10,000 homes per tick stays well under 100 ms
"""

import base64
import time
from datetime import timedelta

import numpy as np
import pytest

from app.sim import behavior, clock, grid, headless, loader, metrics, physics, thermal
from app.sim.baseline import BaselineOperator
from app.sim.synthetic import make_world
from app.sim.weather import outdoor_c
from app.timeutil import at


@pytest.fixture
def world():
    return make_world(n=400, seed=11)


def step_hours(w, hours: float) -> None:
    for _ in range(int(round(hours * 60 / clock.tick_minutes()))):
        physics.world_step(w, clock.dt_hours())
        clock.advance(w)


# ---------------------------------------------------------------- PLAN §19

def test_duty_stays_in_unit_interval(world):
    therm = np.flatnonzero(world.has_thermostat)
    world.offset_c[therm[: therm.size // 2]] = -2.0  # pre-cool half
    world.offset_c[therm[therm.size // 2:]] = 2.0  # set back the rest
    world.setback_active[therm[therm.size // 2:]] = True
    world.sms_accepted[::3] = True
    grid.set_section_power(world, 1, False, None)
    for _ in range(int(9.5 * 60 / clock.tick_minutes())):
        physics.world_step(world, clock.dt_hours())
        assert np.all(world.duty >= 0.0) and np.all(world.duty <= 1.0)
        assert np.all(world.duty[~world.power_on] == 0.0)
        assert np.all(world.duty[world.ac_type == 0] == 0.0)
        assert np.isfinite(world.t_in).all()
        clock.advance(world)


def test_outage_raises_tin_toward_tout(world):
    s = 3
    mask = world.section_idx == s
    ac = mask & (world.ac_type > 0)
    assert ac.any()
    t_out = outdoor_c(world, world.sim_now)
    gap0 = t_out - world.t_in[ac]
    before = world.t_in[ac].copy()
    grid.set_section_power(world, s, False, None)
    step_hours(world, 1.0)
    after = world.t_in[ac]
    gap1 = outdoor_c(world, world.sim_now) - after
    assert np.all(after > before)  # every cut home warms
    assert gap1.mean() < gap0.mean()  # and closes on the outdoor temperature
    assert np.all(world.p_kw[mask] == 0.0)
    assert np.all(world.outage_minutes[mask] == 60)
    assert world.section_cut_minutes_today[s] == 60
    # homes with power held their temperature much better
    live = ~mask & (world.ac_type == 2)
    assert (after - before).mean() > (world.t_in[live] - before.mean()).mean()


def test_precool_lowers_tin():
    a = make_world(n=400, seed=5)
    b = make_world(n=400, seed=5)
    therm = np.flatnonzero(a.has_thermostat & a.consent_thermostat)
    assert therm.size > 10
    b.offset_c[therm] = -2.0
    b.precool_active[therm] = True
    step_hours(a, 1.5)
    step_hours(b, 1.5)
    assert b.t_in[therm].mean() < a.t_in[therm].mean() - 0.5
    others = np.setdiff1d(np.arange(a.n), therm)
    assert np.allclose(a.t_in[others], b.t_in[others])  # nobody else is affected


# ---------------------------------------------------------------- thermal helpers

def test_predict_max_tin(world):
    therm = np.flatnonzero(world.has_thermostat)
    start, end = at(world.sim_now, "16:00"), at(world.sim_now, "18:00")
    snapshot = world.t_in.copy()
    base = thermal.predict_max_tin(world, therm, 0.0, start, end)
    setback = thermal.predict_max_tin(world, therm, 2.0, start, end)
    precool = thermal.predict_max_tin(world, therm, -2.0, start, end)
    dark = thermal.predict_max_tin(world, therm, 0.0, start, end, power_off=True)
    assert base.shape == (therm.size,)
    assert np.all(setback >= base - 1e-9)
    assert np.all(precool <= base + 1e-9)
    assert np.all(dark > base)
    assert np.array_equal(world.t_in, snapshot)  # pure
    mask = np.zeros(world.n, dtype=bool)
    mask[therm] = True
    assert np.allclose(thermal.predict_max_tin(world, mask, 0.0, start, end), base)


def test_kw_saved_estimate(world):
    therm = np.flatnonzero(world.has_thermostat)
    setback = thermal.kw_saved_estimate(world, therm, 2.0)
    precool = thermal.kw_saved_estimate(world, therm, -2.0)
    assert setback.shape == precool.shape == (therm.size,)
    assert np.all(setback >= 0) and np.all(precool >= 0)
    assert precool.sum() > 0
    none = np.flatnonzero(~world.has_thermostat)
    assert np.all(thermal.kw_saved_estimate(world, none, 2.0) == 0)


# ---------------------------------------------------------------- behaviour

def test_battery_stops_at_20_percent(world):
    bat = np.flatnonzero(world.has_battery)
    if bat.size == 0:
        world.has_battery[:3] = True
        world.battery_kw[:3] = 5.0
        bat = np.arange(3)
    world.battery_soc[bat] = 0.3
    world.battery_discharging[bat] = True
    step_hours(world, 1.0)
    assert np.all(world.battery_soc[bat] >= 0.2 - 1e-9)
    assert not world.battery_discharging[bat].any()


def test_override_hazard_rises_with_heat():
    assert behavior.override_hazard(0.2, 30.0, 26.0) > behavior.override_hazard(0.2, 26.0, 26.0) > 0


def test_business_sheds_only_after_dispatch_and_notice(world):
    bid = "biz_northline_data_centre"
    b = world.businesses[bid]
    b.update(status="accepted", accepted_mw=15.0)
    behavior.step_businesses(world)
    assert b["curtailed_mw"] == 0.0
    b["status"] = "dispatched"
    behavior.step_businesses(world)
    assert b["curtailed_mw"] == 0.0  # 30 min notice
    world.sim_now += timedelta(minutes=30)
    behavior.step_businesses(world)
    assert b["curtailed_mw"] == 15.0 and b["status"] == "active"


# ---------------------------------------------------------------- grid

def test_calibration_hits_target_peak():
    w = make_world(n=600, seed=3)
    target = float(w.scenario["target_peak_mw"])
    peak = 0.0
    while not clock.is_finished(w):
        physics.world_step(w, clock.dt_hours())
        peak = max(peak, grid.aggregate(w)["demand_mw"])
        clock.advance(w)
    assert abs(peak - target) / target < 0.005


def test_uncontrolled_trip_after_three_ticks(world):
    grid.aggregate(world)
    world.supply_mw, world.shortfall_mw = 1000.0, 50.0  # 5% > 3%
    assert grid.check_uncontrolled_trip(world) is None
    assert grid.check_uncontrolled_trip(world) is None
    ev = grid.check_uncontrolled_trip(world)
    assert ev is not None and ev["payload"]["kind"] == "uncontrolled_trip"
    s = world.section_ids.index(ev["payload"]["section_id"])
    assert world.section_cut[s] and world.metrics["uncontrolled_trips"] == 1
    world.sim_now += timedelta(minutes=90)
    assert grid.release_expired_cuts(world) == [world.section_ids[s]]
    assert not world.section_cut[s] and world.power_on[world.section_idx == s].all()


def test_contingency_drops_supply(world):
    planned = grid.supply_mw(world, world.sim_now)
    assert grid.check_contingencies(world) == []
    world.sim_now = at(world.sim_now, "17:40")
    evs = grid.check_contingencies(world)
    assert len(evs) == 1 and evs[0]["type"] == "contingency"
    assert grid.supply_mw(world, world.sim_now) == pytest.approx(planned - 350.0)
    assert grid.check_contingencies(world) == []  # fires once


def test_agent_cut_not_auto_released(world):
    grid.set_section_power(world, 2, False, None)  # GridOps style
    world.sim_now += timedelta(hours=3)
    assert grid.release_expired_cuts(world) == []
    agg = grid.aggregate(world)
    sid = world.section_ids[2]
    assert agg["sections"][sid] == 0.0
    assert agg["sections_if_energised"][sid] > 0.0  # rebound the Restorer must plan for


# ---------------------------------------------------------------- metrics and map

def test_encode_map(world):
    grid.set_section_power(world, 0, False, None)
    world.overridden[5] = True
    out = metrics.encode_map(world)
    temps = np.frombuffer(base64.b64decode(out["temps"]), dtype=np.uint8)
    flags = np.frombuffer(base64.b64decode(out["flags"]), dtype=np.uint8)
    assert temps.size == flags.size == world.n
    assert np.allclose(temps / 4.0, world.t_in, atol=0.13)
    assert np.array_equal((flags & 1).astype(bool), ~world.power_on)
    assert flags[5] & 2
    assert np.array_equal((flags & 16).astype(bool), world.vulnerable)


# ---------------------------------------------------------------- headless runs

def _baseline_run(seed: int = 21, n: int = 800) -> tuple[dict, object]:
    w = make_world(n=n, seed=seed, mode="baseline")
    return headless.run(w, BaselineOperator(w)), w


def test_headless_baseline_shows_shortfall_and_outages():
    out, w = _baseline_run()
    s = out["summary"]
    assert s["peak_shortfall_mw"] > 0
    assert s["sections_cut_today"] >= 2
    assert s["outage_home_minutes"] > 0 and s["outage_home_minutes_vulnerable"] > 0
    kinds = {e["payload"].get("kind") for e in out["events"]}
    assert {"baseline_setback", "baseline_sms_appeal", "baseline_outage", "baseline_restore_all"} <= kinds
    assert any(e["type"] == "contingency" for e in out["events"])
    # baseline never cuts a protected section itself
    cut_by_baseline = {e["payload"]["section_id"] for e in out["events"] if e["payload"].get("kind") == "baseline_outage"}
    assert not any(w.section_protected[w.section_ids.index(sid)] for sid in cut_by_baseline)
    assert s["rebound_peak_mw"] > 0
    assert w.sim_now == w.sim_end


def test_headless_is_deterministic():
    a, _ = _baseline_run(seed=4, n=300)
    b, _ = _baseline_run(seed=4, n=300)
    assert a["summary"] | {"sim_ts": 0} == b["summary"] | {"sim_ts": 0}
    assert [e["summary"] for e in a["events"]] == [e["summary"] for e in b["events"]]


def test_uncontrolled_run_trips():
    w = make_world(n=600, seed=9, mode="baseline")
    out = headless.run(w, None)
    assert out["summary"]["uncontrolled_trips"] >= 1


def test_ten_thousand_homes_tick_is_fast():
    t0 = time.perf_counter()
    w = make_world(n=10_000, seed=1)
    build_s = time.perf_counter() - t0
    w.sim_now = at(w.sim_now, "17:00")
    grid.set_section_power(w, 3, False, None)
    w.setback_active[w.has_thermostat] = True
    times = []
    for _ in range(12):
        t = time.perf_counter()
        physics.world_step(w, clock.dt_hours())
        grid.aggregate(w)
        metrics.update(w, clock.dt_hours())
        metrics.encode_map(w)
        times.append(time.perf_counter() - t)
        clock.advance(w)
    assert np.median(times) < 0.05, times
    assert build_s < 10


def test_loader_builds_from_documents():
    from app.sim.synthetic import synthetic_docs

    docs = synthetic_docs(200, 2)
    w = loader.build_world("run_docs", "governed", {}, 2, docs)
    assert w.n == 200 and len(w.section_ids) == 24
    assert w.hh_ids[0] == "hh_00001" and w.hh_index("hh_00010") == 9
    assert w.scale > 0 and w.featured and w.injection_resident_id == "res_02"
    assert w.sim_start.strftime("%H:%M") == "12:00" and w.sim_end.strftime("%H:%M") == "21:30"
    assert set(w.archetype_params[w.archetype_ids[0]]) == {"override_rate_per_hour", "offer_acceptance",
                                                          "ev_pause_acceptance"}
    assert np.all(w.consent_thermostat <= w.has_thermostat)
