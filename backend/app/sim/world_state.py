"""In-memory world for one run: numpy arrays in fixed household order (households.idx).

This dataclass is the frozen contract shared by sim/, mock_systems/, governance/, agents/
and the engine (see docs/CONTRACTS.md). Add fields if you need them; never rename them.
sim/loader.py builds it from Atlas. Physics mutates it in place each tick.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import numpy as np

DWELLING = ("house", "apartment", "highrise")  # dwelling array codes 0..2
AC_TYPE = ("none", "window", "central")  # ac_type array codes 0..2


@dataclass
class World:
    run_id: str
    mode: str  # governed | baseline
    scenario: dict[str, Any]  # parsed scenarios/<id>.yaml
    scale: float  # real homes per simulated home
    sim_start: datetime
    sim_end: datetime
    sim_now: datetime
    tick: int
    rng: np.random.Generator

    # ---- static household attributes (length n)
    hh_ids: list[str]  # "hh_00001", index == households.idx
    lon: np.ndarray
    lat: np.ndarray
    section_idx: np.ndarray  # int, index into section_ids
    archetype_idx: np.ndarray  # int, index into archetype_ids
    neighbourhood_idx: np.ndarray  # int, index into neighbourhood_ids
    income_quintile: np.ndarray  # int 1..5
    dwelling: np.ndarray  # int8, see DWELLING
    ac_type: np.ndarray  # int8, see AC_TYPE
    has_thermostat: np.ndarray  # bool (central AC with a ThermLink thermostat)
    has_ev: np.ndarray  # bool
    has_battery: np.ndarray  # bool
    vulnerable: np.ndarray  # bool (synthetic)
    consent_thermostat: np.ndarray  # bool, CoolShift enrolment
    consent_ev: np.ndarray  # bool
    consent_battery: np.ndarray  # bool
    sms_opt_in: np.ndarray  # bool
    k_env: np.ndarray  # 1/h
    k_ac: np.ndarray  # °C/h at full duty
    ac_kw: np.ndarray
    solar_gain: np.ndarray  # °C/h between 11:00 and 18:00
    setpoint_base: np.ndarray  # °C
    comfort_max: np.ndarray  # °C
    base_kw_scale: np.ndarray
    ev_arrival_h: np.ndarray  # hour of day as float, NaN if no EV
    featured: dict[str, int]  # resident id ("res_01") -> household index
    injection_resident_id: str | None

    # ---- dynamic household state (length n)
    t_in: np.ndarray  # indoor °C
    duty: np.ndarray  # AC duty 0..1
    p_kw: np.ndarray  # household load kW this tick
    power_on: np.ndarray  # bool, False while the section is cut
    offset_c: np.ndarray  # applied ThermLink offset (negative = pre-cool, positive = setback)
    precool_active: np.ndarray  # bool
    setback_active: np.ndarray  # bool
    setback_count_today: np.ndarray  # int
    overridden: np.ndarray  # bool, user override during this event (sticky)
    sms_accepted: np.ndarray  # bool, accepted an incentive: AC duty x0.7 for the window
    sms_count_today: np.ndarray  # int
    sms_stopped: np.ndarray  # bool, replied STOP
    ev_charging: np.ndarray  # bool
    ev_paused: np.ndarray  # bool
    ev_kwh_needed: np.ndarray  # float
    battery_soc: np.ndarray  # 0..1
    battery_discharging: np.ndarray  # bool
    battery_kw: np.ndarray  # discharge kW when active
    notified_cool_space: np.ndarray  # bool, R12 pre-notification sent
    notified_sim_minute: np.ndarray  # int, minutes since sim_start when notified, -1 if never
    over_t_safe_minutes: np.ndarray  # int, accumulated minutes with t_in > t_safe
    outage_minutes: np.ndarray  # int, accumulated minutes without power

    # ---- sections (length s)
    section_ids: list[str]  # "TOR-S01".."TOR-S24"
    section_protected: np.ndarray  # bool
    section_quintile: np.ndarray  # int 1..5 (dominant)
    section_households: np.ndarray  # int (simulated)
    section_cut: np.ndarray  # bool
    section_cut_until: list[datetime | None]
    section_cut_minutes_today: np.ndarray  # int
    section_cut_count_today: np.ndarray  # int
    section_demand_mw: np.ndarray  # float, last tick

    # ---- archetypes
    archetype_ids: list[str]
    archetype_params: dict[str, dict[str, float]]  # id -> override_rate_per_hour, offer_acceptance, ev_pause_acceptance

    # ---- other lookups
    neighbourhood_ids: list[str]
    businesses: dict[str, dict[str, Any]]  # id -> {name, max_mw, notice_min, protected, status, accepted_mw, active_from, active_until, load_mw}
    facilities: list[dict[str, Any]]  # {_id, kind, name, lon, lat, designated, section_id}

    # ---- grid (last tick)
    demand_mw: float = 0.0
    supply_mw: float = 0.0
    shortfall_mw: float = 0.0
    contingency_lost_mw: float = 0.0
    over_threshold_ticks: int = 0  # consecutive ticks with shortfall > 3% of supply

    # ---- control
    halted: bool = False
    halted_scope: str = "all"

    # ---- namespaces owned by one workstream each
    mock: dict[str, Any] = field(default_factory=dict)  # mock_systems/: pending commands, batches, quirks
    metrics: dict[str, Any] = field(default_factory=dict)  # sim/metrics.py accumulators
    events: list[dict[str, Any]] = field(default_factory=list)  # world events this tick, drained by the engine

    @property
    def n(self) -> int:
        return len(self.hh_ids)

    @property
    def minutes_elapsed(self) -> int:
        return int((self.sim_now - self.sim_start).total_seconds() // 60)

    def hh_index(self, hh_id: str) -> int:
        return int(hh_id.removeprefix("hh_")) - 1

    def section_index(self, section_id: str) -> int:
        return self.section_ids.index(section_id)
