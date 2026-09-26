"""Parse the OpenStreetMap extract into walking and driving networks.

Ways are split at junctions and additionally every ``MAX_SEGMENT_M`` metres, so the
graph keeps enough intermediate nodes for the heatmap grid to sample from.
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import osmium
from scipy.sparse import coo_matrix
from scipy.sparse.csgraph import connected_components

from . import geo

MAX_SEGMENT_M = 60.0

# Road classes for driving. The index is stored on each edge and used to look up
# the typical-congestion speed for the chosen time band.
DRIVE_CLASSES = ("motorway", "motorway_link", "trunk", "trunk_link", "primary", "primary_link",
                 "secondary", "secondary_link", "tertiary", "tertiary_link", "unclassified",
                 "residential", "living_street", "service", "road")
DRIVE_CLASS_INDEX = {c: i for i, c in enumerate(DRIVE_CLASSES)}

WALK_HIGHWAYS = {"footway", "path", "pedestrian", "steps", "corridor", "living_street", "residential",
                 "service", "unclassified", "tertiary", "tertiary_link", "secondary", "secondary_link",
                 "primary", "primary_link", "trunk", "trunk_link", "track", "cycleway", "bridleway",
                 "elevator", "road", "crossing"}

# Walk edge kinds (stored per edge; the router applies a cost factor per kind).
WALK_NORMAL, WALK_STEPS, WALK_VOIDDECK = 0, 1, 2

RAIL_LINES = ("NSL", "EWL", "NEL", "CCL", "DTL", "TEL", "BPLRT", "SKLRT", "PGLRT")

_ALLOWED = {"yes", "designated", "permissive", "destination"}


def walk_kind(tags) -> int | None:
    hw = tags.get("highway")
    if hw not in WALK_HIGHWAYS:
        return None
    foot = tags.get("foot")
    if foot in ("no", "private", "use_sidepath"):
        return None
    if tags.get("access") in ("no", "private") and foot not in _ALLOWED:
        return None
    if hw in ("trunk", "trunk_link") and foot not in _ALLOWED and tags.get("sidewalk") in ("no", "none", "separate"):
        return None
    if tags.get("area") == "yes" and hw not in ("pedestrian", "footway"):
        return None
    return WALK_STEPS if hw == "steps" else WALK_NORMAL


def _maxspeed(value: str | None) -> float:
    if not value:
        return 0.0
    digits = "".join(ch for ch in value.split(";")[0] if ch.isdigit() or ch == ".")
    try:
        speed = float(digits)
    except ValueError:
        return 0.0
    return speed * 1.609 if "mph" in value else speed


def drive_attrs(tags) -> tuple[int, int, float] | None:
    """(road class index, oneway flag, maxspeed km/h) or None if cars may not use the way."""
    hw = tags.get("highway")
    cls = DRIVE_CLASS_INDEX.get(hw)
    if cls is None or tags.get("area") == "yes":
        return None
    motor = tags.get("motor_vehicle") or tags.get("motorcar") or tags.get("vehicle")
    if motor in ("no", "private"):
        return None
    if tags.get("access") in ("no", "private") and motor not in _ALLOWED:
        return None
    if hw == "service" and tags.get("service") == "emergency_access":
        return None
    ow = tags.get("oneway")
    if ow in ("yes", "1", "true"):
        oneway = 1
    elif ow in ("-1", "reverse"):
        oneway = -1
    elif ow == "no":
        oneway = 0
    elif tags.get("junction") in ("roundabout", "circular") or hw in ("motorway", "motorway_link"):
        oneway = 1
    else:
        oneway = 0
    return cls, oneway, _maxspeed(tags.get("maxspeed"))


class _RailRelations(osmium.SimpleHandler):
    def __init__(self):
        super().__init__()
        self.way_line: dict[int, str] = {}

    def relation(self, r):
        t = r.tags
        if t.get("route") in ("subway", "light_rail", "monorail") and t.get("ref") in RAIL_LINES:
            for m in r.members:
                if m.type == "w":
                    self.way_line.setdefault(m.ref, t.get("ref"))


class _Ways(osmium.SimpleHandler):
    def __init__(self, rail_ways: dict[int, str]):
        super().__init__()
        self.rail_ways = rail_ways
        self.walk: list[tuple] = []
        self.drive: list[tuple] = []
        self.rail: list[tuple[str, list[tuple[float, float]]]] = []

    def way(self, w):
        tags = w.tags
        line = self.rail_ways.get(w.id)
        if line is None and "highway" not in tags:
            return
        try:
            ids = [n.ref for n in w.nodes]
            lons = [n.lon for n in w.nodes]
            lats = [n.lat for n in w.nodes]
        except osmium.InvalidLocationError:
            return  # way crosses the edge of the extract
        if len(ids) < 2:
            return
        if line is not None:
            self.rail.append((line, list(zip(lons, lats))))
        wk = walk_kind(tags)
        if wk is not None:
            self.walk.append((ids, lons, lats, wk, 0, 0.0))
        da = drive_attrs(tags)
        if da is not None:
            self.drive.append((ids, lons, lats, da[0], da[1], da[2]))


@dataclass
class Graph:
    osm_id: np.ndarray    # int64, per node
    x: np.ndarray         # float64 metres, per node
    y: np.ndarray
    u: np.ndarray         # int32, per directed edge
    v: np.ndarray
    length: np.ndarray    # float32 metres
    attr: np.ndarray      # uint8: walk kind or drive class
    maxspeed: np.ndarray  # float32 km/h (0 = untagged)

    @property
    def n(self) -> int:
        return len(self.x)

    def subset(self, keep: np.ndarray) -> "Graph":
        """Keep only the masked nodes (and edges between them), re-indexing."""
        new_index = np.full(self.n, -1, dtype=np.int64)
        new_index[keep] = np.arange(int(keep.sum()))
        ok = keep[self.u] & keep[self.v]
        return Graph(self.osm_id[keep], self.x[keep], self.y[keep],
                     new_index[self.u[ok]].astype(np.int32), new_index[self.v[ok]].astype(np.int32),
                     self.length[ok], self.attr[ok], self.maxspeed[ok])

    def add_edges(self, u, v, length, attr) -> None:
        self.u = np.concatenate([self.u, np.asarray(u, np.int32)])
        self.v = np.concatenate([self.v, np.asarray(v, np.int32)])
        self.length = np.concatenate([self.length, np.asarray(length, np.float32)])
        self.attr = np.concatenate([self.attr, np.full(len(u), attr, np.uint8)])
        self.maxspeed = np.concatenate([self.maxspeed, np.zeros(len(u), np.float32)])

    def components(self, connection: str = "weak") -> np.ndarray:
        """Size of the connected component each node belongs to."""
        m = coo_matrix((np.ones(len(self.u), np.int8), (self.u, self.v)), shape=(self.n, self.n))
        _, labels = connected_components(m, directed=True, connection=connection)
        return np.bincount(labels)[labels]


def build_graph(ways: list[tuple], directed: bool) -> Graph:
    """Turn OSM ways into a graph with nodes at junctions and every MAX_SEGMENT_M metres."""
    lens = np.fromiter((len(w[0]) for w in ways), dtype=np.int64, count=len(ways))
    ids = np.concatenate([np.asarray(w[0], np.int64) for w in ways])
    x, y = geo.to_xy(np.concatenate([np.asarray(w[1]) for w in ways]),
                     np.concatenate([np.asarray(w[2]) for w in ways]))
    way_idx = np.repeat(np.arange(len(ways)), lens)
    starts = np.cumsum(lens) - lens
    ends = starts + lens - 1

    seg = np.zeros(len(ids))
    seg[1:] = np.hypot(np.diff(x), np.diff(y))
    seg[starts] = 0.0
    total = np.cumsum(seg)
    cum = total - total[starts][way_idx]  # distance along each way

    _, inverse, counts = np.unique(ids, return_inverse=True, return_counts=True)
    bucket = np.floor(cum / MAX_SEGMENT_M)
    new_bucket = np.ones(len(ids), bool)
    new_bucket[1:] = bucket[1:] != bucket[:-1]
    keep = (counts[inverse] >= 2) | new_bucket
    keep[starts] = True
    keep[ends] = True

    k = np.nonzero(keep)[0]
    same_way = way_idx[k[1:]] == way_idx[k[:-1]]
    a, b = k[:-1][same_way], k[1:][same_way]
    elen = (cum[b] - cum[a]).astype(np.float32)

    node_ids, first = np.unique(ids[k], return_index=True)
    ui = np.searchsorted(node_ids, ids[a]).astype(np.int32)
    vi = np.searchsorted(node_ids, ids[b]).astype(np.int32)
    wi = way_idx[a]
    attr = np.array([w[3] for w in ways], np.uint8)[wi]
    oneway = np.array([w[4] for w in ways], np.int8)[wi]
    maxspeed = np.array([w[5] for w in ways], np.float32)[wi]

    ok = ui != vi
    ui, vi, elen, attr, oneway, maxspeed = ui[ok], vi[ok], elen[ok], attr[ok], oneway[ok], maxspeed[ok]
    if directed:
        fwd, bwd = oneway >= 0, oneway <= 0
    else:
        fwd = bwd = np.ones(len(ui), bool)
    u = np.concatenate([ui[fwd], vi[bwd]])
    v = np.concatenate([vi[fwd], ui[bwd]])
    return Graph(node_ids, x[k][first], y[k][first], u, v,
                 np.concatenate([elen[fwd], elen[bwd]]), np.concatenate([attr[fwd], attr[bwd]]),
                 np.concatenate([maxspeed[fwd], maxspeed[bwd]]))


def parse(pbf_path) -> tuple[Graph, Graph, list[tuple[str, list[tuple[float, float]]]]]:
    """Return (walk graph, drive graph, rail line geometries) from an OSM PBF file."""
    rel = _RailRelations()
    rel.apply_file(str(pbf_path))
    handler = _Ways(rel.way_line)
    handler.apply_file(str(pbf_path), locations=True)
    return build_graph(handler.walk, directed=False), build_graph(handler.drive, directed=True), handler.rail
