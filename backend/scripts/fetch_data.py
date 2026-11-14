"""Download the public source data into data/raw/ (PLAN §5.1).

Every source is tried with short timeouts. The result of each attempt is recorded in
data/raw/sources.json as {name, url, license, retrieved_at, ok, file, note}. Nothing here ever
raises: missing downloads are reported and the world can still be built. A raw file left by an
earlier successful run is kept (and reported ok) if a re-download fails.

Run from backend/:  .venv/bin/python -m scripts.fetch_data
"""

import asyncio
import csv
import io
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path

import httpx

from app.config import get_settings

CKAN = "https://ckan0.cf.opendata.inter.prod-toronto.ca/api/3/action/package_show?id="
LICENCE_TORONTO = "Open Government Licence – Toronto"
LICENCE_CANADA = "Open Government Licence – Canada (ECCC)"
LICENCE_IESO = "IESO public reports (attribution)"

ECCC_URL = (
    "https://api.weather.gc.ca/collections/climate-hourly/items"
    "?bbox=-79.70,43.58,-79.20,43.86&datetime=2025-06-22T00:00:00Z/2025-06-26T23:59:59Z"
    "&limit=10000&f=json"
)
IESO_URLS = [
    "https://reports-public.ieso.ca/public/Demand/PUB_Demand_2025.csv",
    "http://reports.ieso.ca/public/Demand/PUB_Demand_2025.csv",
]
IESO_ZONAL_URL = "https://reports-public.ieso.ca/public/DemandZonal/PUB_DemandZonal_2025.csv"
IESO_ZONE_MAP_URL = "https://www.google.com/maps/d/kml?mid=zQEHqto-h7Ww.kk9O7iElA_xo&forcekml=1"
ONTARIO_LINES = "https://ws.lioservices.lrc.gov.on.ca/arcgis2/rest/services/LIO_OPEN_DATA/LIO_Open05/MapServer/11/query"
NEIGHBOURHOODS_ALT = (
    "https://gis.toronto.ca/arcgis/rest/services/cot_geospatial26/FeatureServer/71/query"
    "?where=1%3D1&outFields=*&outSR=4326&f=geojson"
)

TIMEOUT = httpx.Timeout(20.0, connect=8.0)
BIG_TIMEOUT = httpx.Timeout(60.0, connect=8.0)


def raw_dir() -> Path:
    d = get_settings().data_dir / "raw"
    d.mkdir(parents=True, exist_ok=True)
    return d


async def ckan_resource_url(
    client: httpx.AsyncClient, package: str, want_format: str, prefer: tuple[str, ...]
) -> str | None:
    """Resolve a CKAN package to the download URL of its best matching resource."""
    r = await client.get(CKAN + package, timeout=TIMEOUT)
    r.raise_for_status()
    resources = r.json()["result"]["resources"]
    fmt = [x for x in resources if (x.get("format") or "").lower() == want_format.lower()]
    for needle in prefer:
        for x in fmt:
            if needle.lower() in (x.get("name") or "").lower() or needle.lower() in (x.get("url") or "").lower():
                return x["url"]
    return fmt[0]["url"] if fmt else None


def _check_geojson(content: bytes, min_features: int) -> None:
    data = json.loads(content)
    feats = data.get("features") or []
    if len(feats) < min_features:
        raise ValueError(f"only {len(feats)} features")


async def fetch_neighbourhoods(client: httpx.AsyncClient) -> tuple[str, bytes, str]:
    try:
        url = await ckan_resource_url(client, "neighbourhoods", "geojson", ("- 4326.geojson", "4326"))
        if url and "historical" in url:
            url = None
        if not url:
            raise ValueError("no 4326 GeoJSON resource")
        r = await client.get(url, timeout=BIG_TIMEOUT)
        r.raise_for_status()
        _check_geojson(r.content, 100)
        return url, r.content, ""
    except Exception as e:  # noqa: BLE001 — try the ArcGIS mirror next
        r = await client.get(NEIGHBOURHOODS_ALT, timeout=BIG_TIMEOUT)
        r.raise_for_status()
        _check_geojson(r.content, 100)
        return NEIGHBOURHOODS_ALT, r.content, f"CKAN failed ({e!s:.80}); used ArcGIS mirror"


async def fetch_profiles(client: httpx.AsyncClient) -> tuple[str, bytes, str]:
    url = await ckan_resource_url(client, "neighbourhood-profiles", "xlsx", ("2021", "158"))
    if not url:
        raise ValueError("no 2021 xlsx resource")
    r = await client.get(url, timeout=BIG_TIMEOUT)
    r.raise_for_status()
    if len(r.content) < 10_000:
        raise ValueError("profiles file too small")
    return url, r.content, ""


async def fetch_cool_spaces(client: httpx.AsyncClient) -> tuple[str, bytes, str]:
    url = await ckan_resource_url(
        client, "air-conditioned-and-cool-spaces-heat-relief-network", "geojson", ("4326.geojson", "4326")
    )
    if not url:
        raise ValueError("no GeoJSON resource")
    r = await client.get(url, timeout=BIG_TIMEOUT)
    r.raise_for_status()
    _check_geojson(r.content, 10)
    return url, r.content, ""


async def fetch_weather(client: httpx.AsyncClient) -> tuple[str, bytes, str]:
    r = await client.get(ECCC_URL, timeout=BIG_TIMEOUT)
    r.raise_for_status()
    feats = r.json().get("features") or []
    counts: dict[str, int] = {}
    for f in feats:
        p = f.get("properties") or {}
        if p.get("TEMP") is not None:
            counts[p.get("STATION_NAME", "?")] = counts.get(p.get("STATION_NAME", "?"), 0) + 1
    if not counts:
        raise ValueError("no temperature rows")
    best = max(counts.values())
    station = "TORONTO INTL A" if counts.get("TORONTO INTL A") == best else max(counts, key=counts.get)
    return ECCC_URL, r.content, f"station {station} ({counts[station]} temperature rows)"


async def fetch_demand(client: httpx.AsyncClient) -> tuple[str, bytes, str]:
    last: Exception | None = None
    for url in IESO_URLS:
        try:
            r = await client.get(url, timeout=BIG_TIMEOUT)
            r.raise_for_status()
            rows = [x for x in csv.reader(io.StringIO(r.text)) if x and x[0].startswith("2025-06-2")]
            keep = [x for x in rows if x[0] in {f"2025-06-{d}" for d in range(22, 27)}]
            if len(keep) < 24:
                raise ValueError(f"only {len(keep)} rows for June 22-26")
            out = io.StringIO()
            w = csv.writer(out)
            w.writerow(["Date", "Hour", "Market Demand", "Ontario Demand"])
            w.writerows(keep)
            return url, out.getvalue().encode(), "extracted 2025-06-22..26 (hour ending, EST)"
        except Exception as e:  # noqa: BLE001
            last = e
    raise last or ValueError("no IESO host reachable")


async def fetch_zonal_demand(client: httpx.AsyncClient) -> tuple[str, bytes, str]:
    response = await client.get(IESO_ZONAL_URL, timeout=BIG_TIMEOUT)
    response.raise_for_status()
    rows = list(csv.reader(io.StringIO(response.text)))
    header = next(row for row in rows if row[:2] == ["Date", "Hour"])
    # Include the previous day's last EST hour to cover midnight in Toronto (EDT).
    selected = [row for row in rows if row and "2025-06-23" <= row[0] <= "2025-06-24"]
    if len(selected) != 48 or "Toronto" not in header:
        raise ValueError("expected two days of zonal demand and a Toronto column")
    out = io.StringIO()
    csv.writer(out).writerows([header, *selected])
    return IESO_ZONAL_URL, out.getvalue().encode(), "June 23–24, 2025 (hour ending, EST)"


async def fetch_zone_map(client: httpx.AsyncClient) -> tuple[str, bytes, str]:
    response = await client.get(IESO_ZONE_MAP_URL, timeout=BIG_TIMEOUT)
    response.raise_for_status()
    if b"IESO Electrical Zones" not in response.content:
        raise ValueError("missing IESO Electrical Zones layer")
    return IESO_ZONE_MAP_URL, response.content, "KML export linked by the IESO Zonal Map"


async def fetch_hydro_lines(client: httpx.AsyncClient) -> tuple[str, bytes, str]:
    features = []
    while True:
        response = await client.get(ONTARIO_LINES, timeout=BIG_TIMEOUT, params={
            "where": "CLASS_SUBTYPE_NUM IN (1114,1429)", "outFields": "OGF_ID,CLASS_SUBTYPE",
            "outSR": 4326, "f": "geojson", "orderByFields": "OBJECTID",
            "resultOffset": len(features), "resultRecordCount": 1000,
        })
        response.raise_for_status()
        page = response.json()
        rows = page.get("features") or []
        if not rows:
            raise ValueError(f"empty Utility Line page after {len(features)} records")
        features.extend(rows)
        if not page.get("exceededTransferLimit"):
            break
    if len({feature["properties"]["OGF_ID"] for feature in features}) != len(features):
        raise ValueError("duplicate Utility Line records")
    collection = {"type": "FeatureCollection", "features": features}
    return ONTARIO_LINES, json.dumps(collection, separators=(",", ":")).encode(), f"{len(features)} hydro line segments"


SOURCES: list[tuple[str, str, str, str, Callable[[httpx.AsyncClient], Awaitable[tuple[str, bytes, str]]]]] = [
    ("toronto_neighbourhoods", "neighbourhoods.geojson", LICENCE_TORONTO, CKAN + "neighbourhoods", fetch_neighbourhoods),
    ("toronto_neighbourhood_profiles_2021", "neighbourhood_profiles_2021.xlsx", LICENCE_TORONTO,
     CKAN + "neighbourhood-profiles", fetch_profiles),
    ("toronto_heat_relief_network", "heat_relief_network.geojson", LICENCE_TORONTO,
     CKAN + "air-conditioned-and-cool-spaces-heat-relief-network", fetch_cool_spaces),
    ("eccc_climate_hourly", "eccc_hourly_2025-06-22_26.json", LICENCE_CANADA, ECCC_URL, fetch_weather),
    ("ieso_hourly_demand", "ieso_demand_2025-06-22_26.csv", LICENCE_IESO, IESO_URLS[0], fetch_demand),
    ("ieso_hourly_zonal_demand", "ieso_zonal_demand_2025-06-23_24.csv", LICENCE_IESO, IESO_ZONAL_URL, fetch_zonal_demand),
    ("ieso_zonal_map", "ieso_electrical_zones.kml", LICENCE_IESO, IESO_ZONE_MAP_URL, fetch_zone_map),
    ("ontario_hydro_lines", "ontario_hydro_lines.geojson", "Open Government Licence – Ontario", ONTARIO_LINES, fetch_hydro_lines),
]


async def fetch_one(client: httpx.AsyncClient, spec: tuple) -> dict:
    name, filename, licence, default_url, fn = spec
    path = raw_dir() / filename
    rec = {"name": name, "url": default_url, "license": licence, "file": filename,
           "retrieved_at": datetime.now(UTC).isoformat(timespec="seconds"), "ok": False, "note": ""}
    try:
        url, content, note = await asyncio.wait_for(fn(client), timeout=120)
        path.write_bytes(content)
        rec.update(url=url, ok=True, note=note, bytes=len(content))
    except Exception as e:  # noqa: BLE001 — the build must never block on a download
        if path.exists() and path.stat().st_size > 0:
            rec.update(ok=True, note=f"download failed ({type(e).__name__}); using cached raw file")
        else:
            rec.update(note=f"download failed ({type(e).__name__}: {e!s:.120}); synthetic fallback")
    return rec


async def main() -> list[dict]:
    async with httpx.AsyncClient(follow_redirects=True, headers={"User-Agent": "cooldown-hackathon/0.1"}) as client:
        records = await asyncio.gather(*(fetch_one(client, s) for s in SOURCES))
    (raw_dir() / "sources.json").write_text(json.dumps(records, indent=2))
    for r in records:
        print(f"{'ok ' if r['ok'] else 'FALLBACK'}  {r['name']:<38} {r['note']}")
    return records


if __name__ == "__main__":
    asyncio.run(main())
