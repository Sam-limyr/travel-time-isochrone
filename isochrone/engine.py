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
import json
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

    def validate(self) -> None:
        if self.mode not in ("transit", "car"):
            raise ValueError("mode must be 'transit' or 'car'")
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
        for name in ("stop_x", "stop_y", "entrance_x", "entrance_y", "platform_x", "platform_y"):
            setattr(self, name, tr[name].astype(np.float64))
        self.ride_stop, self.ride_pattern = tr["ride_stop"], tr["ride_pattern"]
        self.platform_line = [transit.line_of(c) for c in self.meta["names"]["platform_code"]]
        stations = json.loads((b / "overlays" / "mrt_stations.geojson").read_text(encoding="utf-8"))
        self.station_names = {code: f["properties"]["name"] for f in stations["features"]
                              for code in f["properties"]["codes"].split(" / ")}
        self.land = geo.land_polygon_xy().buffer(LAND_MARGIN_M)
        shapely.prepare(self.land)

    def _check_on_land(self, x: float, y: float) -> None:
        if not shapely.contains_xy(self.land, x, y):
            raise ValueError("Pick a point on Singapore's land (the sea and Johor are outside the network).")

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
        self.d_graph = _Csr(drive["u"].astype(np.int64), drive["v"].astype(np.int64), len(self.drive_x))
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
        runs = tr["hop_run"][first_hop, b] + tr["hop_dwell"][first_hop, b]
        w[idx] = np.where((tr["through_bands"][t] >> b) & 1 == 1, runs, OFF)
        idx = s[K_RAIL_ALIGHT]
        w[idx] = tr["hop_run"][p[idx].astype(np.int64), b]

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
        """Dijkstra from the request origin, cached: switching resolution, parking
        allowance or asking for a route reuses the same search."""
        key = (req.mode, round(req.lon, 6), round(req.lat, 6), req.band, req.wait, req.walk_kmh,
               req.bus, req.rail, req.voiddeck)
        x, y = (float(c) for c in geo.to_xy(req.lon, req.lat))
        self._check_on_land(x, y)
        if key in self._cache:
            self._cache.move_to_end(key)
            dist, pred, snap_m = self._cache[key]
            return dist, pred, x, y, {"cached": True, "snap_m": snap_m}
        t0 = time.perf_counter()
        v = req.walk_kmh / 3.6
        if req.mode == "transit":
            tree, ids, graph = self.walk_tree, self.walk_ids, self.t_graph
            weights = self.transit_weights(req)
        else:
            tree, ids, graph = self.drive_tree, self.drive_ids, self.d_graph
            weights = self.drive_weights(req)
        d, i = tree.query([x, y], k=ORIGIN_K)
        if d[0] > ORIGIN_MAX_M:
            raise ValueError("That point is too far from any footpath or road in Singapore.")
        t1 = time.perf_counter()
        dist, pred = graph.run(weights, ids[i], d * config.STRAIGHT_LINE_DETOUR / v, config.MAX_MINUTES * 60)
        t2 = time.perf_counter()
        self._cache[key] = (dist, pred, float(d[0]))
        while len(self._cache) > 8:
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
                     "encoding": "uint16 little-endian, tenths of a minute; 65535 = no data, 65534 = unreachable",
                     "data": base64.b64encode(grid.tobytes()).decode("ascii")},
            "origin": {"lon": req.lon, "lat": req.lat, "snap_m": round(info["snap_m"], 1)},
            "area_km2": {str(m): round(float((minutes <= m).sum() * cell_km2), 1) for m in (15, 30, 45, 60, 90)},
            "timing_ms": {"search": round((t1 - t0) * 1000), "grid": round((t2 - t1) * 1000),
                          "cached": info["cached"]},
        }

    # --- itineraries ------------------------------------------------------------

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
