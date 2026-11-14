"""Prepare observations from the public source files without generating fallback readings."""

import csv
import json
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from xml.etree import ElementTree

from shapely.geometry import Polygon, mapping
from shapely.geometry.polygon import orient

from app.timeutil import TZ
from .fetch_data import IESO_ZONAL_URL

OBSERVED_DATE = date(2025, 6, 24)
ZONES = ("Northwest", "Northeast", "Ottawa", "East", "Toronto", "Essa", "Bruce", "Southwest", "Niagara", "West")
KML = {"k": "http://www.opengis.net/kml/2.2"}
EST = timezone(timedelta(hours=-5))


def zonal_demand(raw: Path) -> dict:
    hours = {}
    path = raw / "ieso_zonal_demand_2025-06-23_24.csv"
    if path.exists():
        with path.open(newline="") as file:
            for row in csv.DictReader(file):
                day = date.fromisoformat(row["Date"])
                # IESO's EST hour ending 1 starts at midnight EST (01:00 EDT in June).
                start = datetime.combine(day, time(), EST) + timedelta(hours=int(row["Hour"]) - 1)
                local = start.astimezone(TZ)
                if local.date() == OBSERVED_DATE:
                    hours[local.strftime("%H:%M")] = {zone: int(row[zone]) for zone in ZONES}
    return hours


def electrical_zones(raw: Path) -> dict:
    path = raw / "ieso_electrical_zones.kml"
    features = []
    if path.exists():
        document = ElementTree.parse(path)
        for folder in document.findall(".//k:Folder", KML):
            if folder.findtext("k:name", namespaces=KML) != "IESO Electrical Zones":
                continue
            for place in folder.findall("k:Placemark", KML):
                name = place.findtext("k:name", namespaces=KML)
                if name not in ZONES:
                    continue
                rings = []
                for coordinates in place.findall(".//k:Polygon//k:coordinates", KML):
                    rings.append([tuple(map(float, point.split(",")[:2])) for point in coordinates.text.split()])
                if rings:
                    polygon = orient(Polygon(rings[0], rings[1:]))
                    features.append({"type": "Feature", "properties": {"name": name}, "geometry": mapping(polygon)})
    return {"type": "FeatureCollection", "features": features}


def hydro_lines(raw: Path) -> dict:
    path = raw / "ontario_hydro_lines.geojson"
    features = []
    if path.exists():
        for feature in json.loads(path.read_text())["features"]:
            geometry = feature.get("geometry")
            if geometry and geometry["type"] in ("LineString", "MultiLineString"):
                props = feature["properties"]
                features.append({"type": "Feature", "geometry": geometry,
                                 "properties": {"id": props["OGF_ID"], "kind": props["CLASS_SUBTYPE"]}})
    return {"type": "FeatureCollection", "features": features}


def build_observations(raw: Path, hourly_temperatures: dict | None, station: str | None, households: int | None) -> dict:
    temperatures = {
        t.strftime("%H:%M"): c for t, c in (hourly_temperatures or {}).items() if t.date() == OBSERVED_DATE
    }
    return {
        "date": OBSERVED_DATE.isoformat(),
        "zones": electrical_zones(raw),
        "demand_mw": zonal_demand(raw),
        "demand_source": IESO_ZONAL_URL,
        "temperature_c": temperatures,
        "weather_station": station,
        "weather_source": "https://api.weather.gc.ca/collections/climate-hourly?lang=en",
        "toronto_census_households": households,
        "census_source": "https://open.toronto.ca/dataset/neighbourhood-profiles/",
    }
