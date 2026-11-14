"""Build the synthetic Toronto world into data/processed/*.json (PLAN §5.2, §5.3).

Inputs are the raw files from scripts/fetch_data.py; every input has a seeded synthetic fallback,
so this runs offline. Honours SIM_HOUSEHOLDS and SIM_SEED. Output shapes are pinned in
docs/CONTRACTS.md "Data files"; scripts/init_db.py loads them into Atlas.

Run from backend/:  .venv/bin/python -m scripts.build_world
"""

import csv
import json
import math
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from shutil import copyfile
from typing import Any

import numpy as np
import shapely
import yaml
from shapely.geometry import MultiPoint, MultiPolygon, Point, Polygon, box, mapping, shape
from shapely.geometry.polygon import orient
from sklearn.cluster import KMeans

from app.config import ROOT, get_settings
from app.governance.identities import seed_policies
from app.timeutil import TZ, sim_iso
from .build_observations import build_observations, hydro_lines

SCENARIO_ID = "heatwave_2031"
N_SECTIONS = 24
MIN_ELIGIBLE = 12
# Demo beats (PLAN §15, §17) name these sections; they must stay eligible for outages.
DEMO_ELIGIBLE = ("TOR-S03", "TOR-S07", "TOR-S11")
TORONTO_BBOX = (-79.6393, 43.5810, -79.1152, 43.8555)  # lon_min, lat_min, lon_max, lat_max
LON0, LAT0 = -79.38, 43.70
KM_X = 111.320 * math.cos(math.radians(LAT0))
KM_Y = 110.574

# ---------------------------------------------------------------- synthetic assumption tables
# P(ac_type) by dwelling at income quintile 1; central gains `central_per_q` per quintile step,
# taken from "none" first, then "window".
AC_TABLE = {
    "house": {"central": 0.55, "window": 0.30, "central_per_q": 0.08},
    "apartment": {"central": 0.22, "window": 0.50, "central_per_q": 0.05},
    "highrise": {"central": 0.45, "window": 0.35, "central_per_q": 0.06},
}
K_ENV = {"house": 0.30, "apartment": 0.20, "highrise": 0.25}
K_AC = {"central": 4.5, "window": 2.5, "none": 0.0}
AC_KW = {"central": 3.0, "window": 1.0, "none": 0.0}
SOLAR = {"house": 0.3, "apartment": 0.2, "highrise": 0.2}
P_SMART_THERMOSTAT = 0.45
P_THERMOSTAT_CONSENT = 0.40
P_EV_CONSENT = 0.50
P_BATTERY = 0.02
P_SMS_OPT_IN = 0.85
EV_KW = 7.2
LIFESTYLES = ("none", "student", "newcomer", "shift", "wfh")
LIFESTYLE_P = (0.52, 0.08, 0.10, 0.10, 0.20)

# 15 rule-classified archetypes (PLAN §7.1) with defaults used when the LLM is unavailable.
ARCHETYPES: list[dict[str, Any]] = [
    {"_id": "affluent_house_central_ev_battery", "label": "Affluent house, central AC, EV + battery",
     "description": "High-income detached household with central AC, an EV and a home battery. Engaged with energy programs, low override rate.",
     "defaults": {"override_rate_per_hour": 0.04, "offer_acceptance": 0.35, "ev_pause_acceptance": 0.85}},
    {"_id": "senior_highrise_window", "label": "Senior, high-rise, window AC",
     "description": "Older resident in an apartment tower cooled by a window unit. Stays home most of the day and is sensitive to indoor heat.",
     "defaults": {"override_rate_per_hour": 0.30, "offer_acceptance": 0.15, "ev_pause_acceptance": 0.50}},
    {"_id": "senior_highrise_central", "label": "Senior, high-rise, central AC",
     "description": "Older resident in a condo or apartment tower with central air. At home in the afternoon, cautious about warm rooms.",
     "defaults": {"override_rate_per_hour": 0.25, "offer_acceptance": 0.20, "ev_pause_acceptance": 0.50}},
    {"_id": "senior_house_central", "label": "Senior living alone, house, central AC",
     "description": "Older homeowner living alone in a detached or semi-detached house with central air. Home all day.",
     "defaults": {"override_rate_per_hour": 0.22, "offer_acceptance": 0.20, "ev_pause_acceptance": 0.60}},
    {"_id": "retiree_couple_house_central", "label": "Retiree couple, house, central AC",
     "description": "Retired couple in a house with central air. Budget-minded and willing to help if comfortable.",
     "defaults": {"override_rate_per_hour": 0.15, "offer_acceptance": 0.35, "ev_pause_acceptance": 0.70}},
    {"_id": "low_income_house_noac", "label": "Lower-income house, no AC",
     "description": "Lower-income household in an older house without air conditioning. Relies on fans and nearby cool spaces.",
     "defaults": {"override_rate_per_hour": 0.0, "offer_acceptance": 0.40, "ev_pause_acceptance": 0.60}},
    {"_id": "low_income_apartment_noac", "label": "Lower-income apartment, no AC",
     "description": "Lower-income renter in an apartment without air conditioning. Sensitive to the bill, uses fans.",
     "defaults": {"override_rate_per_hour": 0.0, "offer_acceptance": 0.45, "ev_pause_acceptance": 0.60}},
    {"_id": "family_suburban_central_ev", "label": "Suburban family, central AC, EV",
     "description": "Family in a suburban house with central air and an EV that plugs in after the commute.",
     "defaults": {"override_rate_per_hour": 0.12, "offer_acceptance": 0.30, "ev_pause_acceptance": 0.75}},
    {"_id": "family_house_central_noev", "label": "Family house, central AC, no EV",
     "description": "Family in a house with central air and children home in the afternoon. Moderately price-sensitive.",
     "defaults": {"override_rate_per_hour": 0.14, "offer_acceptance": 0.30, "ev_pause_acceptance": 0.70}},
    {"_id": "wfh_professional_central", "label": "Work-from-home professional, central AC",
     "description": "Professional working from home with central air. Values comfort during calls, responds to clear timing.",
     "defaults": {"override_rate_per_hour": 0.18, "offer_acceptance": 0.25, "ev_pause_acceptance": 0.80}},
    {"_id": "shift_worker_apartment", "label": "Shift worker, apartment",
     "description": "Shift worker in an apartment who sleeps during the day. Often misses messages until evening.",
     "defaults": {"override_rate_per_hour": 0.16, "offer_acceptance": 0.25, "ev_pause_acceptance": 0.65}},
    {"_id": "student_shared_window", "label": "Students in shared rental, window AC",
     "description": "Students sharing a rental cooled by a window unit. Price-sensitive and quick to reply.",
     "defaults": {"override_rate_per_hour": 0.10, "offer_acceptance": 0.55, "ev_pause_acceptance": 0.60}},
    {"_id": "newcomer_family_apartment", "label": "Newcomer family, apartment",
     "description": "Newcomer family in a rental apartment. Price-sensitive; prefers short, plain messages.",
     "defaults": {"override_rate_per_hour": 0.12, "offer_acceptance": 0.45, "ev_pause_acceptance": 0.60}},
    {"_id": "young_renter_apartment_window", "label": "Young renter, apartment, window AC",
     "description": "Young renter in an apartment with a window unit, out much of the day. Open to small rebates.",
     "defaults": {"override_rate_per_hour": 0.10, "offer_acceptance": 0.40, "ev_pause_acceptance": 0.70}},
    {"_id": "condo_professional_central", "label": "Condo professional, central AC",
     "description": "Working professional in a condo tower with central air, usually home after 18:00.",
     "defaults": {"override_rate_per_hour": 0.10, "offer_acceptance": 0.30, "ev_pause_acceptance": 0.80}},
]
ARCH_IDS = [a["_id"] for a in ARCHETYPES]

BUSINESSES: list[dict[str, Any]] = [
    {"_id": "biz_harbourfront_mall", "name": "Harbourfront Mall", "kind": "retail", "max_mw": 2.0, "notice_min": 0,
     "max_duration_min": 240, "protected": False, "base_load_mw": 14.0, "curtail_type": "dim_lighting",
     "constraints": "Designated cool space. Can dim lighting by 2 MW; must keep cooling.", "lon": -79.3812, "lat": 43.6393},
    {"_id": "biz_northline_data_centre", "name": "Northline Data Centre", "kind": "data_centre", "max_mw": 15.0,
     "notice_min": 30, "max_duration_min": 240, "protected": False, "base_load_mw": 45.0, "curtail_type": "shift_batch_load",
     "constraints": "Can shift 15 MW of batch load; needs 30 min notice.", "lon": -79.5850, "lat": 43.7250},
    {"_id": "biz_king_st_office_tower", "name": "King St Office Tower", "kind": "office", "max_mw": 3.0, "notice_min": 15,
     "max_duration_min": 180, "protected": False, "base_load_mw": 10.0, "curtail_type": "precool_then_setback",
     "constraints": "Pre-cool then setback, 3 MW.", "lon": -79.3850, "lat": 43.6487},
    {"_id": "biz_port_lands_cold_storage", "name": "Port Lands Cold Storage", "kind": "cold_storage", "max_mw": 4.0,
     "notice_min": 15, "max_duration_min": 90, "protected": False, "base_load_mw": 12.0, "curtail_type": "prechill_then_coast",
     "constraints": "Pre-chill then coast, 4 MW for 90 min.", "lon": -79.3400, "lat": 43.6480},
    {"_id": "biz_lakeside_campus", "name": "Lakeside Campus", "kind": "campus", "max_mw": 5.0, "notice_min": 15,
     "max_duration_min": 180, "protected": False, "base_load_mw": 22.0, "curtail_type": "campus_curtail",
     "constraints": "Can curtail 5 MW across campus buildings.", "lon": -79.5150, "lat": 43.5985},
    {"_id": "biz_ashbridge_water_treatment", "name": "Ashbridge Water Treatment", "kind": "water_treatment", "max_mw": 0.0,
     "notice_min": 0, "max_duration_min": 0, "protected": True, "base_load_mw": 8.0, "curtail_type": "none",
     "constraints": "Protected critical service, 0 MW.", "lon": -79.3160, "lat": 43.6610},
]

FIRST_NAMES = ["Amara", "Bao", "Carmen", "Dev", "Elena", "Farid", "Grace", "Hiro", "Imani", "Jonah", "Kavya", "Liam",
               "Mei", "Nadia", "Omar", "Priya", "Quinn", "Rosa", "Samir", "Tess", "Uche", "Vera", "Wes", "Xin",
               "Yusuf", "Zara", "Aldo", "Bianca", "Chidi", "Dana", "Emeka", "Fiona", "Gus", "Hana", "Ivan", "Joy"]
LAST_NAMES = ["Abara", "Brightwater", "Castellan", "Dunmore", "Esterhay", "Fallowfield", "Grisham", "Halloway",
              "Ingleby", "Juniper", "Kestrel", "Larkspur", "Merriweather", "Northcote", "Oakhurst", "Pellham",
              "Quarrington", "Ravensdale", "Silverbirch", "Thistlewood", "Underhill", "Vantongeren", "Whitlock",
              "Yarborough", "Zelinski", "Ashgrove", "Brambleton", "Corriveau", "Delacroix", "Elmsworth"]
PERSONA_T = {
    "affluent_house_central_ev_battery": (["45-54", "55-64"], ["Lives with a partner and two teenagers"], ["architect", "hospital administrator", "software director"]),
    "senior_highrise_window": (["75+", "65-74"], ["Lives alone on the 14th floor", "Lives alone in a one-bedroom unit"], ["retired transit operator", "retired seamstress", "retired bookkeeper"]),
    "senior_highrise_central": (["65-74", "75+"], ["Lives alone in a condo", "Lives with a sister in a two-bedroom unit"], ["retired teacher", "retired pharmacist's assistant", "retired civil servant"]),
    "senior_house_central": (["75+", "65-74"], ["Lives alone in the family home"], ["retired machinist", "retired nurse educator", "retired postal worker"]),
    "retiree_couple_house_central": (["65-74"], ["Lives with a spouse; grandchildren visit on weekends"], ["retired engineer", "retired shop owner", "part-time library volunteer"]),
    "low_income_house_noac": (["35-44", "45-54"], ["Lives with two kids and a parent", "Shares the house with a cousin's family"], ["warehouse associate", "home-care aide", "line cook"]),
    "low_income_apartment_noac": (["25-34", "55-64"], ["Lives with a roommate", "Lives alone"], ["retail cashier", "cleaner", "delivery driver"]),
    "family_suburban_central_ev": (["35-44"], ["Lives with a partner and two young kids"], ["nurse manager", "electrician", "account manager"]),
    "family_house_central_noev": (["35-44", "45-54"], ["Lives with a partner and three kids"], ["school principal", "plumber", "bank teller"]),
    "wfh_professional_central": (["25-34", "35-44"], ["Lives with a partner", "Lives alone with a dog"], ["data analyst", "UX designer", "translator"]),
    "shift_worker_apartment": (["25-34", "35-44"], ["Lives alone", "Lives with a partner who also works shifts"], ["night-shift security guard", "paramedic dispatcher", "baker"]),
    "student_shared_window": (["18-24"], ["Shares a rental with three classmates"], ["engineering student", "nursing student", "music student"]),
    "newcomer_family_apartment": (["25-34", "35-44"], ["Lives with a spouse and two kids; arrived two years ago"], ["rideshare driver", "lab technician", "daycare assistant"]),
    "young_renter_apartment_window": (["25-34"], ["Lives alone in a basement-free walk-up", "Lives with a roommate"], ["barista", "junior accountant", "graphic designer"]),
    "condo_professional_central": (["25-34", "35-44"], ["Lives alone in a condo", "Lives with a partner in a condo"], ["lawyer", "marketing manager", "project coordinator"]),
}
ATTITUDES = ["cost-conscious and likes small rebates", "sceptical of utility programs", "keen to help the grid",
             "replies quickly to texts", "prefers short, plain messages", "values comfort over savings",
             "follows the news about the heat wave", "wants to know exact timing before agreeing",
             "worries about the bill this summer", "proud to be an early adopter"]


# ---------------------------------------------------------------- helpers
def to_km(lon: np.ndarray, lat: np.ndarray) -> np.ndarray:
    return np.column_stack([(np.asarray(lon) - LON0) * KM_X, (np.asarray(lat) - LAT0) * KM_Y])


def km_to_lonlat(xy: np.ndarray) -> np.ndarray:
    return np.column_stack([xy[:, 0] / KM_X + LON0, xy[:, 1] / KM_Y + LAT0])


def clean_polygonal(geom) -> Polygon | MultiPolygon:
    """make_valid -> polygon parts only -> CCW exterior rings (valid for 2dsphere)."""
    geom = shapely.make_valid(geom)
    polys: list[Polygon] = []
    for g in getattr(geom, "geoms", [geom]):
        if isinstance(g, Polygon) and g.area > 0:
            polys.append(g)
        elif isinstance(g, MultiPolygon):
            polys.extend(p for p in g.geoms if p.area > 0)
    polys = [orient(p, 1.0) for p in polys]
    if not polys:
        raise ValueError("empty geometry")
    return polys[0] if len(polys) == 1 else MultiPolygon(polys)


def geojson(geom) -> dict:
    """GeoJSON dict with 6-decimal coordinates, consecutive duplicate vertices removed."""
    g = shapely.set_precision(geom, 1e-6) if not isinstance(geom, Point) else geom
    if not isinstance(g, Point):
        g = clean_polygonal(g)
    return json.loads(json.dumps(mapping(g)))


def pt(lon: float, lat: float) -> dict:
    return {"type": "Point", "coordinates": [round(float(lon), 6), round(float(lat), 6)]}


def to_num(v: Any) -> float | None:
    if v is None:
        return None
    try:
        return float(str(v).replace(",", "").replace("$", "").strip())
    except ValueError:
        return None


def jsonable(o: Any) -> Any:
    if isinstance(o, dict):
        return {k: jsonable(v) for k, v in o.items()}
    if isinstance(o, list | tuple):
        return [jsonable(v) for v in o]
    if isinstance(o, np.bool_):
        return bool(o)
    if isinstance(o, np.integer):
        return int(o)
    if isinstance(o, np.floating):
        return float(o)
    if isinstance(o, date | datetime):
        return o.isoformat()
    return o


def interp_hhmm(profile: dict[str, float], hour: float) -> float:
    xs = [int(k[:2]) + int(k[3:]) / 60 for k in profile]
    ys = list(profile.values())
    order = np.argsort(xs)
    return float(np.interp(hour, np.array(xs)[order], np.array(ys)[order]))


# ---------------------------------------------------------------- inputs
def load_sources(raw: Path) -> dict[str, bool]:
    try:
        return {r["name"]: bool(r["ok"]) for r in json.loads((raw / "sources.json").read_text())}
    except Exception:  # noqa: BLE001
        return {}


def load_neighbourhoods(raw: Path, rng: np.random.Generator) -> tuple[list[dict], bool]:
    """[{id, name, code, geom}]; falls back to 12 synthetic zones."""
    try:
        fc = json.loads((raw / "neighbourhoods.geojson").read_text())
        out = []
        for f in fc["features"]:
            p = f["properties"]
            name = p.get("AREA_NAME") or p.get("area_name") or p.get("AREA_DESC")
            code = int(p.get("AREA_SHORT_CODE") or p.get("area_short_code") or p.get("AREA_LONG_CODE"))
            geom = clean_polygonal(shape(f["geometry"]).simplify(0.00005, preserve_topology=True))
            out.append({"id": f"nbh_{code:03d}", "name": name, "code": code, "geom": geom})
        if len(out) < 50:
            raise ValueError("too few neighbourhoods")
        return sorted(out, key=lambda n: n["code"]), True
    except Exception as e:  # noqa: BLE001
        print(f"neighbourhoods: synthetic fallback ({e})")
    lon0, lat0, lon1, lat1 = TORONTO_BBOX
    out = []
    for i in range(12):
        r, c = divmod(i, 4)
        x0 = lon0 + (lon1 - lon0) * c / 4
        y0 = lat0 + 0.02 + (lat1 - lat0 - 0.02) * r / 3
        g = box(x0, y0, x0 + (lon1 - lon0) / 4, y0 + (lat1 - lat0 - 0.02) / 3)
        out.append({"id": f"nbh_{i + 1:03d}", "name": f"Zone {i + 1:02d} (synthetic)", "code": i + 1, "geom": orient(g, 1.0)})
    return out, False


PROFILE_LABELS = {
    "population": "Total - Age groups",
    "pop_65": "65 years and over",
    "households": "Total - Private households by household size",
    "median_hh_income": "Median total income of household",
    "single_detached": "Single-detached house",
    "apartment_5plus": "Apartment in a building that has five or more storeys",
    "renter": "Renter",
}


def load_profiles(raw: Path) -> dict[int, dict[str, float]]:
    """Neighbourhood number -> raw counts, by case-insensitive label match (exact first, then substring)."""
    import openpyxl

    wb = openpyxl.load_workbook(raw / "neighbourhood_profiles_2021.xlsx", read_only=True, data_only=True)
    ws = wb[wb.sheetnames[0]]
    rows = [r for r in ws.iter_rows(values_only=True)]
    numbers: list[int | None] = []
    for r in rows[:5]:
        if str(r[0]).strip().lower().startswith("neighbourhood number"):
            numbers = [int(v) if to_num(v) is not None else None for v in r[1:]]
    if not numbers:
        raise ValueError("no 'Neighbourhood Number' row")
    labels = [str(r[0] or "").strip().lower() for r in rows]
    found: dict[str, tuple] = {}
    for key, label in PROFILE_LABELS.items():
        lab = label.lower()
        idx = next((i for i, x in enumerate(labels) if x == lab), None)
        if idx is None:
            idx = next((i for i, x in enumerate(labels) if lab in x), None)
        if idx is not None:
            found[key] = rows[idx][1:]
    out: dict[int, dict[str, float]] = {}
    for j, num in enumerate(numbers):
        if num is None:
            continue
        out[num] = {k: to_num(v[j]) for k, v in found.items() if j < len(v)}
    return out


def neighbourhood_attrs(nbhs: list[dict], raw: Path, rng: np.random.Generator) -> tuple[bool, int | None]:
    """Fill modelling attributes and return the census household total when every count is present."""
    profiles: dict[int, dict] = {}
    ok = True
    try:
        profiles = load_profiles(raw)
    except Exception as e:  # noqa: BLE001
        print(f"profiles: synthetic fallback ({e})")
        ok = False
    rows = []
    census_households = []
    for n in nbhs:
        p = profiles.get(n["code"], {}) if ok else {}
        hh = p.get("households")
        census_households.append(hh)
        pop = p.get("population")

        def frac(key: str) -> float | None:
            v = p.get(key)
            base = pop if key == "pop_65" else hh
            return v / base if v is not None and base else None

        rows.append({
            "population": pop, "households": hh, "pct_65_plus": frac("pop_65"),
            "median_hh_income": p.get("median_hh_income"), "pct_apartment_5plus": frac("apartment_5plus"),
            "pct_single_detached": frac("single_detached"), "pct_renters": frac("renter"),
        })
    if not ok or sum(r["households"] is not None for r in rows) < len(rows) // 2:
        ok = False
        for r in rows:
            r.update(population=float(rng.integers(8000, 40000)), pct_65_plus=float(rng.uniform(0.10, 0.25)),
                     median_hh_income=float(rng.normal(85000, 25000)), pct_apartment_5plus=float(rng.uniform(0.05, 0.7)),
                     pct_single_detached=float(rng.uniform(0.05, 0.6)), pct_renters=float(rng.uniform(0.2, 0.7)))
            r["households"] = round(r["population"] / 2.4)
            r["pct_single_detached"] = min(r["pct_single_detached"], 1 - r["pct_apartment_5plus"])
    # missing value -> city-wide median
    for k in rows[0]:
        vals = [r[k] for r in rows if r[k] is not None]
        med = float(np.median(vals)) if vals else 0.0
        for r in rows:
            if r[k] is None:
                r[k] = med
    incomes = np.array([r["median_hh_income"] for r in rows])
    ranks = incomes.argsort().argsort()
    quint = (ranks * 5 // len(rows)) + 1
    for n, r, q in zip(nbhs, rows, quint, strict=True):
        n["attrs"] = {
            "population": int(round(r["population"])), "households": int(round(r["households"])),
            "pct_65_plus": round(r["pct_65_plus"], 4), "median_hh_income": int(round(r["median_hh_income"])),
            "pct_apartment_5plus": round(r["pct_apartment_5plus"], 4),
            "pct_single_detached": round(r["pct_single_detached"], 4), "pct_renters": round(r["pct_renters"], 4),
        }
        n["income_quintile"] = int(q)
    census_total = sum(int(count) for count in census_households if count is not None)
    return ok, census_total if ok and None not in census_households else None


# ---------------------------------------------------------------- households
def allocate(weights: np.ndarray, total: int) -> np.ndarray:
    exact = weights / weights.sum() * total
    counts = np.floor(exact).astype(int)
    rem = total - counts.sum()
    counts[np.argsort(-(exact - counts))[:rem]] += 1
    return counts


def sample_points(geom, k: int, rng: np.random.Generator) -> np.ndarray:
    """k random points inside geom by rejection sampling over its bbox."""
    if k == 0:
        return np.zeros((0, 2))
    x0, y0, x1, y1 = geom.bounds
    shapely.prepare(geom)
    got: list[np.ndarray] = []
    n_have = 0
    for _ in range(200):
        xs = rng.uniform(x0, x1, k * 4)
        ys = rng.uniform(y0, y1, k * 4)
        inside = shapely.contains_xy(geom, xs, ys)
        pts = np.column_stack([xs[inside], ys[inside]])
        got.append(pts)
        n_have += len(pts)
        if n_have >= k:
            break
    pts = np.vstack(got)[:k]
    if len(pts) < k:  # tiny or degenerate polygon: fall back to its representative point
        rp = geom.representative_point()
        pts = np.vstack([pts, np.tile([rp.x, rp.y], (k - len(pts), 1))])
    return pts


def classify(dw: str, ac: str, ev: bool, battery: bool, senior: bool, alone: bool, q: int, renter: bool, life: str) -> str:
    """Rule-based archetype (PLAN §7.1). Order matters: first match wins."""
    flat = dw in ("apartment", "highrise")
    if battery or (dw == "house" and ac == "central" and ev and q >= 4):
        return "affluent_house_central_ev_battery"
    if senior:
        if flat:
            return "senior_highrise_central" if ac == "central" else "senior_highrise_window"
        if ac == "central":
            return "senior_house_central" if alone else "retiree_couple_house_central"
        return "low_income_house_noac" if ac == "none" else "senior_highrise_window"
    if ac == "none":
        return "low_income_house_noac" if dw == "house" else "low_income_apartment_noac"
    if life == "student" and ac == "window":
        return "student_shared_window"
    if life == "newcomer" and flat:
        return "newcomer_family_apartment"
    if life == "shift" and flat:
        return "shift_worker_apartment"
    if life == "wfh" and ac == "central":
        return "wfh_professional_central"
    if dw == "house" and ac == "central":
        return "family_suburban_central_ev" if ev else "family_house_central_noev"
    if flat and ac == "window":
        return "young_renter_apartment_window" if (renter or q <= 3) else "condo_professional_central"
    if flat and ac == "central":
        return "condo_professional_central"
    return "young_renter_apartment_window" if ac == "window" else "family_house_central_noev"


def build_households(nbhs: list[dict], n_total: int, rng: np.random.Generator) -> dict[str, np.ndarray]:
    weights = np.array([max(n["attrs"]["households"], 1) for n in nbhs], dtype=float)
    counts = allocate(weights, n_total)
    lon, lat, nb_idx = [], [], []
    for i, (n, k) in enumerate(zip(nbhs, counts, strict=True)):
        pts = sample_points(n["geom"], int(k), rng)
        lon.append(pts[:, 0])
        lat.append(pts[:, 1])
        nb_idx.append(np.full(len(pts), i))
    H: dict[str, Any] = {"lon": np.concatenate(lon), "lat": np.concatenate(lat), "nb": np.concatenate(nb_idx)}
    n = len(H["lon"])
    a = [nbhs[i]["attrs"] for i in H["nb"]]
    p65 = np.array([x["pct_65_plus"] for x in a])
    sd = np.array([x["pct_single_detached"] for x in a])
    apt5 = np.array([x["pct_apartment_5plus"] for x in a])
    rent = np.array([x["pct_renters"] for x in a])
    q = np.array([nbhs[i]["income_quintile"] for i in H["nb"]])

    other = np.clip(1 - sd - apt5, 0, 1)
    p_house = sd + 0.5 * other
    p_apart = 0.5 * other
    p_high = apt5
    tot = p_house + p_apart + p_high
    u = rng.random(n)
    dwelling = np.where(u < p_house / tot, "house", np.where(u < (p_house + p_apart) / tot, "apartment", "highrise"))

    ac = np.empty(n, dtype=object)
    u = rng.random(n)
    for i in range(n):
        t = AC_TABLE[dwelling[i]]
        pc = min(0.95, t["central"] + t["central_per_q"] * (q[i] - 1))
        pw = min(t["window"], 1 - pc)
        ac[i] = "central" if u[i] < pc else ("window" if u[i] < pc + pw else "none")
    ac = ac.astype(str)

    has_thermostat = (ac == "central") & (rng.random(n) < P_SMART_THERMOSTAT)
    c_thermo = has_thermostat & (rng.random(n) < P_THERMOSTAT_CONSENT)
    has_ev = rng.random(n) < (0.03 + 0.03 * (q - 1))
    ev_arrival = np.where(has_ev, rng.normal(17.75, 40 / 60, n), np.nan)
    c_ev = has_ev & (rng.random(n) < P_EV_CONSENT)
    has_batt = rng.random(n) < P_BATTERY
    sms = rng.random(n) < P_SMS_OPT_IN

    # Synthetic household-head age and vulnerability. Overall P(vulnerable) = 0.35*pct_65_plus + 0.01
    # (PLAN §5.2), concentrated in senior-headed households.
    p_senior = np.clip(1.4 * p65, 0.02, 0.6)
    senior = rng.random(n) < p_senior
    v = 0.35 * p65 + 0.01
    pv_senior = np.minimum(0.95, 0.8 * v / p_senior)
    pv_other = np.clip((v - p_senior * pv_senior) / (1 - p_senior), 0, 1)
    vulnerable = rng.random(n) < np.where(senior, pv_senior, pv_other)
    alone = rng.random(n) < np.where(senior, 0.5, 0.3)
    renter = rng.random(n) < rent
    life = rng.choice(LIFESTYLES, size=n, p=LIFESTYLE_P)

    k_env = np.clip(np.array([K_ENV[d] for d in dwelling]) + rng.normal(0, 0.03, n), 0.08, 0.5)
    k_ac = np.array([K_AC[x] for x in ac])
    ac_kw = np.array([AC_KW[x] for x in ac])
    solar = np.array([SOLAR[d] for d in dwelling])
    setpoint = np.clip(rng.normal(23.5, 1.0, n), 21, 26)
    comfort = setpoint + rng.uniform(2, 4, n)
    base_scale = rng.uniform(0.6, 1.2, n)

    arch = np.array([classify(dwelling[i], ac[i], bool(has_ev[i]), bool(has_batt[i]), bool(senior[i]),
                              bool(alone[i]), int(q[i]), bool(renter[i]), life[i]) for i in range(n)])
    H.update(dwelling=dwelling, ac=ac, has_thermostat=has_thermostat, c_thermo=c_thermo, has_ev=has_ev,
             ev_arrival=ev_arrival, c_ev=c_ev, has_batt=has_batt, sms=sms, senior=senior, alone=alone,
             vulnerable=vulnerable, renter=renter, life=life, k_env=k_env, k_ac=k_ac, ac_kw=ac_kw, solar=solar,
             setpoint=setpoint, comfort=comfort, base_scale=base_scale, arch=arch, q=q)
    return H


# ---------------------------------------------------------------- sections, facilities, protection
def build_sections(H: dict, seed: int) -> tuple[np.ndarray, list[dict], KMeans]:
    """KMeans(k=24) on household coordinates; hull buffered ~150 m. Returns (label per home, raw clusters, model)."""
    X = to_km(H["lon"], H["lat"])
    k = min(N_SECTIONS, len(X))
    km = KMeans(n_clusters=k, n_init=10, random_state=seed).fit(X)
    clusters = []
    for c in range(k):
        m = km.labels_ == c
        hull = MultiPoint(X[m]).convex_hull.buffer(0.15, quad_segs=6)
        ring = km_to_lonlat(np.asarray(hull.exterior.coords))
        geom = orient(Polygon(ring), 1.0)
        clusters.append({"c": c, "mask": m, "geom": geom, "cx": float(H["lon"][m].mean()), "cy": float(H["lat"][m].mean())})
    return km.labels_, clusters, km


def load_cool_spaces(raw: Path) -> tuple[list[dict], bool]:
    try:
        fc = json.loads((raw / "heat_relief_network.geojson").read_text())
        out = []
        for f in fc["features"]:
            p = f["properties"]
            g = f.get("geometry") or {}
            coords = g.get("coordinates")
            if not coords:
                continue
            lon, lat = (coords[0] if g["type"] == "MultiPoint" else coords)[:2]
            kind = (p.get("locationDesc") or p.get("locationTypeDesc") or "").strip()
            if kind not in ("Community Centre", "Indoor Pool", "Library"):
                continue  # splash pads and outdoor pools are not indoor cool spaces
            out.append({"name": (p.get("locationName") or "Cool space").strip(), "kind_desc": kind,
                        "address": (p.get("address") or "").strip(), "lon": float(lon), "lat": float(lat)})
        if len(out) < 12:
            raise ValueError("too few indoor cool spaces")
        return out, True
    except Exception as e:  # noqa: BLE001
        print(f"cool spaces: synthetic fallback ({e})")
        return [], False


def synthetic_cool_spaces(H: dict, rng: np.random.Generator, k: int = 40) -> list[dict]:
    idx = rng.choice(len(H["lon"]), size=k, replace=False)
    return [{"name": f"Cool Space {i + 1:02d} (synthetic)", "kind_desc": "Community Centre", "address": "",
             "lon": float(H["lon"][j]), "lat": float(H["lat"][j])} for i, j in enumerate(idx)]


def choose_protected(clusters: list[dict], forced: set[int], H: dict, labels: np.ndarray) -> list[int]:
    """Exactly N_SECTIONS - MIN_ELIGIBLE protected clusters: forced ones, then spread by farthest-point."""
    target = max(0, len(clusters) - MIN_ELIGIBLE)
    chosen = list(dict.fromkeys(forced))[:target]
    cent = to_km(np.array([c["cx"] for c in clusters]), np.array([c["cy"] for c in clusters]))
    sizes = np.bincount(labels, minlength=len(clusters))
    if not chosen:
        chosen = [int(np.argmax(sizes))]
    while len(chosen) < target:
        d = np.min(np.linalg.norm(cent[:, None, :] - cent[None, chosen, :], axis=2), axis=1)
        d[chosen] = -1
        chosen.append(int(np.argmax(d)))
    return chosen


def assign_section_ids(clusters: list[dict], protected: set[int]) -> dict[int, str]:
    """TOR-S01.. ordered west to east; then swap so the demo sections are eligible."""
    order = sorted(range(len(clusters)), key=lambda c: clusters[c]["cx"])
    names = {c: f"TOR-S{i + 1:02d}" for i, c in enumerate(order)}
    by_name = {v: k for k, v in names.items()}
    for demo in DEMO_ELIGIBLE:
        c = by_name.get(demo)
        if c is None or c not in protected:
            continue
        swap = next(cc for cc in order if cc not in protected and names[cc] not in DEMO_ELIGIBLE)
        names[c], names[swap] = names[swap], names[c]
        by_name = {v: k for k, v in names.items()}
    return names


# ---------------------------------------------------------------- residents
def pick_featured(H: dict, sec_ids: list[str], protected_ids: set[str], rng: np.random.Generator, n_pick: int = 25) -> list[int]:
    n = len(H["lon"])
    perm = [int(i) for i in rng.permutation(n) if H["sms"][i]]
    chosen: list[int] = []
    used_sections: Counter = Counter()

    def take(cond, k: int, prefer_new_section: bool = True) -> None:
        got = 0
        for strict in (True, False):
            for i in perm:
                if got >= k:
                    return
                if i in chosen or not cond(i):
                    continue
                if strict and prefer_new_section and used_sections[sec_ids[i]] > 0:
                    continue
                chosen.append(i)
                used_sections[sec_ids[i]] += 1
                got += 1

    # one resident in each demo section (TOR-S11 gets a vulnerable one for the R12 beat if possible)
    for s in DEMO_ELIGIBLE:
        before = len(chosen)
        if s == "TOR-S11":
            take(lambda i, s=s: sec_ids[i] == s and H["vulnerable"][i], 1, False)
        if s == "TOR-S07":  # the injection actor: enrolled, not vulnerable, not senior
            take(lambda i, s=s: sec_ids[i] == s and not H["vulnerable"][i] and not H["senior"][i] and H["c_thermo"][i], 1, False)
            if len(chosen) == before:
                take(lambda i, s=s: sec_ids[i] == s and not H["vulnerable"][i] and not H["senior"][i], 1, False)
        if len(chosen) == before:
            take(lambda i, s=s: sec_ids[i] == s and not (s == "TOR-S07" and H["vulnerable"][i]), 1, False)
    take(lambda i: H["vulnerable"][i], max(0, 3 - sum(H["vulnerable"][i] for i in chosen)))
    take(lambda i: H["has_ev"][i], max(0, 4 - sum(H["has_ev"][i] for i in chosen)))
    take(lambda i: H["c_thermo"][i], max(0, 4 - sum(H["c_thermo"][i] for i in chosen)))
    # then spread across archetypes and sections
    while len(chosen) < min(n_pick, len(perm)):
        before = len(chosen)
        have = Counter(H["arch"][i] for i in chosen)
        for a in sorted(ARCH_IDS, key=lambda a: have[a]):
            if len(chosen) >= n_pick:
                break
            take(lambda i, a=a: H["arch"][i] == a, 1)
        if len(chosen) == before:
            take(lambda i: True, n_pick - len(chosen), False)
            break
    return chosen[:n_pick]


def build_residents(H: dict, featured: list[int], hh_ids: list[str], sec_ids: list[str], nbhs: list[dict],
                    rng: np.random.Generator) -> list[dict]:
    firsts = list(rng.permutation(FIRST_NAMES))
    lasts = list(rng.permutation(LAST_NAMES))
    # injection actor: non-vulnerable resident in TOR-S07 (self-interested ask), else any non-vulnerable one
    inj = next((i for i in featured if sec_ids[i] == "TOR-S07" and not H["vulnerable"][i] and not H["senior"][i]),
               next((i for i in featured if not H["vulnerable"][i] and not H["senior"][i]),
                    next(i for i in featured if not H["vulnerable"][i])))
    out = []
    for r, i in enumerate(featured):
        arch = str(H["arch"][i])
        ages, households, jobs = PERSONA_T[arch]
        age = ages[int(rng.integers(len(ages)))]
        household = households[int(rng.integers(len(households)))]
        job = jobs[int(rng.integers(len(jobs)))]
        attitudes = [str(x) for x in rng.choice(ATTITUDES, size=2, replace=False)]
        if i == inj:
            attitudes = ["enthusiastic about CoolShift", "writes long, chatty replies"]
        name = f"{firsts[r % len(firsts)]} {lasts[r % len(lasts)]}"
        dw = {"house": "a house", "apartment": "a low-rise apartment", "highrise": "a high-rise apartment"}[H["dwelling"][i]]
        acd = {"central": "central air", "window": "a window AC unit", "none": "no air conditioning"}[H["ac"][i]]
        facts = []
        if H["c_thermo"][i]:
            facts.append("Enrolled in CoolShift with a ThermLink thermostat.")
        elif H["has_thermostat"][i]:
            facts.append("Has a ThermLink thermostat but has not joined CoolShift.")
        if H["has_ev"][i]:
            facts.append(f"Drives an EV and plugs in around {int(H['ev_arrival'][i])}:{int(H['ev_arrival'][i] % 1 * 60):02d}"
                         + (" (ChargeFlow program member)." if H["c_ev"][i] else "."))
        if H["has_batt"][i]:
            facts.append("Has a PowerCell home battery.")
        nb_name = nbhs[int(H["nb"][i])]["name"]
        persona = " ".join([f"{name}, age {age}, {job}. {household} in {dw} in {nb_name} with {acd}.",
                            *facts, f"Attitudes: {attitudes[0]}; {attitudes[1]}."])
        out.append({
            "_id": f"res_{r + 1:02d}", "household_id": hh_ids[i], "name": name, "age_bracket": age,
            "household": household, "job": job, "attitudes": attitudes, "persona": persona,
            "injection_actor": i == inj, "archetype_id": arch, "section_id": sec_ids[i],
            "neighbourhood": nb_name, "location": pt(H["lon"][i], H["lat"][i]),
            "vulnerable": bool(H["vulnerable"][i]), "has_ev": bool(H["has_ev"][i]),
            "enrolled": bool(H["c_thermo"][i]),
        })
    return out


# ---------------------------------------------------------------- weather and demand shape
def eccc_hourly(raw: Path) -> tuple[str | None, dict[datetime, float]]:
    """Return the station name and its hourly temperatures in America/Toronto."""
    try:
        feats = json.loads((raw / "eccc_hourly_2025-06-22_26.json").read_text())["features"]
        by_station: dict[str, dict[datetime, float]] = {}
        for f in feats:
            p = f["properties"]
            if p.get("TEMP") is None or not p.get("UTC_DATE"):
                continue
            t = datetime.fromisoformat(p["UTC_DATE"]).replace(tzinfo=UTC).astimezone(TZ)
            by_station.setdefault(p["STATION_NAME"], {})[t] = float(p["TEMP"])
        if not by_station:
            return None, {}
        best = max(len(v) for v in by_station.values())
        name = "TORONTO INTL A" if len(by_station.get("TORONTO INTL A", {})) == best else max(by_station, key=lambda k: len(by_station[k]))
        return name, by_station[name]
    except Exception as e:  # noqa: BLE001
        print(f"weather: synthetic fallback ({e})")
        return None, {}


def synthetic_temp(hour: float) -> float:
    """Heat-wave diurnal curve: min 24 °C at 05:00, max 36 °C at 16:00 (hour may exceed 24)."""
    h = hour % 24
    if 5 <= h <= 16:
        return 24 + 12 * (1 - math.cos(math.pi * (h - 5) / 11)) / 2
    since = (h - 16) % 24
    return 36 - 12 * (1 - math.cos(math.pi * since / 13)) / 2


def build_weather(scn: dict, obs: dict[datetime, float]) -> tuple[list[dict], bool]:
    """5-minute temperatures from 00:00 on the scenario day to 06:00 the next day (covers the
    12:00-21:30 run plus the Forecaster's 6 h look-ahead)."""
    day = date.fromisoformat(str(scn["date"]))
    delta = float((scn.get("weather") or {}).get("delta_c", 3.0))
    src_day = date(2025, 6, 24)
    src = (scn.get("weather") or {}).get("source", "")
    if src.startswith("eccc_"):
        src_day = date.fromisoformat(src.removeprefix("eccc_"))
    start = datetime.combine(day, datetime.min.time(), tzinfo=TZ)
    steps = [start + timedelta(minutes=5 * i) for i in range(int(30 * 60 / 5) + 1)]
    ok = False
    if obs:
        src_start = datetime.combine(src_day, datetime.min.time(), tzinfo=TZ)
        xs = np.array([(t - src_start).total_seconds() / 3600 for t in sorted(obs)])
        ys = np.array([obs[t] for t in sorted(obs)])
        need = (xs >= -1) & (xs <= 31)
        if need.sum() >= 24:
            ok = True
    out = []
    for t in steps:
        h = (t - start).total_seconds() / 3600
        temp = float(np.interp(h, xs, ys)) if ok else synthetic_temp(h)
        out.append({"scenario_id": SCENARIO_ID, "ts": sim_iso(t), "temp_c": round(temp + delta, 2),
                    "source": f"eccc_{src_day.isoformat()}+{delta}" if ok else f"synthetic+{delta}"})
    return out, ok


def ieso_shape(raw: Path, src_day: date) -> dict[str, float] | None:
    """Hourly Ontario demand on src_day, normalized to 1.0 at 17:00 local (EDT), or None."""
    try:
        rows = list(csv.reader((raw / "ieso_demand_2025-06-22_26.csv").read_text().splitlines()))
        d = {int(r[1]): float(r[3]) for r in rows[1:] if r and r[0] == src_day.isoformat()}
        if len(d) < 24:
            return None
        # Hour ending h in EST covers h-1..h EST = h..h+1 EDT.
        edt = {h % 24: v for h, v in d.items()}
        return {f"{h:02d}:00": round(edt[h] / edt[17], 4) for h in range(24)}
    except Exception:  # noqa: BLE001
        return None


# ---------------------------------------------------------------- event memory seed (PLAN §5.3)
LESSONS = [
    ("precool", "Pre-cooling enrolled homes before 15:00 delivered more evening relief than late setbacks."),
    ("thermostat_setback", "A simultaneous thermostat restore at 20:00 caused a rebound peak; staggering the restore in four groups avoided it."),
    ("thermostat_setback", "Senior high-rise households overrode setbacks quickly once indoor temperatures passed their comfort limit; exempting them cost little MW."),
    ("business_curtail_request", "The data centre needed 30 minutes of notice; requests sent late delivered nothing in the first window."),
    ("battery_discharge", "Home batteries stopped at 20% state of charge after about two hours; dispatching them for the steepest hour only worked best."),
    ("battery_discharge", "A late-afternoon generator trip left little time for voluntary rungs; holding battery reserve until 17:30 helped."),
    ("feeder_outage", "Rotating 60-minute outages kept the income-quintile gap under an hour when the rotation order was enforced."),
    ("send_sms", "SMS incentives above $5 added little; acceptance was driven by clear timing and short messages."),
    ("ev_pause", "EV pause requests were mostly honoured; about 4% of sessions ignored the pause and kept charging."),
    ("feeder_outage", "An outage in a section without a notified cool space drew complaints; pre-notification fixed it next time."),
    ("send_sms", "A reply asking operators to change policy was treated as a customer preference only and escalated for review."),
    ("precool", "Pre-cool offsets of -2 °C for two hours kept indoor temperatures well below the safety limit during setbacks."),
]
ACTION_MW = {"precool": (25, 70), "battery_discharge": (8, 25), "ev_pause": (15, 45), "business_curtail_request": (10, 29),
             "thermostat_setback": (30, 90), "send_sms": (5, 20), "feeder_outage": (40, 120)}
OUTCOMES = {"no_outage": "no customer outages were needed",
            "rotating_outages": "{k} short rotating outage(s) were approved",
            "uncontrolled_trip": "an uncontrolled section trip occurred before operators could respond"}


def build_event_memory(seed: int) -> list[dict]:
    rng = np.random.default_rng(seed + 7)
    out = []
    for i in range(30):
        year = 2026 + i % 5
        d = date(year, 6, 1) + timedelta(days=int(rng.integers(0, 85)))
        tmax = round(float(rng.uniform(31.0, 39.0)), 1)
        contingency = bool(rng.random() < 0.3)
        shortfall = int(max(0, (tmax - 31) * 30 + rng.normal(0, 40) + (200 if contingency else 0)))
        lesson_action, lesson = LESSONS[i % len(LESSONS)]
        rungs = list(ACTION_MW)
        n_act = int(rng.integers(2, 6))
        actions = [a for a in rungs[:-1] if rng.random() < n_act / 6]
        if lesson_action not in actions:
            actions.append(lesson_action)
        if shortfall > 250 and "feeder_outage" not in actions and rng.random() < 0.6:
            actions.append("feeder_outage")
        actions = sorted(set(actions), key=rungs.index)
        mw = {a: int(rng.integers(*ACTION_MW[a])) for a in actions}
        delivered = sum(mw.values())
        if "feeder_outage" in actions:
            outcome, k = "rotating_outages", int(rng.integers(1, 4))
        elif delivered < shortfall * 0.8 and contingency:
            outcome, k = "uncontrolled_trip", 0
        else:
            outcome, k = "no_outage", 0
        opt = {a: round(float(rng.uniform(0.02, 0.35)), 2) for a in rng.choice(ARCH_IDS, size=3, replace=False)}
        names = {"precool": "pre-cooling", "battery_discharge": "battery dispatch", "ev_pause": "EV charging pauses",
                 "business_curtail_request": "business curtailment", "thermostat_setback": "thermostat setbacks",
                 "send_sms": "SMS incentives", "feeder_outage": "rotating feeder outages"}
        used = ", ".join(names[a] for a in actions)
        summary = (f"On {d:%B %-d, %Y} the zone reached {tmax} °C with a {shortfall} MW evening shortfall"
                   + (" after a generator trip around 17:35" if contingency else "") + ". "
                   f"Lakeshore Grid Ops used {used}, delivering about {delivered} MW. "
                   f"Outcome: {OUTCOMES[outcome].format(k=k)}. Lesson: {lesson}")
        out.append({
            "_id": f"mem_{i + 1:04d}", "kind": "heat_event", "date": d.isoformat(), "max_temp_c": tmax,
            "shortfall_mw": shortfall, "contingency": contingency, "actions": actions, "mw_by_action": mw,
            "optout_by_archetype": opt, "outcome": outcome, "lesson": lesson, "summary": summary,
        })
    return out


# ---------------------------------------------------------------- calibration estimate
def calibration_estimate(H: dict, scn: dict, weather: list[dict]) -> dict:
    """Steady-state residential load at the peak hour, and the scale it implies (PLAN §5.2 item 8).
    The engine should recompute scale from an uncontrolled run; this is a sanity value."""
    cal = scn.get("calibration") or {}
    peak = cal.get("peak_time", "17:00")
    ph = int(peak[:2]) + int(peak[3:]) / 60
    t_out = next((w["temp_c"] for w in weather if w["ts"][11:16] == peak), 36.0)
    base = interp_hhmm(cal.get("base_kw_profile") or {"00:00": 1.0}, ph) * H["base_scale"]
    solar = np.where(11 <= ph <= 18, H["solar"], 0)
    with np.errstate(divide="ignore", invalid="ignore"):
        duty = np.where(H["k_ac"] > 0, np.clip((H["k_env"] * (t_out - H["setpoint"]) + solar) / H["k_ac"], 0, 1), 0)
    ev = np.where(H["has_ev"] & (H["ev_arrival"] <= ph), EV_KW, 0)
    kw = base + H["ac_kw"] * duty + ev
    res_mw = float(kw.sum() / 1000)
    bg = interp_hhmm(cal.get("commercial_background_mw") or {"00:00": 0.45 * scn["target_peak_mw"]}, ph)
    biz = sum(b["base_load_mw"] for b in BUSINESSES)
    scale = (scn["target_peak_mw"] - bg - biz) / res_mw if res_mw > 0 else 0
    return {"peak_time": peak, "outdoor_c_at_peak": t_out, "target_peak_mw": scn["target_peak_mw"],
            "commercial_background_mw_at_peak": round(bg, 1), "businesses_base_mw": biz,
            "residential_sim_mw_at_peak_est": round(res_mw, 3), "residential_kw_per_home_est": round(float(kw.mean()), 3),
            "scale_est": round(scale, 2)}


# ---------------------------------------------------------------- main
def main() -> dict:
    s = get_settings()
    seed, n_total = int(s.sim_seed), int(s.sim_households)
    rng = np.random.default_rng(seed)
    raw = s.data_dir / "raw"
    out_dir = s.data_dir / "processed"
    out_dir.mkdir(parents=True, exist_ok=True)
    scn = yaml.safe_load((s.scenarios_dir / f"{SCENARIO_ID}.yaml").read_text())
    sources = load_sources(raw)

    nbhs, nb_ok = load_neighbourhoods(raw, rng)
    prof_ok, census_total = neighbourhood_attrs(nbhs, raw, rng)
    H = build_households(nbhs, n_total, rng)
    n = len(H["lon"])
    hh_ids = [f"hh_{i + 1:05d}" for i in range(n)]

    labels, clusters, km = build_sections(H, seed)

    # businesses and cool spaces -> clusters
    def cluster_of(lon: float, lat: float) -> int:
        inside = [c["c"] for c in clusters if c["geom"].contains(Point(lon, lat))]
        if len(inside) == 1:
            return inside[0]
        cand = inside or list(range(len(clusters)))
        d = [math.dist(to_km(np.array([lon]), np.array([lat]))[0], to_km(np.array([clusters[c]["cx"]]), np.array([clusters[c]["cy"]]))[0]) for c in cand]
        return cand[int(np.argmin(d))]

    biz_cluster = {b["_id"]: cluster_of(b["lon"], b["lat"]) for b in BUSINESSES}
    forced = [biz_cluster["biz_harbourfront_mall"], biz_cluster["biz_ashbridge_water_treatment"]]
    prot = choose_protected(clusters, set(forced), H, labels)
    prot_set = set(prot)
    names = assign_section_ids(clusters, prot_set)
    sec_of_home = [names[int(c)] for c in labels]

    # facilities
    cool, cool_ok = load_cool_spaces(raw)
    if not cool_ok:
        cool = synthetic_cool_spaces(H, rng)
    for c in cool:
        c["cluster"] = cluster_of(c["lon"], c["lat"])
    reasons: dict[int, list[str]] = {c: [] for c in range(len(clusters))}
    facilities: list[dict] = []
    designated_idx: set[int] = set()

    mall = next(b for b in BUSINESSES if b["_id"] == "biz_harbourfront_mall")
    facilities.append({"_id": "fac_cool_000", "kind": "cool_space", "name": "Harbourfront Mall", "location": pt(mall["lon"], mall["lat"]),
                       "designated": True, "hours": {"open": "10:00", "close": "23:00"}, "section_id": names[forced[0]],
                       "source": "fictional", "address": "Harbourfront (fictional)", "business_id": mall["_id"]})
    reasons[forced[0]].append("Designated cool space: Harbourfront Mall")
    ash = next(b for b in BUSINESSES if b["_id"] == "biz_ashbridge_water_treatment")
    reasons[forced[1]].append(f"Critical service: {ash['name']}")

    # hospitals (6) in the largest protected sections, pumping stations (2) in the southernmost others
    sizes = np.bincount(labels, minlength=len(clusters))
    hosp_secs = sorted(prot, key=lambda c: -sizes[c])[:6]
    pump_secs = sorted([c for c in prot if c not in hosp_secs], key=lambda c: clusters[c]["cy"])[:2]
    for letter, c in zip("ABCDEF", hosp_secs, strict=False):
        members = np.flatnonzero(labels == c)
        j = members[np.argmin(np.hypot(H["lon"][members] - clusters[c]["cx"], H["lat"][members] - clusters[c]["cy"]))]
        facilities.append({"_id": f"fac_hosp_{letter}", "kind": "critical", "name": f"Hospital {letter}",
                           "location": pt(H["lon"][j] + 0.0015, H["lat"][j] + 0.001), "designated": False,
                           "hours": {"open": "00:00", "close": "24:00"}, "section_id": names[c], "source": "synthetic"})
        reasons[c].append(f"Critical facility: Hospital {letter}")
    for k_, c in enumerate(pump_secs, start=1):
        facilities.append({"_id": f"fac_pump_{k_}", "kind": "pumping", "name": f"Pumping Station P-{k_}",
                           "location": pt(clusters[c]["cx"] - 0.002, clusters[c]["cy"] - 0.0015), "designated": False,
                           "hours": {"open": "00:00", "close": "24:00"}, "section_id": names[c], "source": "synthetic"})
        reasons[c].append(f"Pumping station: P-{k_}")

    # designated cool spaces: one per protected section (mall covers its own), community centres first
    n_designated = int(scn.get("designated_cool_spaces", 12))
    rank = {"Community Centre": 0, "Library": 0, "Indoor Pool": 1}
    n_des = 1
    for c in prot:
        if n_des >= n_designated:
            break
        if any(r.startswith("Designated cool space") for r in reasons[c]):
            continue
        cands = [i for i, x in enumerate(cool) if x["cluster"] == c and i not in designated_idx]
        if cands:
            best = min(cands, key=lambda i: (rank.get(cool[i]["kind_desc"], 2),
                                             math.hypot(cool[i]["lon"] - clusters[c]["cx"], cool[i]["lat"] - clusters[c]["cy"])))
            designated_idx.add(best)
            reasons[c].append(f"Designated cool space: {cool[best]['name']}")
        else:  # no real cool space in this section: synthetic one at the section centre
            cool.append({"name": f"{names[c]} Community Hub (synthetic)", "kind_desc": "Community Centre", "address": "",
                         "lon": clusters[c]["cx"], "lat": clusters[c]["cy"], "cluster": c})
            designated_idx.add(len(cool) - 1)
            reasons[c].append(f"Designated cool space: {cool[-1]['name']}")
        n_des += 1
    for i, x in enumerate(cool):
        des = i in designated_idx
        facilities.append({
            "_id": f"fac_cool_{i + 1:03d}", "kind": "cool_space", "name": x["name"], "location": pt(x["lon"], x["lat"]),
            "designated": des, "hours": {"open": "10:00", "close": "23:00"} if des else
            ({"open": "09:00", "close": "21:00"} if x["kind_desc"] != "Indoor Pool" else {"open": "12:00", "close": "20:00"}),
            "section_id": names[x["cluster"]], "source": "heat_relief_network" if cool_ok else "synthetic",
            "facility_type": x["kind_desc"], "address": x["address"]})

    # sections
    sections = []
    for c in sorted(range(len(clusters)), key=lambda c: names[c]):
        m = labels == c
        qs = Counter(int(q) for q in H["q"][m])
        sections.append({
            "_id": names[c], "geometry": geojson(clusters[c]["geom"]), "centroid": pt(clusters[c]["cx"], clusters[c]["cy"]),
            "households": int(m.sum()), "vulnerable": int(H["vulnerable"][m].sum()),
            "income_quintile": int(max(qs, key=lambda q: (qs[q], -q))), "protected": bool(reasons[c]),
            "protected_reasons": reasons[c],
        })

    # businesses
    businesses = []
    for b in BUSINESSES:
        doc = {k: v for k, v in b.items() if k not in ("lon", "lat")}
        doc.update(location=pt(b["lon"], b["lat"]), section_id=names[biz_cluster[b["_id"]]])
        businesses.append(doc)

    # featured residents
    featured = pick_featured(H, sec_of_home, {x["_id"] for x in sections if x["protected"]}, rng)
    residents = build_residents(H, featured, hh_ids, sec_of_home, nbhs, rng)
    feat_map = {r["household_id"]: r["_id"] for r in residents}

    # households
    households = []
    for i in range(n):
        ac, dw = str(H["ac"][i]), str(H["dwelling"][i])
        devices: dict[str, str] = {}
        if H["has_thermostat"][i]:
            devices["thermostat_id"] = f"tl_{i + 1:05d}"
        if H["has_ev"][i]:
            devices["ev_session_id"] = f"cf_{i + 1:05d}"
        if H["has_batt"][i]:
            devices["battery_id"] = f"pc_{i + 1:05d}"
        doc = {
            "_id": hh_ids[i], "idx": i, "location": pt(H["lon"][i], H["lat"][i]),
            "neighbourhood_id": nbhs[int(H["nb"][i])]["id"], "section_id": sec_of_home[i], "archetype_id": str(H["arch"][i]),
            "dwelling": dw, "ac_type": ac,
            "thermal": {"k_env": round(float(H["k_env"][i]), 4), "k_ac": float(H["k_ac"][i]), "ac_kw": float(H["ac_kw"][i]),
                        "solar": float(H["solar"][i])},
            "setpoint_base": round(float(H["setpoint"][i]), 2), "comfort_max": round(float(H["comfort"][i]), 2),
            "base_kw_scale": round(float(H["base_scale"][i]), 3), "devices": devices,
            "consent": {"thermostat_program": bool(H["c_thermo"][i]), "ev_program": bool(H["c_ev"][i]),
                        "battery_program": bool(H["has_batt"][i]), "sms_opt_in": bool(H["sms"][i])},
            "has_ev": bool(H["has_ev"][i]), "has_battery": bool(H["has_batt"][i]),
            "vulnerable": bool(H["vulnerable"][i]), "income_quintile": int(H["q"][i]),
        }
        if H["has_ev"][i]:
            doc["ev_arrival_h"] = round(float(H["ev_arrival"][i]), 3)
        if hh_ids[i] in feat_map:
            doc["featured_resident_id"] = feat_map[hh_ids[i]]
        households.append(doc)

    counts = Counter(str(a) for a in H["arch"])
    archetypes = [{**a, "households": int(counts.get(a["_id"], 0))} for a in ARCHETYPES]
    neighbourhoods = [{"_id": x["id"], "name": x["name"], "code": x["code"], "geometry": geojson(x["geom"]),
                       "attrs": x["attrs"], "income_quintile": x["income_quintile"]} for x in nbhs]

    station, temperatures = eccc_hourly(raw)
    weather, wx_ok = build_weather(scn, temperatures)
    src_day = date.fromisoformat(str((scn.get("weather") or {}).get("source", "eccc_2025-06-24")).removeprefix("eccc_"))
    shape_ = ieso_shape(raw, src_day)
    households_observed = census_total if nb_ok else None
    observations = build_observations(raw, temperatures, station, households_observed)
    lines = hydro_lines(raw)
    calib = calibration_estimate(H, scn, weather)
    scenario_doc = jsonable({**scn, "_id": SCENARIO_ID, "date": str(scn["date"])})
    scenario_doc.setdefault("calibration", {})
    scenario_doc["calibration"]["ieso_demand_shape"] = shape_
    scenario_doc["calibration"]["estimate"] = calib
    events = build_event_memory(seed)

    lon0, lat0, lon1, lat1 = (float(H["lon"].min()), float(H["lat"].min()), float(H["lon"].max()), float(H["lat"].max()))
    meta = {
        "households": n, "seed": seed, "scenario_id": SCENARIO_ID, "bbox": [round(v, 6) for v in (lon0, lat0, lon1, lat1)],
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "sources_ok": {**sources, "neighbourhoods_used": nb_ok, "profiles_used": prof_ok, "cool_spaces_used": cool_ok,
                       "weather_used": wx_ok, "ieso_shape_used": shape_ is not None,
                       "ieso_zonal_demand_used": bool(observations["demand_mw"]),
                       "ieso_zonal_map_used": bool(observations["zones"]["features"]),
                       "ontario_hydro_lines_used": bool(lines["features"])},
        "counts": {"neighbourhoods": len(neighbourhoods), "sections": len(sections), "facilities": len(facilities),
                   "designated_cool_spaces": sum(f["designated"] for f in facilities), "archetypes": len(archetypes),
                   "residents": len(residents), "businesses": len(businesses), "weather": len(weather),
                   "event_memory": len(events), "vulnerable": int(H["vulnerable"].sum()),
                   "enrolled_thermostats": int(H["c_thermo"].sum()), "ev": int(H["has_ev"].sum()),
                   "batteries": int(H["has_batt"].sum())},
        "protected_sections": [x["_id"] for x in sections if x["protected"]],
        "eligible_sections": [x["_id"] for x in sections if not x["protected"]],
        "injection_resident_id": next(r["_id"] for r in residents if r["injection_actor"]),
        "calibration": calib,
    }

    files = {"neighbourhoods": neighbourhoods, "sections": sections, "facilities": facilities, "households": households,
             "archetypes": archetypes, "residents": residents, "businesses": businesses, "weather": weather,
             "event_memory_seed": events, "scenarios": [scenario_doc]}
    for name, docs in files.items():
        (out_dir / f"{name}.json").write_text(json.dumps(jsonable(docs), separators=(",", ":")))
    (out_dir / "meta.json").write_text(json.dumps(jsonable(meta), indent=2))
    (out_dir / "observations.json").write_text(json.dumps(observations, separators=(",", ":")))
    (out_dir / "hydro-lines.geojson").write_text(json.dumps(lines, separators=(",", ":")))
    if s.data_dir.resolve() == (ROOT / "data").resolve() and (ROOT / "cooldown-react").is_dir():
        # Include the public layers in local Vite builds and static deployments.
        public_data = ROOT / "cooldown-react" / "public" / "data"
        public_data.mkdir(parents=True, exist_ok=True)
        for filename in ("observations.json", "hydro-lines.geojson"):
            copyfile(out_dir / filename, public_data / filename)
        policies, version = seed_policies()
        (public_data / "policies.json").write_text(json.dumps({
            "version": version, "rules": [policies[key].to_mongo() for key in sorted(policies)],
        }, separators=(",", ":")))

    c = meta["counts"]
    print(f"households {n}  sections {c['sections']} (protected {len(meta['protected_sections'])}, eligible {len(meta['eligible_sections'])})")
    print(f"facilities {c['facilities']} (designated {c['designated_cool_spaces']})  archetypes {c['archetypes']}  residents {c['residents']}")
    print(f"vulnerable {c['vulnerable']}  enrolled {c['enrolled_thermostats']}  ev {c['ev']}  batteries {c['batteries']}")
    print(f"weather {c['weather']} pts  event_memory {c['event_memory']}  injection {meta['injection_resident_id']}")
    print(f"calibration: res {calib['residential_sim_mw_at_peak_est']} MW sim @ {calib['outdoor_c_at_peak']} °C -> scale ≈ {calib['scale_est']}")
    print(f"sources used: {meta['sources_ok']}")
    return meta


if __name__ == "__main__":
    main()
