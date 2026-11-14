"""A small synthetic world for tests and headless checks (no Atlas, no data files).

Documents follow the docs/CONTRACTS.md shapes, so `make_world` exercises the same
`loader.build_world` path as a real run. Deterministic for a given seed.
"""

from typing import Any

import numpy as np

from . import scenario as scenario_mod
from .loader import build_world
from .world_state import World

ARCHETYPES = [
    ("senior_highrise_window", 0.30, 0.15, 0.50),
    ("family_house_central_noev", 0.12, 0.30, 0.75),
    ("family_suburban_central_ev", 0.10, 0.30, 0.80),
    ("young_renter_apartment_window", 0.20, 0.40, 0.70),
    ("affluent_house_central_ev_battery", 0.04, 0.35, 0.85),
    ("low_income_house_noac", 0.15, 0.45, 0.60),
]
BUSINESSES = [
    ("biz_harbourfront_mall", "Harbourfront Mall", 2.0, 0, False, 14.0),
    ("biz_northline_data_centre", "Northline Data Centre", 15.0, 30, False, 45.0),
    ("biz_king_st_office_tower", "King St Office Tower", 3.0, 15, False, 10.0),
    ("biz_port_lands_cold_storage", "Port Lands Cold Storage", 4.0, 15, False, 12.0),
    ("biz_lakeside_campus", "Lakeside Campus", 5.0, 15, False, 22.0),
    ("biz_ashbridge_water_treatment", "Ashbridge Water Treatment", 0.0, 0, True, 8.0),
]


def synthetic_docs(n: int = 400, seed: int = 7, sections: int = 24) -> dict[str, list[dict[str, Any]]]:
    rng = np.random.default_rng([seed, 31])
    sec_ids = [f"TOR-S{i + 1:02d}" for i in range(sections)]
    protected = {i for i in range(sections) if i % 2 == 0}  # half protected, like the real build
    sec_q = rng.integers(1, 6, size=sections)
    households: list[dict[str, Any]] = []
    for i in range(n):
        s = i % sections
        dw = ["house", "apartment", "highrise"][int(rng.integers(0, 3))]
        r = rng.random()
        ac = "central" if r < 0.58 else "window" if r < 0.91 else "none"
        thermostat = ac == "central" and rng.random() < 0.45
        has_ev = rng.random() < 0.10
        has_bat = rng.random() < 0.03
        sp = float(np.clip(rng.normal(23.5, 1.0), 21, 26))
        arch = ARCHETYPES[i % len(ARCHETYPES)][0]
        d: dict[str, Any] = {
            "_id": f"hh_{i + 1:05d}", "idx": i,
            "location": {"type": "Point", "coordinates": [-79.6 + 0.4 * rng.random(), 43.6 + 0.2 * rng.random()]},
            "neighbourhood_id": f"nbh_{s % 6 + 1:03d}", "section_id": sec_ids[s], "archetype_id": arch,
            "dwelling": dw, "ac_type": ac,
            "thermal": {
                "k_env": round(float({"house": 0.30, "apartment": 0.20, "highrise": 0.25}[dw] + rng.normal(0, 0.03)), 4),
                "k_ac": {"central": 4.5, "window": 2.5, "none": 0.0}[ac],
                "ac_kw": {"central": 3.0, "window": 1.0, "none": 0.0}[ac],
                "solar": 0.3 if dw == "house" else 0.2,
            },
            "setpoint_base": round(sp, 2), "comfort_max": round(sp + float(rng.uniform(2, 4)), 2),
            "base_kw_scale": round(float(rng.uniform(0.6, 1.2)), 3),
            "devices": {}, "has_ev": bool(has_ev), "has_battery": bool(has_bat),
            "vulnerable": bool(rng.random() < 0.08), "income_quintile": int(sec_q[s]),
            "consent": {"thermostat_program": bool(thermostat and rng.random() < 0.6),
                        "ev_program": bool(has_ev and rng.random() < 0.5), "battery_program": bool(has_bat),
                        "sms_opt_in": bool(rng.random() < 0.85)},
        }
        if thermostat:
            d["devices"]["thermostat_id"] = f"tl_{i + 1:05d}"
        if has_ev:
            d["devices"]["ev_session_id"] = f"cf_{i + 1:05d}"
            d["ev_arrival_h"] = round(float(np.clip(rng.normal(17.75, 0.67), 15.5, 21.0)), 3)
        if has_bat:
            d["devices"]["battery_id"] = f"pc_{i + 1:05d}"
        if i < 5:
            d["featured_resident_id"] = f"res_{i + 1:02d}"
        households.append(d)
    sec_docs = []
    for s, sid in enumerate(sec_ids):
        members = [h for h in households if h["section_id"] == sid]
        sec_docs.append({
            "_id": sid, "households": len(members), "vulnerable": sum(h["vulnerable"] for h in members),
            "income_quintile": int(sec_q[s]), "protected": s in protected,
            "protected_reasons": ["Designated cool space (synthetic)"] if s in protected else [],
        })
    return {
        "households": households,
        "sections": sec_docs,
        "archetypes": [{"_id": a, "description": a.replace("_", " "),
                        "defaults": {"override_rate_per_hour": o, "offer_acceptance": acc, "ev_pause_acceptance": ev}}
                       for a, o, acc, ev in ARCHETYPES],
        "businesses": [{"_id": b, "name": nm, "max_mw": mx, "notice_min": nt, "protected": pr, "base_load_mw": base,
                        "section_id": sec_ids[(k * 3 + 1) % sections]}
                       for k, (b, nm, mx, nt, pr, base) in enumerate(BUSINESSES)],
        "facilities": [{"_id": f"fac_cool_{k:03d}", "kind": "cool_space", "name": f"Community Centre {k + 1}",
                        "location": {"type": "Point", "coordinates": [-79.5 + 0.02 * k, 43.65]},
                        "designated": True, "section_id": sec_ids[k * 2 % sections]} for k in range(6)],
        "neighbourhoods": [{"_id": f"nbh_{k:03d}", "name": f"Neighbourhood {k}"} for k in range(1, 7)],
        "residents": [{"_id": f"res_{k:02d}", "household_id": f"hh_{k:05d}", "injection_actor": k == 2}
                      for k in range(1, 6)],
        "weather": [],  # synthetic heat-wave curve + scenario delta_c
    }


def make_world(n: int = 400, seed: int = 7, mode: str = "governed", scenario: dict | None = None,
               run_id: str = "run_synth", **kw: Any) -> World:
    """A ready-to-run synthetic World at sim_start (warmed up and calibrated by default)."""
    sc = scenario if scenario is not None else scenario_mod.with_defaults({})
    return build_world(run_id, mode, sc, seed, synthetic_docs(n, seed), **kw)
