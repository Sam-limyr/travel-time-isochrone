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
from dataclasses import dataclass

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from . import config, geo, osm

EPS = 1e-3          # seconds; stands in for zero-cost edges (explicit zeros are fragile in sparse graphs)
OFF = 1e9           # weight of a disabled edge
ORIGIN_K = 4        # the clicked point connects to this many nearby network nodes
ORIGIN_MAX_M = 1500.0
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

    def run(self, weights: np.ndarray, origin_nodes: np.ndarray, origin_s: np.ndarray, limit: float) -> np.ndarray:
        """``weights`` are per edge in sorted order, excluding the origin slots."""
        data = np.empty(self.n_edges)
        data[:-ORIGIN_K] = weights
        data[-ORIGIN_K:] = OFF
        indices = self.indices.copy()
        m = len(origin_nodes)
        indices[-ORIGIN_K:][:m] = origin_nodes
        data[-ORIGIN_K:][:m] = np.maximum(origin_s, EPS)
        graph = csr_matrix((data, indices, self.indptr), shape=(self.n, self.n))
        return dijkstra(graph, directed=True, indices=self.origin, limit=limit)


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
        self.tr = {k: tr[k] for k in ("bus_wait", "ride_hop_s", "hop_wait", "hop_run", "hop_dwell", "through",
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

    def isochrone(self, req: Request) -> dict:
        req.validate()
        t0 = time.perf_counter()
        x, y = (float(c) for c in geo.to_xy(req.lon, req.lat))
        v = req.walk_kmh / 3.6
        limit = config.MAX_MINUTES * 60
        if req.mode == "transit":
            tree, ids, graph = self.walk_tree, self.walk_ids, self.t_graph
            weights = self.transit_weights(req)
        else:
            tree, ids, graph = self.drive_tree, self.drive_ids, self.d_graph
            weights = self.drive_weights(req)
        d, i = tree.query([x, y], k=ORIGIN_K)
        if d[0] > ORIGIN_MAX_M:
            raise ValueError("That point is too far from any footpath or road in Singapore.")
        origin_nodes = ids[i]
        origin_s = d * config.STRAIGHT_LINE_DETOUR / v
        t1 = time.perf_counter()
        dist = graph.run(weights, origin_nodes, origin_s, limit)
        t2 = time.perf_counter()

        g = self.meta["grids"][req.res]
        if req.mode == "transit":
            nodes, metres = self.grids[f"{req.res}_walk_node"], self.grids[f"{req.res}_walk_m"]
            extra = 0.0
        else:
            nodes, metres = self.grids[f"{req.res}_drive_node"], self.grids[f"{req.res}_drive_m"]
            extra = req.parking_min * 60
        cell_s = (dist[nodes] + metres / v).min(axis=1) + extra
        # the origin's own neighbourhood: walking straight there can beat the network
        cells = self.grids[f"{req.res}_cell"]
        cx = g["origin_xy"][0] + (cells % g["nx"] + 0.5) * g["res_m"]
        cy = g["origin_xy"][1] - (cells // g["nx"] + 0.5) * g["res_m"]
        direct = np.hypot(cx - x, cy - y) * config.STRAIGHT_LINE_DETOUR / v
        cell_s = np.minimum(cell_s, np.where(direct < 600 / v, direct, np.inf))

        grid = np.full(g["nx"] * g["ny"], NO_DATA, np.uint16)
        reached = cell_s <= limit
        grid[cells] = np.where(reached, np.round(cell_s / 6).clip(0, UNREACHED - 1), UNREACHED).astype(np.uint16)
        t3 = time.perf_counter()

        cell_km2 = (g["res_m"] / 1000) ** 2
        minutes = cell_s / 60
        area = {str(m): round(float((minutes <= m).sum() * cell_km2), 1) for m in (15, 30, 45, 60, 90)}
        return {
            "grid": {"nx": g["nx"], "ny": g["ny"], "res_m": g["res_m"], "bounds": g["bounds"],
                     "encoding": "uint16 little-endian, tenths of a minute; 65535 = no data, 65534 = unreachable",
                     "data": base64.b64encode(grid.tobytes()).decode("ascii")},
            "origin": {"lon": req.lon, "lat": req.lat, "snap_m": round(float(d[0]), 1)},
            "area_km2": area,
            "timing_ms": {"setup": round((t1 - t0) * 1000), "dijkstra": round((t2 - t1) * 1000),
                          "grid": round((t3 - t2) * 1000)},
        }
