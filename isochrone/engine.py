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
import functools
import json
import threading
import time
import tomllib
from collections import OrderedDict
from dataclasses import dataclass, replace

import numpy as np
import shapely
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra
from scipy.spatial import cKDTree

from . import barriers, config, geo, osm, transit

EPS = 1e-3          # seconds; stands in for zero-cost edges (explicit zeros are fragile in sparse graphs)
OFF = 1e9           # weight of a disabled edge
ORIGIN_K = 6        # links from the clicked point: its FOOTPATH_K nearest footpaths, nearest exit and stop
FOOTPATH_K = 4
ORIGIN_MAX_M = 1000.0
DIRECT_MAX_M = 600.0  # near the origin, spots under this walk (500 m straight) can be walked to directly
LAND_MARGIN_M = 150.0  # points this far off the URA coastline still count as land (piers, new reclamation)
CACHE_SIZE = 12     # recent searches kept (~4.5 MB each): up to five places plus the start point
PLACE_CACHE_BYTES = 96 * 2**20  # profile maps: each place's times on a grid, kept per setting
PROFILE_CACHE_SIZE = 16         # finished profile maps kept
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
            limit: float, predecessors: bool = True):
        """Shortest times (and predecessors, unless not wanted) from the origin.

        ``weights`` are per edge in sorted order, excluding the origin slots.
        """
        data = np.empty(self.n_edges)
        data[:-ORIGIN_K] = weights
        data[-ORIGIN_K:] = OFF
        indices = self.indices.copy()
        m = len(origin_nodes)
        indices[-ORIGIN_K:][:m] = origin_nodes
        data[-ORIGIN_K:][:m] = np.where(np.isfinite(origin_s), np.maximum(origin_s, EPS), OFF)  # inf: no usable link
        graph = csr_matrix((data, indices, self.indptr), shape=(self.n, self.n))
        return dijkstra(graph, directed=True, indices=self.origin, limit=limit, return_predecessors=predecessors)


def _transposed(u: np.ndarray, v: np.ndarray, n_nodes: int, fwd: _Csr) -> tuple[_Csr, np.ndarray]:
    """The graph with every edge reversed, and the permutation taking per-edge weights from
    ``fwd``'s order to its order. A search over it from a point gives each node's time *to* it."""
    rev = _Csr(v, u, n_nodes)
    m = len(u)
    pos = np.empty(m, np.int64)
    pos[fwd.order[fwd.order < m]] = np.arange(m)  # original edge -> slot in the forward order
    return rev, pos[rev.order[rev.order < m]].astype(np.int32)


def _tenths(seconds: np.ndarray) -> np.ndarray:
    """Seconds in the grid encoding: tenths of a minute, UNREACHED beyond the routing cut-off."""
    limit = config.MAX_MINUTES * 60
    return np.where(seconds <= limit, np.round(seconds / 6).clip(0, UNREACHED - 1), UNREACHED).astype(np.uint16)


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
        if self.meta.get("format") != config.BUILD_FORMAT:
            raise FileNotFoundError("The network build is from an older version of this app. "
                                    "Run `python -m isochrone build` (run.sh does this by itself).")
        walk, drive, tr = np.load(b / "walk.npz"), np.load(b / "drive.npz"), np.load(b / "transit.npz")
        self.grids = dict(np.load(b / "grids.npz"))
        self.walk_x, self.walk_y = walk["x"].astype(np.float64), walk["y"].astype(np.float64)
        self.drive_x, self.drive_y = drive["x"].astype(np.float64), drive["y"].astype(np.float64)
        self.W = len(self.walk_x)
        self._build_transit(walk, tr)
        self._build_drive(drive)
        # Points join the network by straight walks that cross no barrier (as the build joined
        # map cells): to footpaths, bus stops and station exits, or to roads for a car.
        self.barriers = barriers.Barriers.load()
        o = self.offsets
        if self.meta["snap"]["node_offsets"] != {"stop": o["stop"], "entrance": o["entrance"]}:
            raise RuntimeError("data/build does not match this code's transit-graph layout; rebuild it.")
        wm, dm = np.nonzero(walk["major"])[0], np.nonzero(drive["major"])[0]
        self.pools = {
            "walk": barriers.Pool(wm, self.walk_x[wm], self.walk_y[wm]),
            "exit": barriers.Pool(o["entrance"] + np.arange(len(tr["entrance_x"])), tr["entrance_x"], tr["entrance_y"]),
            "stop": barriers.Pool(o["stop"] + np.arange(len(tr["stop_x"])), tr["stop_x"], tr["stop_y"]),
            "car": barriers.Pool(dm, self.drive_x[dm], self.drive_y[dm]),
        }
        self._cache: OrderedDict = OrderedDict()
        self._lock = threading.Lock()
        # profile maps run outside self._lock (their searches take seconds), under their own lock
        self._profile_lock = threading.Lock()
        self._place_cache: OrderedDict = OrderedDict()
        self._place_cache_bytes = 0
        self._profile_cache: OrderedDict = OrderedDict()
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
        self._station_access(tr["access"])
        stations = json.loads((b / "overlays" / "mrt_stations.geojson").read_text(encoding="utf-8"))
        self.station_names = {code: f["properties"]["name"] for f in stations["features"]
                              for code in f["properties"]["codes"].split(" / ")}
        self._load_key_destinations(stations)
        land = geo.land_polygon_xy()
        shapely.prepare(land)
        self.land_cells = {name: _land_cells(land, g) for name, g in self.meta["grids"].items()}
        self.land = land.buffer(LAND_MARGIN_M)
        shapely.prepare(self.land)

    def _station_access(self, access: np.ndarray) -> None:
        """Per entrance-platform pair: the station's street-to-platform seconds (at the
        default walking pace) and the metres walked on top, from entrances beyond the
        platform's span (see config.STATION_SPAN_M)."""
        codes = self.meta["names"]["platform_code"]
        self.platform_access_s = np.full(len(codes), config.STATION_ACCESS_FALLBACK_S)
        self.station_access = []  # the file's rows, for the About panel
        if config.STATION_ACCESS.exists():
            self.platform_access_s, self.station_access = transit.station_access_seconds(config.STATION_ACCESS, codes)
        e, p = access[:, 0], access[:, 1]
        d = np.hypot(self.entrance_x[e] - self.platform_x[p], self.entrance_y[e] - self.platform_y[p])
        rise_m = self.platform_access_s[p] / config.STATION_ACCESS_S_PER_M
        span = config.STATION_SPAN_M + rise_m * config.ESCALATOR_RUN_PER_M
        self.access_s = self.platform_access_s[p]
        self.access_walk_m = np.maximum(d - span, 0) * config.STRAIGHT_LINE_DETOUR

    def _check_on_land(self, x: float, y: float) -> None:
        if not shapely.contains_xy(self.land, x, y):
            raise ValueError("Pick a point on Singapore's land (the sea and Johor are outside the network).")

    def _load_key_destinations(self, stations: dict) -> None:
        """The preset profiles behind the key-destinations score, and their places snapped to
        both networks. A place is a station's name or has coordinates; names that match no
        station are skipped (and listed)."""
        where = {f["properties"]["name"].casefold(): f["geometry"]["coordinates"] for f in stations["features"]}
        self.key_profiles, self.key_places = [], []  # places are shared between profiles
        path = config.KEY_DESTINATIONS
        if not path.exists():
            return
        place_ids: dict[tuple, int] = {}
        for key, profile in tomllib.loads(path.read_text(encoding="utf-8")).items():
            items, skipped = [], []
            for group, places in profile.get("groups", {}).items():
                for name, spec in places.items():
                    spec = spec if isinstance(spec, dict) else {"weight": spec}
                    try:
                        weight = float(spec["weight"])
                        lon, lat = ((float(spec["lon"]), float(spec["lat"])) if "lon" in spec and "lat" in spec
                                    else where.get(name.casefold(), (None, None)))
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ValueError(f"{path.name}: [{key}] {group}: bad weight or coordinates for {name!r}") from exc
                    if lon is None:
                        skipped.append(name)
                        continue
                    pid = place_ids.setdefault((name, round(lon, 5), round(lat, 5)), len(place_ids))
                    if pid == len(self.key_places):
                        self.key_places.append({"name": name, "lon": lon, "lat": lat})
                    items.append({"group": group, "name": name, "weight": weight, "place": pid})
            place_weights: dict[int, float] = {}  # a place in several groups counts once, with their weights added
            for i in items:
                place_weights[i["place"]] = place_weights.get(i["place"], 0.0) + i["weight"]
            self.key_profiles.append({"key": key, "name": profile.get("name", key),
                                      "description": profile.get("description", ""), "items": items,
                                      "place_weights": list(place_weights.items()), "skipped": skipped})
        if self.key_places:
            x, y = geo.to_xy([p["lon"] for p in self.key_places], [p["lat"] for p in self.key_places])
            self.key_snap = {}
            for mode in ("transit", "car"):
                nodes, metres, sx, sy, moved = self._snap(mode, x, y)
                self.key_snap[mode] = (nodes, metres)
            self.key_xy, self.key_moved = np.column_stack([sx, sy]), moved  # where each stands, off any barrier

    def _snap(self, mode: str, x, y, max_m: float = ORIGIN_MAX_M):
        """Links from points into the network by straight, barrier-aware walks: to their
        FOOTPATH_K nearest footpaths (or roads, by car) and, on foot, their nearest station
        exit and bus stop. See barriers.snap."""
        if mode == "car":
            targets = [(self.pools["car"], FOOTPATH_K, max_m)]
        else:
            targets = [(self.pools["walk"], FOOTPATH_K, max_m), (self.pools["exit"], 1, config.ACCESS_SNAP_M),
                       (self.pools["stop"], 1, config.ACCESS_SNAP_M)]
        return barriers.snap(self.barriers, x, y, targets, fallback=True)

    def _direct_s(self, x: float, y: float, moved: float, tx: np.ndarray, ty: np.ndarray, t_moved, v: float):
        """Seconds to walk straight from (x, y) to each target: near the origin this can beat
        the network. Only walks under DIRECT_MAX_M that cross no barrier (inf for the rest)."""
        metres = (moved + t_moved + np.hypot(tx - x, ty - y)) * config.STRAIGHT_LINE_DETOUR
        near = np.flatnonzero(metres < DIRECT_MAX_M)
        if len(near) and self.barriers is not None:
            near = near[self.barriers.clear(np.full(len(near), x), np.full(len(near), y), tx[near], ty[near])]
        out = np.full(len(tx), np.inf)
        out[near] = metres[near] / v
        return out

    def _key_destinations(self, req: Request, dist: np.ndarray, x: float, y: float, moved: float) -> dict | None:
        """For each profile: the travel time from the origin to each of its places, group
        averages and the weighted average. Places beyond the routing cut-off count as the cut-off."""
        if not self.key_places:
            return None
        v = req.walk_kmh / 3.6
        nodes, metres = self.key_snap[req.mode]
        extra = req.parking_min * 60 if req.mode == "car" else 0.0
        t = (dist[nodes] + metres / v).min(axis=1) + extra
        t = np.minimum(t, self._direct_s(x, y, moved, self.key_xy[:, 0], self.key_xy[:, 1], self.key_moved, v))
        limit = config.MAX_MINUTES * 60
        reached = t <= limit
        minutes = np.where(reached, t, limit) / 60

        def summary(profile: dict) -> dict:
            items = profile["items"]
            place = np.array([i["place"] for i in items], np.int64)
            w, m = np.array([i["weight"] for i in items]), minutes[place]

            def average(sel: np.ndarray) -> float | None:
                return round(float((w[sel] * m[sel]).sum() / w[sel].sum()), 1) if w[sel].sum() > 0 else None

            groups = list(dict.fromkeys(i["group"] for i in items))
            in_group = {g: np.array([i["group"] == g for i in items]) for g in groups}
            return {
                "weighted_min": average(np.ones(len(items), bool)),
                "unreachable": int((~reached[np.unique(place)]).sum()),
                "groups": [{"name": g, "weight": round(float(w[in_group[g]].sum()), 2), "minutes": average(in_group[g])}
                           for g in groups],
                "items": [{"group": i["group"], "name": i["name"], "weight": i["weight"],
                           "lon": self.key_places[i["place"]]["lon"], "lat": self.key_places[i["place"]]["lat"],
                           "minutes": round(float(minutes[i["place"]]), 1) if reached[i["place"]] else None}
                          for i in items],
            }
        return {p["key"]: summary(p) for p in self.key_profiles if p["items"]}

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
        acc = tr["access"]  # params: the entrance-platform pair (see _station_access)
        add(o_ent + acc[:, 0], o_plat + acc[:, 1], K_STATION_IN, np.arange(len(acc)))
        add(o_plat + acc[:, 1], o_ent + acc[:, 0], K_STATION_OUT, np.arange(len(acc)))
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

        pace = config.WALK_KMH_DEFAULT / req.walk_kmh  # station times are given at the default pace
        for k in (K_STATION_IN, K_STATION_OUT):
            a = p[s[k]].astype(np.int64)
            w[s[k]] = self.access_s[a] * pace + self.access_walk_m[a] / v
        if not req.rail:
            w[s[K_STATION_IN]] = OFF
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

    def _graph_and_weights(self, req: Request, forward: bool) -> tuple[_Csr, np.ndarray]:
        """The graph a search runs over (reversed for times *to* a point) and its edge weights."""
        if req.mode == "transit":
            w = self.transit_weights(req)
            return (self.t_graph, w) if forward else (self.t_graph_rev, w[self.t_rev_perm])
        w = self.drive_weights(req)
        return (self.d_graph, w) if forward else (self.d_graph_rev, w[self.d_rev_perm])

    def _search(self, req: Request) -> tuple[np.ndarray, np.ndarray, float, float, dict]:
        """Dijkstra from the request's point, or for direction "to", towards it over the
        reversed graph (each node's time to reach the point). Cached: switching resolution,
        parking allowance or asking for a route reuses the same search. Returns the times,
        predecessors, where the point stands once off any barrier (x, y), and info with
        "moved_m", how far it stepped off one."""
        key = (req.mode, req.direction, round(req.lon, 6), round(req.lat, 6), req.band, req.wait, req.walk_kmh,
               req.bus, req.rail, req.voiddeck)
        x, y = (float(c) for c in geo.to_xy(req.lon, req.lat))
        self._check_on_land(x, y)
        if key in self._cache:
            self._cache.move_to_end(key)
            dist, pred, snap = self._cache[key]
            return dist, pred, snap["x"], snap["y"], snap | {"cached": True}
        t0 = time.perf_counter()
        v = req.walk_kmh / 3.6
        graph, weights = self._graph_and_weights(req, forward=req.direction == "from")
        nodes, metres, sx, sy, moved = self._snap(req.mode, x, y)
        if not np.isfinite(metres[0]).any():
            raise ValueError("That point is too far from any footpath or road in Singapore.")
        t1 = time.perf_counter()
        dist, pred = graph.run(weights, nodes[0], metres[0] / v, config.MAX_MINUTES * 60)
        t2 = time.perf_counter()
        snap = {"x": float(sx[0]), "y": float(sy[0]), "moved_m": float(moved[0]),
                "snap_m": float(metres[0].min() / config.STRAIGHT_LINE_DETOUR)}
        self._cache[key] = (dist, pred, snap)
        while len(self._cache) > CACHE_SIZE:
            self._cache.popitem(last=False)
        return dist, pred, snap["x"], snap["y"], snap | {
            "cached": False, "weights_ms": round((t1 - t0) * 1000), "dijkstra_ms": round((t2 - t1) * 1000)}

    def _cell_times(self, req: Request, dist: np.ndarray, x: float, y: float,
                    moved: float = 0.0) -> tuple[np.ndarray, np.ndarray]:
        """Seconds to reach each land cell of the requested grid (and the cell ids), from a
        search whose point stands at (x, y) having stepped ``moved`` metres off a barrier."""
        g = self.meta["grids"][req.res]
        v = req.walk_kmh / 3.6
        kind = "walk" if req.mode == "transit" else "drive"
        nodes, metres = self.grids[f"{req.res}_{kind}_node"], self.grids[f"{req.res}_{kind}_m"]
        extra = req.parking_min * 60 if req.mode == "car" else 0.0
        cell_s = (dist[nodes] + metres / v).min(axis=1) + extra
        cells = self.grids[f"{req.res}_cell"]
        cx = g["origin_xy"][0] + (cells % g["nx"] + 0.5) * g["res_m"]
        cy = g["origin_xy"][1] - (cells // g["nx"] + 0.5) * g["res_m"]
        return np.minimum(cell_s, self._direct_s(x, y, moved, cx, cy, 0.0, v)), cells

    @_serialised
    def isochrone(self, req: Request) -> dict:
        req.validate()
        t0 = time.perf_counter()
        dist, _, x, y, info = self._search(req)
        t1 = time.perf_counter()
        cell_s, cells = self._cell_times(req, dist, x, y, info["moved_m"])
        g = self.meta["grids"][req.res]
        grid = np.full(g["nx"] * g["ny"], NO_DATA, np.uint16)
        grid[cells] = _tenths(cell_s)
        t2 = time.perf_counter()
        cell_km2 = (g["res_m"] / 1000) ** 2
        minutes = cell_s / 60
        return {
            "grid": self._grid_payload(req.res, grid),
            "origin": {"lon": req.lon, "lat": req.lat, "snap_m": round(info["snap_m"], 1)},
            "direction": req.direction,
            "area_km2": {str(m): round(float((minutes <= m).sum() * cell_km2), 1) for m in (15, 30, 45, 60, 90)},
            "key_destinations": (self._key_destinations(req, dist, x, y, info["moved_m"])
                                 if req.direction == "from" else None),
            "timing_ms": {"search": round((t1 - t0) * 1000), "grid": round((t2 - t1) * 1000),
                          "cached": info["cached"]},
        }

    def _grid_payload(self, res: str, grid: np.ndarray) -> dict:
        g = self.meta["grids"][res]
        return {"nx": g["nx"], "ny": g["ny"], "res_m": g["res_m"], "bounds": g["bounds"],
                "land_cells": self.land_cells[res],
                "encoding": "uint16 little-endian, tenths of a minute; 65535 = no data, 65534 = unreachable",
                "data": base64.b64encode(grid.astype("<u2").tobytes()).decode("ascii")}

    @_serialised
    def key_destinations_at(self, req: Request) -> dict | None:
        """Every profile's key-destination scores for a start point, without the grid."""
        req.validate()
        dist, _, x, y, info = self._search(replace(req, direction="from"))
        return self._key_destinations(req, dist, x, y, info["moved_m"])

    # --- profile maps -----------------------------------------------------------

    def profile_keys(self) -> list[str]:
        return [p["key"] for p in self.key_profiles if p["items"]]

    def profile_isochrone(self, req: Request, key: str) -> dict:
        """Every land cell's weighted average travel time to the places of a key-destination
        profile: the key-destinations score for a start point in that cell.

        As in the several-places view, each place gets one search over the reversed graph,
        sampled onto the grid; places beyond the routing cut-off count as the cut-off, as in
        the score. A place's times are cached per setting, so switching to a profile that
        shares places, or back, is quick. The request's point is not used. This runs outside
        the engine lock (a new profile takes a few seconds) under its own lock, and touches
        nothing the other queries change.
        """
        req.validate()
        if key not in self.profile_keys():
            raise ValueError(f"profile must be one of {self.profile_keys()}")
        profile = next(p for p in self.key_profiles if p["key"] == key)
        setting = (req.mode, req.band, req.wait, req.walk_kmh, req.bus, req.rail, req.voiddeck, req.parking_min,
                   req.res)
        with self._profile_lock:
            if (key, setting) in self._profile_cache:
                self._profile_cache.move_to_end((key, setting))
                return self._profile_cache[(key, setting)] | {"timing_ms": {"search": 0, "grid": 0, "cached": True}}
            t0 = time.perf_counter()
            cells = self.grids[f"{req.res}_cell"]
            cap = config.MAX_MINUTES * 10  # tenths of a minute: an unreachable place counts as the cut-off
            total, reached = np.zeros(len(cells)), np.zeros(len(cells), bool)
            weights, searched = None, 0
            for place, w in profile["place_weights"]:
                times = self._place_cache.get((place, setting))
                if times is None:
                    if weights is None:  # one set of weights serves every place
                        weights = self._graph_and_weights(req, forward=False)
                    times = self._times_to_place(req, place, *weights)
                    self._keep_place_times((place, setting), times)
                    searched += 1
                else:
                    self._place_cache.move_to_end((place, setting))
                ok = times < UNREACHED
                reached |= ok
                total += w * np.where(ok, times, cap)
            average = total / sum(w for _, w in profile["place_weights"])
            g = self.meta["grids"][req.res]
            grid = np.full(g["nx"] * g["ny"], NO_DATA, np.uint16)
            grid[cells] = np.where(reached, np.round(average).clip(0, UNREACHED - 1), UNREACHED).astype(np.uint16)
            result = {"grid": self._grid_payload(req.res, grid), "profile": key,
                      "places": len(profile["place_weights"]), "searched": searched}
            self._profile_cache[(key, setting)] = result
            while len(self._profile_cache) > PROFILE_CACHE_SIZE:
                self._profile_cache.popitem(last=False)
        return result | {"timing_ms": {"search": round((time.perf_counter() - t0) * 1000), "grid": 0, "cached": False}}

    def _times_to_place(self, req: Request, place: int, graph: _Csr, weights: np.ndarray) -> np.ndarray:
        """Tenths of a minute from every land cell of the requested grid to key place ``place``
        (UNREACHED beyond the cut-off): one search over the reversed graph."""
        v = req.walk_kmh / 3.6
        nodes, metres = self.key_snap[req.mode]
        dist = graph.run(weights, nodes[place], metres[place] / v, config.MAX_MINUTES * 60, predecessors=False)
        cell_s, _ = self._cell_times(req, dist, *self.key_xy[place], self.key_moved[place])
        return _tenths(cell_s)

    def _keep_place_times(self, key: tuple, times: np.ndarray) -> None:
        self._place_cache[key] = times
        self._place_cache_bytes += times.nbytes
        while self._place_cache_bytes > PLACE_CACHE_BYTES and len(self._place_cache) > 1:
            _, old = self._place_cache.popitem(last=False)
            self._place_cache_bytes -= old.nbytes

    # --- itineraries ------------------------------------------------------------

    @_serialised
    def route(self, req: Request, to_lon: float, to_lat: float) -> dict:
        """Fastest itinerary from the request origin to a point, as legs with geometry."""
        req.validate()
        dist, pred, x, y, info = self._search(req)
        v = req.walk_kmh / 3.6
        tx, ty = (float(c) for c in geo.to_xy(to_lon, to_lat))
        self._check_on_land(tx, ty)
        nodes, metres, sx, sy, t_moved = self._snap(req.mode, tx, ty)
        cand = dist[nodes[0]] + metres[0] / v
        best = int(np.argmin(cand))
        direct = self._direct_s(x, y, info["moved_m"], sx, sy, t_moved, v)[0]
        extra = req.parking_min * 60 if req.mode == "car" else 0.0
        if not np.isfinite(cand[best]) and not np.isfinite(direct):
            return {"reachable": False}
        if direct <= cand[best] + extra:
            return {"reachable": True, "total_s": round(direct), "legs": [
                {"type": "walk", "seconds": round(direct), "coords": [[req.lon, req.lat], [to_lon, to_lat]]}]}

        path = [int(nodes[0, best])]
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
        tail = metres[0, best] / v
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
