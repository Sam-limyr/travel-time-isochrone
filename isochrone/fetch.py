"""Download the raw datasets.

Small datasets are written to ``data/raw`` (committed); large ones to ``data/cache``
(gitignored). Every fetch records where and when the data came from in
``data/raw/sources.json`` so the UI can show how fresh the data is.
"""
from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import config

SOURCES_FILE = config.RAW / "sources.json"
LTA_DIR = config.RAW / "lta"

_session = requests.Session()
_session.headers["User-Agent"] = "sg-travel-time-isochrone/0.1 (local research tool)"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _record_source(name: str, **info) -> None:
    sources = json.loads(SOURCES_FILE.read_text(encoding="utf-8")) if SOURCES_FILE.exists() else {}
    sources[name] = {"fetched_at": _now(), **info}
    SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    SOURCES_FILE.write_text(json.dumps(sources, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _get(url: str, *, retries: int = 4, **kwargs) -> requests.Response:
    for attempt in range(retries):
        try:
            resp = _session.get(url, timeout=kwargs.pop("timeout", 60), **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"HTTP {resp.status_code}", response=resp)
            resp.raise_for_status()
            return resp
        except (requests.ConnectionError, requests.Timeout, requests.HTTPError):
            if attempt == retries - 1:
                raise
            time.sleep(2 ** attempt)
    raise AssertionError("unreachable")


def _download(url: str, dest: Path, **kwargs) -> requests.Response:
    """Stream a file to disk atomically."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    with _session.get(url, stream=True, timeout=120, **kwargs) as resp:
        resp.raise_for_status()
        with open(tmp, "wb") as fh:
            for chunk in resp.iter_content(chunk_size=1 << 20):
                fh.write(chunk)
    tmp.replace(dest)
    return resp


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore", lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


# --- LTA DataMall -------------------------------------------------------------

def _datamall_headers() -> dict:
    key = config.lta_account_key()
    if not key:
        raise SystemExit(
            "No LTA DataMall key found. Put LTA_ACCOUNT_KEY=<key> in .env "
            "(or skip with --only osm,hdb,busrouter; the committed data in data/raw is used)."
        )
    return {"AccountKey": key, "accept": "application/json"}


def _datamall_all(endpoint: str) -> list[dict]:
    """Fetch every record of a paginated DataMall endpoint (500 records per call)."""
    headers = _datamall_headers()
    rows: list[dict] = []
    while True:
        page = _get(f"{config.DATAMALL_BASE}{endpoint}", headers=headers,
                    params={"$skip": len(rows)}).json()["value"]
        if not page:
            return rows
        rows.extend(page)


BUS_STOP_FIELDS = ["BusStopCode", "RoadName", "Description", "Latitude", "Longitude"]
BUS_SERVICE_FIELDS = ["ServiceNo", "Operator", "Direction", "Category", "OriginCode", "DestinationCode",
                      "AM_Peak_Freq", "AM_Offpeak_Freq", "PM_Peak_Freq", "PM_Offpeak_Freq", "LoopDesc"]
BUS_ROUTE_FIELDS = ["ServiceNo", "Operator", "Direction", "StopSequence", "BusStopCode", "Distance",
                    "WD_FirstBus", "WD_LastBus", "SAT_FirstBus", "SAT_LastBus", "SUN_FirstBus", "SUN_LastBus"]


def fetch_lta() -> None:
    print("LTA DataMall: bus stops, services, routes ...")
    stops = _datamall_all("BusStops")
    services = _datamall_all("BusServices")
    routes = _datamall_all("BusRoutes")
    routes.sort(key=lambda r: (r["ServiceNo"], int(r["Direction"]), int(r["StopSequence"])))
    services.sort(key=lambda r: (r["ServiceNo"], int(r["Direction"])))
    stops.sort(key=lambda r: r["BusStopCode"])
    _write_csv(LTA_DIR / "bus_stops.csv", stops, BUS_STOP_FIELDS)
    _write_csv(LTA_DIR / "bus_services.csv", services, BUS_SERVICE_FIELDS)
    _write_csv(LTA_DIR / "bus_routes.csv", routes, BUS_ROUTE_FIELDS)
    print(f"  {len(stops)} stops, {len(services)} service-directions, {len(routes)} route stops")

    print("LTA DataMall: train GTFS schedule ...")
    value = _get(f"{config.DATAMALL_BASE}GTFSScheduleTrain", headers=_datamall_headers()).json()["value"]
    item = value[0] if isinstance(value, list) else value
    _download(item["link"], LTA_DIR / "train_gtfs.zip")
    size_mb = (LTA_DIR / "train_gtfs.zip").stat().st_size / 1e6
    print(f"  train_gtfs.zip {size_mb:.1f} MB (published {item.get('timestamp')})")

    _record_source("lta_datamall", url=config.DATAMALL_BASE,
                   datasets=["BusStops", "BusServices", "BusRoutes", "GTFSScheduleTrain"],
                   counts={"bus_stops": len(stops), "bus_service_directions": len(services),
                           "bus_route_stops": len(routes)},
                   train_gtfs_timestamp=item.get("timestamp"),
                   licence="Singapore Open Data Licence v1.0 (contains information from LTA DataMall)")


# --- OpenStreetMap ------------------------------------------------------------

OSM_PBF = config.CACHE / "singapore-latest.osm.pbf"


def fetch_osm(force: bool = False) -> None:
    if OSM_PBF.exists() and not force:
        print(f"OSM extract already cached ({OSM_PBF.name}); use --force to refresh")
        return
    print("OpenStreetMap: Singapore extract ...")
    resp = _download(config.OSM_PBF_URL, OSM_PBF)
    print(f"  {OSM_PBF.stat().st_size / 1e6:.1f} MB, last modified {resp.headers.get('Last-Modified')}")
    _record_source("openstreetmap", url=config.OSM_PBF_URL, last_modified=resp.headers.get("Last-Modified"),
                   licence="ODbL 1.0, (c) OpenStreetMap contributors")


# --- HDB building footprints --------------------------------------------------

HDB_GEOJSON = config.CACHE / "hdb_existing_building.geojson"


def _datagov_download(dataset_id: str, dest: Path) -> None:
    """Download a data.gov.sg dataset file via its initiate/poll API."""
    base = f"{config.DATAGOV_API}{dataset_id}"
    _get(f"{base}/initiate-download")
    for _ in range(20):
        url = (_get(f"{base}/poll-download").json().get("data") or {}).get("url")
        if url:
            _download(url, dest)
            print(f"  {dest.stat().st_size / 1e6:.1f} MB")
            return
        time.sleep(3)
    raise RuntimeError(f"data.gov.sg did not return a download URL for {dataset_id}")


PEAK_SPEEDS_DATASET_ID = "d_26f6afadf2f86b2004f9a1e28f5564cc"  # LTA "Average Speed During Peak Hours"


def fetch_peak_speeds() -> None:
    """LTA's yearly average peak-hour road speeds (used to calibrate car mode)."""
    print("data.gov.sg: LTA Average Speed During Peak Hours ...")
    _datagov_download(PEAK_SPEEDS_DATASET_ID, LTA_DIR / "average_peak_speeds.csv")
    _record_source("lta_peak_speeds", url=f"https://data.gov.sg/datasets/{PEAK_SPEEDS_DATASET_ID}/view",
                   licence="Singapore Open Data Licence v1.0 (LTA via data.gov.sg)")


def fetch_hdb(force: bool = False) -> None:
    """Download HDB building footprints (used to let walkers cut through void decks)."""
    if not HDB_GEOJSON.exists() or force:
        print("data.gov.sg: HDB Existing Building ...")
        _datagov_download(config.HDB_DATASET_ID, HDB_GEOJSON)
    count = _compact_hdb()
    _record_source("hdb_buildings", url=f"https://data.gov.sg/datasets/{config.HDB_DATASET_ID}/view",
                   count=count, licence="Singapore Open Data Licence v1.0 (HDB via data.gov.sg)")


def _compact_hdb() -> int:
    """Reduce the 50+ MB GeoJSON to simplified outer rings, small enough to commit."""
    from shapely.geometry import shape, mapping

    src = json.loads(HDB_GEOJSON.read_text(encoding="utf-8"))
    blocks = []
    for feat in src["features"]:
        geom = shape(feat["geometry"])
        if geom.is_empty:
            continue
        # ~3 m simplification and ~1 m rounding: plenty for deciding which footpaths
        # a void deck connects, and it keeps the committed file small.
        geom = geom.simplify(0.000027, preserve_topology=True)
        polys = list(geom.geoms) if geom.geom_type == "MultiPolygon" else [geom]
        props = feat.get("properties") or {}
        for poly in polys:
            ring = [[round(x, 5), round(y, 5)] for x, y in poly.exterior.coords]
            blocks.append({"blk": props.get("BLK_NO"), "postal": props.get("POSTAL_COD"), "ring": ring})
    out = config.RAW / "hdb" / "hdb_buildings.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(blocks, separators=(",", ":")), encoding="utf-8")
    print(f"  {len(blocks)} footprints -> {out.relative_to(config.ROOT)} ({out.stat().st_size / 1e6:.1f} MB)")
    return len(blocks)


# --- Singapore land outline ---------------------------------------------------

LAND_FILE = config.RAW / "boundary" / "sg_land.geojson"


def fetch_boundary() -> None:
    """URA region boundaries (no sea), dissolved into one land polygon.

    The OSM extract also covers southern Johor, so this outline keeps the networks
    and the heatmap inside Singapore.
    """
    from shapely.geometry import mapping, shape
    from shapely.ops import unary_union

    print("data.gov.sg: Master Plan 2019 Region Boundary (No Sea) ...")
    src = config.CACHE / "mp2019_region_boundary.geojson"
    _datagov_download(config.REGION_DATASET_ID, src)
    feats = json.loads(src.read_text(encoding="utf-8"))["features"]
    land = unary_union([shape(f["geometry"]).buffer(0) for f in feats])
    land = land.simplify(0.00005, preserve_topology=True)  # ~5 m
    LAND_FILE.parent.mkdir(parents=True, exist_ok=True)
    geojson = {"type": "Feature", "properties": {"name": "Singapore land (URA MP2019, no sea)"},
               "geometry": mapping(land)}
    LAND_FILE.write_text(json.dumps(geojson, separators=(",", ":")), encoding="utf-8")
    print(f"  {len(feats)} regions -> {LAND_FILE.relative_to(config.ROOT)} ({LAND_FILE.stat().st_size / 1e3:.0f} kB)")
    _record_source("land_boundary", url=f"https://data.gov.sg/datasets/{config.REGION_DATASET_ID}/view",
                   licence="Singapore Open Data Licence v1.0 (URA via data.gov.sg)")


# --- busrouter.sg (bus route polylines for the overlay) -----------------------

def fetch_busrouter() -> None:
    print("busrouter.sg: bus route polylines ...")
    out = config.RAW / "busrouter"
    for name in ("routes.min.json", "services.min.json"):
        _download(config.BUSROUTER_BASE + name, out / name)
    updated = _get(config.BUSROUTER_BASE + "last-updated.txt").text.strip().strip('"')
    print(f"  mirror last updated {updated}")
    _record_source("busrouter", url=config.BUSROUTER_BASE, last_updated=updated,
                   licence="Route lines from github.com/cheeaun/sgbusdata (derived from LTA data)")


FETCHERS = {"lta": fetch_lta, "speeds": fetch_peak_speeds, "osm": fetch_osm, "hdb": fetch_hdb,
            "boundary": fetch_boundary, "busrouter": fetch_busrouter}


def fetch(only: list[str] | None = None, force: bool = False) -> None:
    for name in only or list(FETCHERS):
        fn = FETCHERS[name]
        fn(force=force) if name in ("osm", "hdb") else fn()
