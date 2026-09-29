"""Travel-time computation.

The public-transport network is one directed graph:

    walk nodes <-> bus stops -> on-bus nodes (one per service direction and stop) -> bus stops
    walk nodes <-> station entrances <-> platforms -> track segments ("hops") -> platforms

Boarding edges carry the waiting time, on-vehicle edges the running time, and
interchange edges the (speed-scaled) transfer walks. Edge weights depend on the
request (time band, wait assumption, walking speed, enabled modes), so they are
recomputed per request as one vectorised pass over the edge arrays, followed by a
single Dijkstra run from the clicked point. Travel times are then sampled onto the
heatmap grid: each cell takes the best of its nearest network nodes plus the
remaining walk.
"""
from __future__ import annotations

import base64
import csv
import functools
import json
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

import numpy as np
import shapely
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from . import config, geo, osm, transit

EPS = 1e-3          # seconds; stands in for zero-cost edges (explicit zeros are fragile in sparse graphs)
OFF = 1e9           # weight of a disabled edge
ORIGIN_K = 4        # the clicked point connects to this many nearby network nodes
ORIGIN_MAX_M = 1000.0
LAND_MARGIN_M = 150.0  # points this far off the URA coastline still count as land (piers, new reclamation)
CACHE_SIZE = 12     # recent searches kept (~4.5 MB each): up to five places plus the start point
NO_DATA, UNREACHED = 65535, 65534  # grid encoding; other values are tenths of a minute

# edge kinds in the public-transport graph
(K_WALK, K_STOP_LINK, K_BUS_BOARD, K_BUS_RIDE, K_BUS_ALIGHT, K_ENT_LINK, K_STATION_IN, K_STATION_OUT,
 K_TRANSFER, K_RAIL_BOARD, K_RAIL_THROUGH, K_RAIL_ALIGHT, K_ORIGIN) = range(13)


@dataclass
class Request:
    lon: float
    lat: float
    mode: str = "transit"          # "transit" or "car"
    band: str = "am_peak"
    wait: str = "avg"              # best / avg / worst
    walk_kmh: float = config.WALK_KMH_DEFAULT
    res: str = "med"
    bus: bool = True
    rail: bool = True
    voiddeck: bool = True
    parking_min: float = 0.0
    direction: str = "from"        # "from" the point to everywhere, or "to" the point from everywhere

    def validate(self) -> None:
        if self.mode not in ("transit", "car"):
            raise ValueError("mode must be 'transit' or 'car'")
        if self.direction not in ("from", "to"):
            raise ValueError("direction must be 'from' or 'to'")
        if self.band not in config.BANDS:
            raise ValueError(f"band must be one of {list(config.BANDS)}")
        if self.wait not in config.WAIT_MODES:
            raise ValueError(f"wait must be one of {list(config.WAIT_MODES)}")
        if self.res not in config.RESOLUTIONS:
            raise ValueError(f"res must be one of {list(config.RESOLUTIONS)}")
        if not config.WALK_KMH_MIN <= self.walk_kmh <= config.WALK_KMH_MAX:
            raise ValueError(f"walk_kmh must be within {config.WALK_KMH_MIN}-{config.WALK_KMH_MAX}")
        if not 0 <= self.parking_min <= 30:
            raise ValueError("parking_min must be within 0-30")


class _Csr:
    """A CSR graph whose last node is a movable origin with ORIGIN_K outgoing edges."""

    def __init__(self, u: np.ndarray, v: np.ndarray, n_nodes: int):
        origin = n_nodes
        u = np.concatenate([u, np.full(ORIGIN_K, origin)])
        v = np.concatenate([v, np.zeros(ORIGIN_K, v.dtype)])
        self.order = np.argsort(u, kind="stable")
        self.n = n_nodes + 1
        self.origin = origin
        self.indices = v[self.order].astype(np.int32)
        self.indptr = np.concatenate([[0], np.cumsum(np.bincount(u, minlength=self.n))]).astype(np.int32)
        self.n_edges = len(u)

    def run(self, weights: np.ndarray, origin_nodes: np.ndarray, origin_s: np.ndarray,
            limit: float) -> tuple[np.ndarray, np.ndarray]:
        """Shortest times and predecessors from the origin.

        ``weights`` are per edge in sorted order, excluding the origin slots.
        """
        data = np.empty(self.n_edges)
        data[:-ORIGIN_K] = weights
        data[-ORIGIN_K:] = OFF
        indices = self.indices.copy()
        m = len(origin_nodes)
        indices[-ORIGIN_K:][:m] = origin_nodes
        data[-ORIGIN_K:][:m] = np.maximum(origin_s, EPS)
        graph = csr_matrix((data, indices, self.indptr), shape=(self.n, self.n))
        return dijkstra(graph, directed=True, indices=self.origin, limit=limit, return_predecessors=True)


def _transposed(u: np.ndarray, v: np.ndarray, n_nodes: int, fwd: _Csr) -> tuple[_Csr, np.ndarray]:
    """The graph with every edge reversed, and the permutation taking per-edge weights from
    ``fwd``'s order to its order. A search over it from a point gives each node's time *to* it."""
    rev = _Csr(v, u, n_nodes)
    m = len(u)
    pos = np.empty(m, np.int64)
    pos[fwd.order[fwd.order < m]] = np.arange(m)  # original edge -> slot in the forward order
    return rev, pos[rev.order[rev.order < m]].astype(np.int32)


def _serialised(method):
    """Run one engine query at a time. The server answers requests on several threads,
    and neither GEOS prepared geometries (the land check) nor the search cache are
    thread-safe: overlapping queries crash the process."""
    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return method(self, *args, **kwargs)
    return wrapper


def _land_cells(land, g: dict) -> int:
    """Cells of grid ``g`` whose centre is on land. The grid itself keeps only those
    within reach of a footpath; the rest (forest, reservoirs, airfields, military and
    industrial islands) still count towards the island's area."""
    col, row = np.meshgrid(np.arange(g["nx"]), np.arange(g["ny"]))
    cx = g["origin_xy"][0] + (col.ravel() + 0.5) * g["res_m"]
    cy = g["origin_xy"][1] - (row.ravel() + 0.5) * g["res_m"]
    return int(shapely.contains_xy(land, cx, cy).sum())


class Engine:
    def __init__(self) -> None:
        b = config.BUILD
        if not (b / "meta.json").exists():
            raise FileNotFoundError("No network build found. Run `python -m isochrone build` first.")
        self.meta = json.loads((b / "meta.json").read_text(encoding="utf-8"))
        walk, drive, tr = np.load(b / "walk.npz"), np.load(b / "drive.npz"), np.load(b / "transit.npz")
        self.grids = dict(np.load(b / "grids.npz"))
        self.walk_x, self.walk_y = walk["x"].astype(np.float64), walk["y"].astype(np.float64)
        self.drive_x, self.drive_y = drive["x"].astype(np.float64), drive["y"].astype(np.float64)
        self.W = len(self.walk_x)
        wm, dm = np.nonzero(walk["major"])[0], np.nonzero(drive["major"])[0]
        self.walk_ids, self.walk_tree = wm, cKDTree(np.column_stack([self.walk_x[wm], self.walk_y[wm]]))
        self.drive_ids, self.drive_tree = dm, cKDTree(np.column_stack([self.drive_x[dm], self.drive_y[dm]]))
        self._build_transit(walk, tr)
        self._build_drive(drive)
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        for name in ("stop_x", "stop_y", "entrance_x", "entrance_y", "platform_x", "platform_y"):
            setattr(self, name, tr[name].astype(np.float64))
        self.ride_stop, self.ride_pattern = tr["ride_stop"], tr["ride_pattern"]
        self.platform_line = [transit.line_of(c) for c in self.meta["names"]["platform_code"]]
        self.rail_sections = {}  # published frequency row -> hops it sets
        if config.TRAIN_FREQUENCIES.exists():
            self.tr["hop_wait"], self.rail_sections = transit.published_rail_waits(
                config.TRAIN_FREQUENCIES, self.meta["names"]["platform_code"], self.tr["hop"], self.tr["hop_wait"])
        self.hop_factor = np.array([config.RAIL_RUNTIME_FACTOR.get(self.platform_line[p], 1.0)
                                    for p in tr["hop"][:, 0]])
        stations = json.loads((b / "overlays" / "mrt_stations.geojson").read_text(encoding="utf-8"))
        self.station_names = {code: f["properties"]["name"] for f in stations["features"]
                              for code in f["properties"]["codes"].split(" / ")}
        self._load_key_destinations(stations)
        land = geo.land_polygon_xy()
        shapely.prepare(land)
        self.land_cells = {name: _land_cells(land, g) for name, g in self.meta["grids"].items()}
        self.land = land.buffer(LAND_MARGIN_M)
        shapely.prepare(self.land)

    def _check_on_land(self, x: float, y: float) -> None:
        if not shapely.contains_xy(self.land, x, y):
            raise ValueError("Pick a point on Singapore's land (the sea and Johor are outside the network).")

    def _load_key_destinations(self, stations: dict) -> None:
        """The weighted places behind the key-destinations score, snapped to both networks.
        Rows without coordinates name a station; names that match none are skipped."""
        where = {f["properties"]["name"].casefold(): f["geometry"]["coordinates"] for f in stations["features"]}
        self.key_dest, self.key_skipped = [], []
        path = config.KEY_DESTINATIONS
        if not path.exists():
            return
        with path.open(encoding="utf-8-sig", newline="") as fh:
            for r in csv.DictReader(fh):
                name = r["name"].strip()
                try:
                    weight = float(r["weight"])
                    if r.get("lon") and r.get("lat"):
                        lon, lat = float(r["lon"]), float(r["lat"])
                    else:
                        lon, lat = where.get(name.casefold(), (None, None))
                except ValueError as exc:
                    raise ValueError(f"{path.name}: bad weight or coordinates for {name!r}") from exc
                if lon is None:
                    self.key_skipped.append(name)
                    continue
                self.key_dest.append({"group": r["group"].strip(), "name": name, "weight": weight,
                                      "lon": lon, "lat": lat})
        if self.key_dest:
            pts = np.column_stack(geo.to_xy([r["lon"] for r in self.key_dest], [r["lat"] for r in self.key_dest]))
            self.key_xy = pts
            self.key_weight = np.array([r["weight"] for r in self.key_dest])
            self.key_snap = {}
            for kind, tree, ids in (("transit", self.walk_tree, self.walk_ids), ("car", self.drive_tree, self.drive_ids)):
                d, i = tree.query(pts, k=ORIGIN_K)
                self.key_snap[kind] = (ids[i], d * config.STRAIGHT_LINE_DETOUR)

    def _key_destinations(self, req: Request, dist: np.ndarray, x: float, y: float) -> dict | None:
        """Travel time from the origin to each key destination, and their weighted average.
        Destinations beyond the routing cut-off count as the cut-off."""
        if not self.key_dest:
            return None
        v = req.walk_kmh / 3.6
        nodes, metres = self.key_snap[req.mode]
        extra = req.parking_min * 60 if req.mode == "car" else 0.0
        t = (dist[nodes] + metres / v).min(axis=1) + extra
        direct = np.hypot(self.key_xy[:, 0] - x, self.key_xy[:, 1] - y) * config.STRAIGHT_LINE_DETOUR / v
        t = np.minimum(t, np.where(direct < 600 / v, direct, np.inf))
        limit = config.MAX_MINUTES * 60
        reached = t <= limit
        minutes = np.where(reached, t, limit) / 60
        w = self.key_weight

        def average(sel: np.ndarray) -> float | None:
            return round(float((w[sel] * minutes[sel]).sum() / w[sel].sum()), 1) if w[sel].sum() > 0 else None

        groups = list(dict.fromkeys(r["group"] for r in self.key_dest))
        in_group = {g: np.array([r["group"] == g for r in self.key_dest]) for g in groups}
        return {
            "weighted_min": average(np.ones(len(w), bool)),
            "unreachable": int((~reached).sum()),
            "groups": [{"name": g, "weight": round(float(w[in_group[g]].sum()), 2), "minutes": average(in_group[g])}
                       for g in groups],
            "items": [r | {"minutes": round(float(m), 1) if ok else None}
                      for r, m, ok in zip(self.key_dest, minutes, reached)],
        }

    # --- graph assembly ---------------------------------------------------------

    def _build_transit(self, walk, tr) -> None:
        W = self.W
        S = len(tr["stop_x"])
        R = len(tr["ride_pattern"])
        E = len(tr["entrance_x"])
        P = len(tr["platform_x"])
        H = len(tr["hop"])
        o_stop, o_ride, o_ent, o_plat, o_hop = W, W + S, W + S + R, W + S + R + E, W + S + R + E + P
        n_nodes = o_hop + H

        us, vs, kinds, params = [], [], [], []

        def add(u, v, kind, param):
            us.append(np.asarray(u, np.int64))
            vs.append(np.asarray(v, np.int64))
            kinds.append(np.full(len(us[-1]), kind, np.int8))
            params.append(np.asarray(param, np.float64))

        add(walk["u"], walk["v"], K_WALK, np.arange(len(walk["u"])))
        sl_stop, sl_node, sl_m = tr["stop_link_stop"], tr["stop_link_node"], tr["stop_link_m"]
        add(o_stop + sl_stop, sl_node, K_STOP_LINK, sl_m)
        add(sl_node, o_stop + sl_stop, K_STOP_LINK, sl_m)

        ride_pat, ride_stop, ride_hop = tr["ride_pattern"], tr["ride_stop"], tr["ride_hop_s"]
        has_next = ~np.isnan(ride_hop)
        r = np.nonzero(has_next)[0]
        add(o_stop + ride_stop[r], o_ride + r, K_BUS_BOARD, ride_pat[r])
        add(o_ride + r, o_ride + r + 1, K_BUS_RIDE, r)
        first = np.ones(R, bool)
        first[1:] = ride_pat[1:] != ride_pat[:-1]
        a = np.nonzero(~first)[0]
        add(o_ride + a, o_stop + ride_stop[a], K_BUS_ALIGHT, np.zeros(len(a)))

        el_ent, el_node, el_m = tr["entrance_link_entrance"], tr["entrance_link_node"], tr["entrance_link_m"]
        add(o_ent + el_ent, el_node, K_ENT_LINK, el_m)
        add(el_node, o_ent + el_ent, K_ENT_LINK, el_m)
        acc, acc_m = tr["access"], tr["access_m"]
        add(o_ent + acc[:, 0], o_plat + acc[:, 1], K_STATION_IN, acc_m)
        add(o_plat + acc[:, 1], o_ent + acc[:, 0], K_STATION_OUT, acc_m)
        trf = tr["transfer"]
        add(o_plat + trf[:, 0], o_plat + trf[:, 1], K_TRANSFER, tr["transfer_s"])
        hop = tr["hop"]
        add(o_plat + hop[:, 0], o_hop + np.arange(H), K_RAIL_BOARD, np.arange(H))
        thr = tr["through"]
        add(o_hop + thr[:, 0], o_hop + thr[:, 1], K_RAIL_THROUGH, np.arange(len(thr)))
        add(o_hop + np.arange(H), o_plat + hop[:, 1], K_RAIL_ALIGHT, np.arange(H))

        u, v = np.concatenate(us), np.concatenate(vs)
        self.t_graph = _Csr(u, v, n_nodes)
        self.t_graph_rev, self.t_rev_perm = _transposed(u, v, n_nodes, self.t_graph)
        o = self.t_graph.order
        o = o[o < len(u)]  # origin slots sort to the end (origin is the largest node id)
        self.t_kind = np.concatenate(kinds)[o]
        self.t_param = np.concatenate(params)[o]
        self.t_walk_kind = np.zeros(len(o), np.int8)
        wsel = self.t_kind == K_WALK
        self.t_walk_len = np.zeros(len(o))
        self.t_walk_len[wsel] = walk["length"][self.t_param[wsel].astype(np.int64)]
        self.t_walk_kind[wsel] = walk["kind"][self.t_param[wsel].astype(np.int64)]
        self.t_sel = {k: np.nonzero(self.t_kind == k)[0] for k in range(K_ORIGIN)}
        self.tr = {k: tr[k] for k in ("bus_wait", "ride_hop_s", "hop", "hop_wait", "hop_run", "hop_dwell", "through",
                                        "through_bands", "transfer_s")}
        self.offsets = {"stop": o_stop, "ride": o_ride, "entrance": o_ent, "platform": o_plat, "hop": o_hop}

    def _build_drive(self, drive) -> None:
        u, v = drive["u"].astype(np.int64), drive["v"].astype(np.int64)
        self.d_graph = _Csr(u, v, len(self.drive_x))
        self.d_graph_rev, self.d_rev_perm = _transposed(u, v, len(self.drive_x), self.d_graph)
        o = self.d_graph.order
        o = o[o < len(drive["u"])]
        self.d_len = drive["length"][o].astype(np.float64)
        self.d_class = drive["road_class"][o]
        self.d_maxspeed = drive["maxspeed"][o].astype(np.float64)

    # --- weights ----------------------------------------------------------------

    def transit_weights(self, req: Request) -> np.ndarray:
        b = config.BAND_KEYS.index(req.band)
        wmode = config.WAIT_MODES.index(req.wait)
        v = req.walk_kmh / 3.6
        s, p, tr = self.t_sel, self.t_param, self.tr
        w = np.empty(len(self.t_kind))

        idx = s[K_WALK]
        factor = np.array([1.0, config.STEPS_FACTOR, config.VOIDDECK_FACTOR if req.voiddeck else np.inf])
        w[idx] = self.t_walk_len[idx] * factor[self.t_walk_kind[idx]] / v
        for k in (K_STOP_LINK, K_ENT_LINK):
            w[s[k]] = p[s[k]] / v

        idx = s[K_BUS_BOARD]
        w[idx] = tr["bus_wait"][p[idx].astype(np.int64), b, wmode] if req.bus else OFF
        idx = s[K_BUS_RIDE]
        w[idx] = tr["ride_hop_s"][p[idx].astype(np.int64)] * config.BUS_BAND_FACTOR[req.band]
        w[s[K_BUS_ALIGHT]] = EPS

        idx = s[K_STATION_IN]
        w[idx] = (config.STATION_ENTRY_S + p[idx] / v) if req.rail else OFF
        idx = s[K_STATION_OUT]
        w[idx] = config.STATION_EXIT_S + p[idx] / v
        idx = s[K_TRANSFER]
        w[idx] = p[idx] * config.LEISURELY_WALK_KMH / req.walk_kmh
        idx = s[K_RAIL_BOARD]
        w[idx] = tr["hop_wait"][p[idx].astype(np.int64), b, wmode]
        idx = s[K_RAIL_THROUGH]
        t = p[idx].astype(np.int64)
        first_hop = tr["through"][t, 0]
        runs = (tr["hop_run"][first_hop, b] + tr["hop_dwell"][first_hop, b]) * self.hop_factor[first_hop]
        w[idx] = np.where((tr["through_bands"][t] >> b) & 1 == 1, runs, OFF)
        idx = s[K_RAIL_ALIGHT]
        h = p[idx].astype(np.int64)
        w[idx] = tr["hop_run"][h, b] * self.hop_factor[h]

        w[~np.isfinite(w)] = OFF  # NaN = not running in this band; inf = disabled
        return np.maximum(w, EPS)

    def drive_weights(self, req: Request) -> np.ndarray:
        b = config.BAND_KEYS.index(req.band)
        speeds = np.array([config.CAR_SPEEDS[c][b] for c in osm.DRIVE_CLASSES], np.float64)
        kmh = speeds[self.d_class]
        tagged = self.d_maxspeed > 0
        kmh[tagged] = np.minimum(kmh[tagged], self.d_maxspeed[tagged])
        return np.maximum(self.d_len / (kmh / 3.6), EPS)

    # --- queries ----------------------------------------------------------------

    def _search(self, req: Request) -> tuple[np.ndarray, np.ndarray, float, float, dict]:
        """Dijkstra from the request's point, or for direction "to", towards it over the
        reversed graph (each node's time to reach the point). Cached: switching resolution,
        parking allowance or asking for a route reuses the same search."""
        key = (req.mode, req.direction, round(req.lon, 6), round(req.lat, 6), req.band, req.wait, req.walk_kmh,
               req.bus, req.rail, req.voiddeck)
        x, y = (float(c) for c in geo.to_xy(req.lon, req.lat))
        self._check_on_land(x, y)
        if key in self._cache:
            self._cache.move_to_end(key)
            dist, pred, snap_m = self._cache[key]
            return dist, pred, x, y, {"cached": True, "snap_m": snap_m}
        t0 = time.perf_counter()
        v = req.walk_kmh / 3.6
        forward = req.direction == "from"
        if req.mode == "transit":
            tree, ids = self.walk_tree, self.walk_ids
            weights = self.transit_weights(req)
            graph, perm = (self.t_graph, None) if forward else (self.t_graph_rev, self.t_rev_perm)
        else:
            tree, ids = self.drive_tree, self.drive_ids
            weights = self.drive_weights(req)
            graph, perm = (self.d_graph, None) if forward else (self.d_graph_rev, self.d_rev_perm)
        if perm is not None:
            weights = weights[perm]
        d, i = tree.query([x, y], k=ORIGIN_K)
        if d[0] > ORIGIN_MAX_M:
            raise ValueError("That point is too far from any footpath or road in Singapore.")
        t1 = time.perf_counter()
        dist, pred = graph.run(weights, ids[i], d * config.STRAIGHT_LINE_DETOUR / v, config.MAX_MINUTES * 60)
        t2 = time.perf_counter()
        self._cache[key] = (dist, pred, float(d[0]))
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return dist, pred, x, y, {"cached": False, "snap_m": float(d[0]),
                                  "weights_ms": round((t1 - t0) * 1000), "dijkstra_ms": round((t2 - t1) * 1000)}

    def _cell_times(self, req: Request, dist: np.ndarray, x: float, y: float) -> tuple[np.ndarray, np.ndarray]:
        """Seconds to reach each land cell of the requested grid (and the cell ids)."""
        g = self.meta["grids"][req.res]
        v = req.walk_kmh / 3.6
        kind = "walk" if req.mode == "transit" else "drive"
        nodes, metres = self.grids[f"{req.res}_{kind}_node"], self.grids[f"{req.res}_{kind}_m"]
        extra = req.parking_min * 60 if req.mode == "car" else 0.0
        cell_s = (dist[nodes] + metres / v).min(axis=1) + extra
        # near the origin, walking straight there can beat the network
        cells = self.grids[f"{req.res}_cell"]
        cx = g["origin_xy"][0] + (cells % g["nx"] + 0.5) * g["res_m"]
        cy = g["origin_xy"][1] - (cells // g["nx"] + 0.5) * g["res_m"]
        direct = np.hypot(cx - x, cy - y) * config.STRAIGHT_LINE_DETOUR / v
        return np.minimum(cell_s, np.where(direct < 600 / v, direct, np.inf)), cells

    @_serialised
    def isochrone(self, req: Request) -> dict:
        req.validate()
        t0 = time.perf_counter()
        dist, _, x, y, info = self._search(req)
        t1 = time.perf_counter()
        cell_s, cells = self._cell_times(req, dist, x, y)
        g = self.meta["grids"][req.res]
        limit = config.MAX_MINUTES * 60
        grid = np.full(g["nx"] * g["ny"], NO_DATA, np.uint16)
        grid[cells] = np.where(cell_s <= limit, np.round(cell_s / 6).clip(0, UNREACHED - 1),
                               UNREACHED).astype(np.uint16)
        t2 = time.perf_counter()
        cell_km2 = (g["res_m"] / 1000) ** 2
        minutes = cell_s / 60
        return {
            "grid": {"nx": g["nx"], "ny": g["ny"], "res_m": g["res_m"], "bounds": g["bounds"],
                     "land_cells": self.land_cells[req.res],
                     "encoding": "uint16 little-endian, tenths of a minute; 65535 = no data, 65534 = unreachable",
                     "data": base64.b64encode(grid.astype("<u2").tobytes()).decode("ascii")},
            "origin": {"lon": req.lon, "lat": req.lat, "snap_m": round(info["snap_m"], 1)},
            "direction": req.direction,
            "area_km2": {str(m): round(float((minutes <= m).sum() * cell_km2), 1) for m in (15, 30, 45, 60, 90)},
            "key_destinations": self._key_destinations(req, dist, x, y) if req.direction == "from" else None,
            "timing_ms": {"search": round((t1 - t0) * 1000), "grid": round((t2 - t1) * 1000),
                          "cached": info["cached"]},
        }

    # --- itineraries ------------------------------------------------------------

    @_serialised
    def route(self, req: Request, to_lon: float, to_lat: float) -> dict:
        """Fastest itinerary from the request origin to a point, as legs with geometry."""
        req.validate()
        dist, pred, x, y, _ = self._search(req)
        v = req.walk_kmh / 3.6
        tx, ty = (float(c) for c in geo.to_xy(to_lon, to_lat))
        self._check_on_land(tx, ty)
        tree, ids =(self.walk_tree, self.walk_ids) if req.mode == "transit" else (self.drive_tree, self.drive_ids)
        d, i = tree.query([tx, ty], k=ORIGIN_K)
        cand = dist[ids[i]] + d * config.STRAIGHT_LINE_DETOUR / v
        best = int(np.argmin(cand))
        direct = np.hypot(tx - x, ty - y) * config.STRAIGHT_LINE_DETOUR / v
        extra = req.parking_min * 60 if req.mode == "car" else 0.0
        if not np.isfinite(cand[best]) and direct >= 600 / v:
            return {"reachable": False}
        if direct < 600 / v and direct <= cand[best] + extra:
            return {"reachable": True, "total_s": round(direct), "legs": [
                {"type": "walk", "seconds": round(direct), "coords": [[req.lon, req.lat], [to_lon, to_lat]]}]}

        path = [int(ids[i[best]])]
        while pred[path[-1]] >= 0:
            path.append(int(pred[path[-1]]))
        path.reverse()
        origin = (self.t_graph if req.mode == "transit" else self.d_graph).origin
        if path[0] == origin:
            path = path[1:]
        if req.mode == "transit":
            legs = self._transit_legs(path, dist)
        else:
            legs = [{"type": "drive", "seconds": round(dist[path[-1]] - dist[path[0]]),
                     "coords": self._coords(path, drive=True)}]
            if extra:
                legs.append({"type": "park", "seconds": round(extra)})
        legs.insert(0, {"type": "walk", "seconds": round(dist[path[0]]),
                        "coords": [[req.lon, req.lat]] + self._coords(path[:1], drive=req.mode == "car")})
        tail = d[best] * config.STRAIGHT_LINE_DETOUR / v
        legs.append({"type": "walk", "seconds": round(tail),
                     "coords": self._coords(path[-1:], drive=req.mode == "car") + [[to_lon, to_lat]]})
        merged = []
        for leg in legs:  # fold consecutive walks together
            if merged and leg["type"] == "walk" == merged[-1]["type"]:
                merged[-1]["seconds"] += leg["seconds"]
                merged[-1]["coords"] += leg["coords"]
            else:
                merged.append(leg)
        return {"reachable": True, "total_s": round(cand[best] + extra), "legs": [m for m in merged if m["seconds"] > 0 or m["type"] != "walk"]}

    def _node_xy(self, n: int) -> tuple[float, float]:
        o = self.offsets
        if n < o["stop"]:
            return self.walk_x[n], self.walk_y[n]
        if n < o["ride"]:
            return self.stop_x[n - o["stop"]], self.stop_y[n - o["stop"]]
        if n < o["entrance"]:
            s = self.ride_stop[n - o["ride"]]
            return self.stop_x[s], self.stop_y[s]
        if n < o["platform"]:
            return self.entrance_x[n - o["entrance"]], self.entrance_y[n - o["entrance"]]
        if n < o["hop"]:
            return self.platform_x[n - o["platform"]], self.platform_y[n - o["platform"]]
        p = self.tr["hop"][n - o["hop"], 0]
        return self.platform_x[p], self.platform_y[p]

    def _coords(self, nodes: list[int], drive: bool = False) -> list[list[float]]:
        if drive:
            xs, ys = self.drive_x[nodes], self.drive_y[nodes]
        else:
            xs, ys = zip(*(self._node_xy(n) for n in nodes)) if nodes else ([], [])
        lon, lat = geo.to_lonlat(np.array(xs), np.array(ys))
        return [[round(float(a), 6), round(float(b), 6)] for a, b in zip(np.atleast_1d(lon), np.atleast_1d(lat))]

    def _node_type(self, n: int) -> str:
        o = self.offsets
        for name, start in (("hop", o["hop"]), ("platform", o["platform"]), ("entrance", o["entrance"]),
                            ("ride", o["ride"]), ("stop", o["stop"])):
            if n >= start:
                return name
        return "walk"

    def _transit_legs(self, path: list[int], dist: np.ndarray) -> list[dict]:
        o, names = self.offsets, self.meta["names"]
        types = [self._node_type(n) for n in path]
        legs, i = [], 0
        walk_start = 0
        while i < len(path) - 1:
            a, t_b = path[i], types[i + 1]
            if types[i] == "stop" and t_b == "ride" or types[i] == "platform" and t_b == "hop":
                if i > walk_start:
                    legs.append({"type": "walk", "seconds": round(dist[a] - dist[path[walk_start]]),
                                 "coords": self._coords(path[walk_start:i + 1])})
                j = i + 1
                while j + 1 < len(path) and types[j + 1] == t_b:
                    j += 1
                board, alight = a, path[j + 1]
                wait = dist[path[i + 1]] - dist[board]
                if t_b == "ride":
                    pattern = int(self.ride_pattern[path[i + 1] - o["ride"]])
                    leg = {"type": "bus", "service": names["bus_pattern"][pattern].split(" ")[0],
                           "from": self._stop_label(board - o["stop"]), "to": self._stop_label(alight - o["stop"])}
                else:
                    p0, p1 = board - o["platform"], alight - o["platform"]
                    leg = {"type": "train", "line": self.platform_line[p0],
                           "from": self.station_label(p0), "to": self.station_label(p1)}
                leg |= {"stops": j - i, "wait_s": round(wait), "ride_s": round(dist[alight] - dist[path[i + 1]]),
                        "seconds": round(dist[alight] - dist[board]), "coords": self._coords(path[i:j + 2])}
                legs.append(leg)
                i = j + 1
                walk_start = i
            elif types[i] == "platform" and t_b == "platform":
                if i > walk_start:
                    legs.append({"type": "walk", "seconds": round(dist[a] - dist[path[walk_start]]),
                                 "coords": self._coords(path[walk_start:i + 1])})
                legs.append({"type": "transfer", "at": self.station_label(a - o["platform"]),
                             "seconds": round(dist[path[i + 1]] - dist[a]), "coords": self._coords(path[i:i + 2])})
                i += 1
                walk_start = i
            else:
                i += 1
        if len(path) - 1 > walk_start:
            legs.append({"type": "walk", "seconds": round(dist[path[-1]] - dist[path[walk_start]]),
                         "coords": self._coords(path[walk_start:])})
        return legs

    def _stop_label(self, s: int) -> str:
        return f"{self.meta['names']['bus_stop_name'][s]} ({self.meta['names']['bus_stop'][s]})"

    def station_label(self, p: int) -> str:
        return f"{self.station_names.get(self.meta['names']['platform_code'][p], '?')} ({self.meta['names']['platform_code'][p]})"
