"""Build the routing networks, heatmap grids and map overlays into ``data/build``.

Run with ``python -m isochrone build``. Takes about a minute.
"""
from __future__ import annotations

import csv
import io
import json
import math
import time
import zipfile
from datetime import date, datetime, timedelta

import numpy as np
import shapely
from scipy.spatial import cKDTree

from . import barriers, config, fetch, geo, osm, transit

VOIDDECK_NEAR_M = 15.0   # footpath nodes this close to an HDB block can use its void deck
VOIDDECK_MAX_M = 100.0   # longest cut-through considered
VOIDDECK_PER_NODE = 4
MAJOR_COMPONENT = 1000   # walk components smaller than this are ignored for snapping
STOP_SNAP_M = 150.0
ENTRANCE_SNAP_M = 300.0
DRIVE_SNAP_M = 1000.0    # a car is parked at most this far from a map cell


def _log(t0: float, msg: str) -> None:
    print(f"[{time.time() - t0:5.1f}s] {msg}", flush=True)


def build_is_current() -> bool:
    """Whether data/build holds a build in the format this code reads."""
    path = config.BUILD / "meta.json"
    return path.exists() and json.loads(path.read_text(encoding="utf-8")).get("format") == config.BUILD_FORMAT


def reference_weekday(today: date | None = None) -> date:
    """The weekday whose train timetable is used: today, or the next weekday."""
    day = today or date.today()
    while day.weekday() >= 5:
        day += timedelta(days=1)
    return day


def voiddeck_edges(x: np.ndarray, y: np.ndarray, candidates: np.ndarray):
    """Walking links straight through HDB blocks (void decks).

    Footpath nodes within VOIDDECK_NEAR_M of a block are joined across the block
    when the straight line between them passes through the footprint. Each node
    keeps its VOIDDECK_PER_NODE shortest such links.
    """
    blocks = json.loads((config.RAW / "hdb" / "hdb_buildings.json").read_text(encoding="utf-8"))
    polys = []
    for b in blocks:
        ring = np.asarray(b["ring"])
        px, py = geo.to_xy(ring[:, 0], ring[:, 1])
        poly = shapely.Polygon(np.column_stack([px, py]))
        polys.append(poly if poly.is_valid else poly.buffer(0))
    polys = np.array(polys, dtype=object)
    idx = np.nonzero(candidates)[0]
    tree = shapely.STRtree(shapely.points(x[idx], y[idx]))
    poly_i, node_i = tree.query(polys, predicate="dwithin", distance=VOIDDECK_NEAR_M)
    order = np.argsort(poly_i, kind="stable")
    poly_i, node_i = poly_i[order], idx[node_i[order]]
    bounds = np.flatnonzero(np.diff(poly_i)) + 1
    us, vs, ds = [], [], []
    for grp_poly, grp_nodes in zip(np.split(poly_i, bounds), np.split(node_i, bounds)):
        if len(grp_nodes) < 2:
            continue
        poly = polys[grp_poly[0]]
        a, b = np.triu_indices(len(grp_nodes), k=1)
        na, nb = grp_nodes[a], grp_nodes[b]
        d = np.hypot(x[na] - x[nb], y[na] - y[nb])
        ok = (d > 3) & (d <= VOIDDECK_MAX_M)
        ok &= shapely.contains_xy(poly, (x[na] + x[nb]) / 2, (y[na] + y[nb]) / 2)
        if not ok.any():
            continue
        cand = sorted(zip(d[ok], na[ok], nb[ok]))
        used: dict[int, int] = {}
        for dist, p, q in cand:
            if used.get(p, 0) < VOIDDECK_PER_NODE and used.get(q, 0) < VOIDDECK_PER_NODE:
                us.append(p); vs.append(q); ds.append(dist)
                used[p] = used.get(p, 0) + 1
                used[q] = used.get(q, 0) + 1
    u, v, d = np.array(us, np.int32), np.array(vs, np.int32), np.array(ds, np.float32)
    return np.concatenate([u, v]), np.concatenate([v, u]), np.concatenate([d, d]), len(blocks)


def _snap(barrier, pool: barriers.Pool, x, y, k: int, max_m: float):
    """(item, node, metres) links from points to their k nearest network nodes within max_m
    that they can walk to in a straight line without crossing a barrier."""
    node, metres, *_ = barriers.snap(barrier, x, y, [(pool, k, max_m)], fallback=True)
    ok = np.isfinite(metres)
    item = np.repeat(np.arange(len(node)), k).reshape(node.shape)
    return item[ok].astype(np.int32), node[ok].astype(np.int32), metres[ok].astype(np.float32)


def build_grids(land, barrier, walk_targets: list, drive_targets: list) -> tuple[dict, dict]:
    """Per land cell, the network nodes it joins by a straight, barrier-aware walk: for
    public transport its nearest footpaths, station exit and bus stop; for a car, its
    nearest roads (see barriers.snap for the targets). Cells that reach nothing within
    reach are left out (no data)."""
    minx, miny, maxx, maxy = land.bounds
    arrays, meta = {}, {}
    for name, res in config.RESOLUTIONS.items():
        nx, ny = math.ceil((maxx - minx) / res), math.ceil((maxy - miny) / res)
        col, row = np.meshgrid(np.arange(nx), np.arange(ny))
        cx = minx + (col.ravel() + 0.5) * res
        cy = maxy - (row.ravel() + 0.5) * res  # row 0 is the northern edge (image order)
        cell = np.nonzero(shapely.contains_xy(land, cx, cy))[0]
        wn, wm, *_ = barriers.snap(barrier, cx[cell], cy[cell], walk_targets, step=False)
        keep = np.isfinite(wm).any(axis=1)
        cell, wn, wm = cell[keep], wn[keep], wm[keep]
        dn, dm, *_ = barriers.snap(barrier, cx[cell], cy[cell], drive_targets, step=False)
        arrays |= {
            f"{name}_cell": cell.astype(np.int32),
            f"{name}_walk_node": wn.astype(np.int32),
            f"{name}_walk_m": wm.astype(np.float32),
            f"{name}_drive_node": dn.astype(np.int32),
            f"{name}_drive_m": dm.astype(np.float32),
        }
        west, north = geo.to_lonlat(minx, maxy)
        east, south = geo.to_lonlat(minx + nx * res, maxy - ny * res)
        meta[name] = {"res_m": res, "nx": nx, "ny": ny, "cells": int(len(cell)),
                      "bounds": [float(west), float(south), float(east), float(north)],
                      "origin_xy": [float(minx), float(maxy)]}
    return arrays, meta


def build_overlays(rail_ways, rail: transit.RailModel, bus: transit.BusModel) -> None:
    out = config.BUILD / "overlays"
    out.mkdir(parents=True, exist_ok=True)
    route_line = {"NSL": "NSL", "EWL": "EWL", "EWL_CGL": "EWL", "NEL": "NEL", "DTL": "DTL", "TEL": "TEL",
                  "BP": "BPLRT", "SK": "SKLRT", "PG": "PGLRT"}
    colours = {}
    with zipfile.ZipFile(config.RAW / "lta" / "train_gtfs.zip") as z:
        for r in csv.DictReader(io.TextIOWrapper(z.open("routes.txt"), encoding="utf-8-sig")):
            line = "CCL" if r["route_id"].startswith("CCL") else route_line.get(r["route_id"])
            if line:
                colours.setdefault(line, "#" + r["route_color"])
    lines: dict[str, list] = {}
    for line, coords in rail_ways:
        lines.setdefault(line, []).append([[round(lon, 6), round(lat, 6)] for lon, lat in coords])
    features = [{"type": "Feature", "properties": {"line": line, "colour": colours.get(line, "#888888")},
                 "geometry": {"type": "MultiLineString", "coordinates": parts}}
                for line, parts in sorted(lines.items())]
    (out / "mrt_lines.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": features}), encoding="utf-8")

    stations = [{"type": "Feature", "properties": {"name": s["name"], "codes": " / ".join(s["codes"]),
                                                   "lines": s["lines"]},
                 "geometry": {"type": "Point", "coordinates": [round(s["lon"], 6), round(s["lat"], 6)]}}
                for s in rail.stations]
    (out / "mrt_stations.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": stations}), encoding="utf-8")

    lon, lat = geo.to_lonlat(bus.stop_x, bus.stop_y)
    stops = [{"type": "Feature", "properties": {"code": c, "name": n},
              "geometry": {"type": "Point", "coordinates": [round(float(a), 6), round(float(b), 6)]}}
             for c, n, a, b in zip(bus.stop_code, bus.stop_name, lon, lat)]
    (out / "bus_stops.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": stops}), encoding="utf-8")

    # Bus route lines come as encoded polylines; decode here so the browser gets GeoJSON.
    routes = json.loads((config.RAW / "busrouter" / "routes.min.json").read_text(encoding="utf-8"))
    feats = [{"type": "Feature", "properties": {"service": svc},
              "geometry": {"type": "MultiLineString",
                           "coordinates": [[[round(a, 5), round(b, 5)] for a, b in geo.decode_polyline(p)] for p in polys]}}
             for svc, polys in sorted(routes.items())]
    (out / "bus_routes.geojson").write_text(json.dumps({"type": "FeatureCollection", "features": feats},
                                                       separators=(",", ":")), encoding="utf-8")


def build(day: date | None = None) -> None:
    t0 = time.time()
    day = reference_weekday(day)
    config.BUILD.mkdir(parents=True, exist_ok=True)
    if not fetch.OSM_PBF.exists():
        fetch.fetch_osm()

    _log(t0, "parsing OpenStreetMap ...")
    walk, drive, rail_ways = osm.parse(fetch.OSM_PBF)
    land = geo.land_polygon_xy()
    near_land = land.buffer(600)  # keeps bridges to Sentosa etc.; drops Johor
    shapely.prepare(land)
    shapely.prepare(near_land)
    walk = walk.subset(shapely.contains_xy(near_land, walk.x, walk.y))
    drive = drive.subset(shapely.contains_xy(near_land, drive.x, drive.y))
    on_land = land.buffer(50)
    shapely.prepare(on_land)
    _log(t0, f"walk {walk.n} nodes / {len(walk.u)} edges; drive {drive.n} nodes / {len(drive.u)} edges")

    walk_major = (walk.components() >= MAJOR_COMPONENT) & shapely.contains_xy(on_land, walk.x, walk.y)
    vu, vv, vd, n_blocks = voiddeck_edges(walk.x, walk.y, walk_major)
    walk.add_edges(vu, vv, vd, osm.WALK_VOIDDECK)
    walk_major = (walk.components() >= MAJOR_COMPONENT) & shapely.contains_xy(on_land, walk.x, walk.y)
    scc = drive.components("strong")
    drive_major = (scc == scc.max()) & shapely.contains_xy(on_land, drive.x, drive.y)
    _log(t0, f"{len(vu) // 2} void-deck links from {n_blocks} HDB footprints; "
             f"{walk_major.sum()} routable walk nodes, {drive_major.sum()} drive nodes")

    barrier = barriers.Barriers.load()
    _log(t0, "barrier grid: " + ("data/raw/barriers, so straight walks don't cross barriers" if barrier else
                                 "none (isochrone fetch --only barriers); straight walks ignore barriers"))
    walk_ids = np.nonzero(walk_major)[0]
    walk_pool = barriers.Pool(walk_ids, walk.x[walk_ids], walk.y[walk_ids])

    def usable_stop(x, y):
        d, _ = walk_pool.tree.query(np.column_stack([x, y]))
        return shapely.contains_xy(on_land, x, y) & (d <= STOP_SNAP_M)

    bus = transit.build_bus(usable_stop)
    _log(t0, f"bus: {bus.stats}")
    rail = transit.build_rail(day)
    _log(t0, f"rail: " + json.dumps({k: v for k, v in rail.stats.items() if k != 'active_services'}))

    stop_item, stop_node, stop_m = _snap(barrier, walk_pool, bus.stop_x, bus.stop_y, 2, STOP_SNAP_M)
    ent_item, ent_node, ent_m = _snap(barrier, walk_pool, rail.entrance_x, rail.entrance_y, 2, ENTRANCE_SNAP_M)
    missing = set(range(len(rail.entrance_id))) - set(ent_item.tolist())
    if missing:
        _log(t0, f"warning: {len(missing)} station entrances are not near a footpath")

    # Map cells also join their nearest bus stop and station exit directly, so the walk from a
    # block to an exit beside it doesn't depend on OpenStreetMap having the path. The ids
    # follow the engine's transit-graph layout: walk nodes, stops, on-bus nodes, entrances.
    o_stop, o_ent = walk.n, walk.n + len(bus.stop_x) + len(bus.ride_pattern)
    stop_pool = barriers.Pool(o_stop + np.arange(len(bus.stop_x)), bus.stop_x, bus.stop_y)
    exit_pool = barriers.Pool(o_ent + np.arange(len(rail.entrance_x)), rail.entrance_x, rail.entrance_y)
    drive_ids = np.nonzero(drive_major)[0]
    drive_pool = barriers.Pool(drive_ids, drive.x[drive_ids], drive.y[drive_ids])
    grid_arrays, grid_meta = build_grids(
        land, barrier,
        [(walk_pool, config.GRID_SNAP_K, config.GRID_MAX_SNAP_M), (exit_pool, 1, config.ACCESS_SNAP_M),
         (stop_pool, 1, config.ACCESS_SNAP_M)],
        [(drive_pool, config.GRID_SNAP_K, DRIVE_SNAP_M)])
    _log(t0, "grids: " + ", ".join(f"{k} {v['res_m']} m = {v['cells']} cells" for k, v in grid_meta.items()))

    np.savez_compressed(config.BUILD / "walk.npz", x=walk.x.astype(np.float32), y=walk.y.astype(np.float32),
                        u=walk.u, v=walk.v, length=walk.length, kind=walk.attr, major=walk_major)
    np.savez_compressed(config.BUILD / "drive.npz", x=drive.x.astype(np.float32), y=drive.y.astype(np.float32),
                        u=drive.u, v=drive.v, length=drive.length, road_class=drive.attr,
                        maxspeed=drive.maxspeed, major=drive_major)
    np.savez_compressed(
        config.BUILD / "transit.npz",
        stop_x=bus.stop_x.astype(np.float32), stop_y=bus.stop_y.astype(np.float32),
        stop_link_stop=stop_item, stop_link_node=stop_node, stop_link_m=stop_m,
        bus_wait=bus.pattern_wait, ride_pattern=bus.ride_pattern, ride_stop=bus.ride_stop, ride_hop_s=bus.ride_hop_s,
        entrance_x=rail.entrance_x.astype(np.float32), entrance_y=rail.entrance_y.astype(np.float32),
        entrance_link_entrance=ent_item, entrance_link_node=ent_node, entrance_link_m=ent_m,
        platform_x=rail.platform_x.astype(np.float32), platform_y=rail.platform_y.astype(np.float32),
        access=rail.access, transfer=rail.transfer, transfer_s=rail.transfer_s,
        hop=rail.hop, hop_wait=rail.hop_wait, hop_run=rail.hop_run, hop_dwell=rail.hop_dwell,
        through=rail.through, through_bands=rail.through_bands,
    )
    np.savez_compressed(config.BUILD / "grids.npz", **grid_arrays)
    build_overlays(rail_ways, rail, bus)

    sources = json.loads((config.RAW / "sources.json").read_text(encoding="utf-8"))
    meta = {
        "format": config.BUILD_FORMAT,
        "built_at": datetime.now().isoformat(timespec="seconds"),
        "service_date": day.isoformat(),
        "snap": {"barriers": barrier is not None, "access_m": config.ACCESS_SNAP_M,
                 "node_offsets": {"stop": int(o_stop), "entrance": int(o_ent)}},
        "sources": sources,
        "grids": grid_meta,
        "names": {"bus_stop": bus.stop_code, "bus_stop_name": bus.stop_name, "bus_pattern": bus.pattern_name,
                  "platform": rail.platform_id, "platform_code": rail.platform_code,
                  "entrance": rail.entrance_id},
        "stats": {"walk_nodes": int(walk.n), "walk_edges": int(len(walk.u)), "voiddeck_links": int(len(vu) // 2),
                  "drive_nodes": int(drive.n), "drive_edges": int(len(drive.u)),
                  "bus": bus.stats, "rail": {k: v for k, v in rail.stats.items() if k != "active_services"},
                  "rail_active_services": rail.stats["active_services"]},
    }
    (config.BUILD / "meta.json").write_text(json.dumps(meta, indent=1), encoding="utf-8")
    _log(t0, f"done -> {config.BUILD.relative_to(config.ROOT)}")
