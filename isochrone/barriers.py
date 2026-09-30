"""Barrier-aware walking off the footpath network.

Walks follow OpenStreetMap's footpaths, and straight walks join places to them: a map
cell or clicked point to its nearest footpaths, a bus stop or station exit to the path
beside it, and short walks straight across open ground near the start. A straight line
ignores what's in the way, so these walks are barrier-aware, like the distance the
hdb-resale-analysis project derived (its pipeline steps 06b and 07b). Open ground is
walkable in any direction, but not across

    expressways, major roads (trunk and primary), rivers and canals, reservoirs and other
    water, at-grade railway, and fenced grounds (schools, prisons, military land,
    airfields and golf courses),

except where OpenStreetMap maps a crossing: a pedestrian crossing, junction, overhead
bridge, underpass or road bridge. That project draws them on a 3 m grid over Singapore
(SVY21, 1 = barrier); data/raw/barriers holds a copy of its main variant.

A straight walk may start or end on a barrier, as a point on a road, a bridge or in school
grounds does, but may not pass over one between open ground at both ends. A point that
is itself on a barrier first steps off it to the nearest open ground within
BARRIER_MOVE_MAX_M: a kerbside bus stop joins the footpath on its own side of the road.
"""
from __future__ import annotations

import numpy as np

from . import config, geo

CHUNK = 20_000  # walks checked at a time


class Barriers:
    def __init__(self, path=None) -> None:
        r = np.load(path or config.BARRIERS)
        self.bits = r["main"]  # packed bits, row-major; row 0 is the southern edge
        self.nx, self.ny = int(r["nx"]), int(r["ny"])
        self.x0, self.y0, self.res = float(r["x0"]), float(r["y0"]), float(r["res"])
        # where to look for open ground around a point on a barrier, nearest first
        k = int(config.BARRIER_MOVE_MAX_M // self.res)
        dy, dx = (a.ravel() for a in np.mgrid[-k:k + 1, -k:k + 1])
        dist = np.hypot(dy, dx) * self.res
        order = np.argsort(dist, kind="stable")
        order = order[dist[order] <= config.BARRIER_MOVE_MAX_M]
        self._offsets = np.column_stack([dy[order], dx[order]])

    @classmethod
    def load(cls) -> Barriers | None:
        """The barrier grid, or None if data/raw/barriers has none (walks are then plain straight lines)."""
        return cls() if config.BARRIERS.exists() else None

    def _svy21(self, x, y):
        return geo.to_svy21(*geo.to_lonlat(x, y))

    def _cell(self, sx, sy):
        return (np.floor((sy - self.y0) / self.res).astype(np.int64),
                np.floor((sx - self.x0) / self.res).astype(np.int64))

    def _on_barrier(self, r, c) -> np.ndarray:
        inside = (r >= 0) & (r < self.ny) & (c >= 0) & (c < self.nx)  # beyond the grid counts as open
        flat = np.where(inside, r * self.nx + c, 0)
        return inside & (((self.bits[flat >> 3] >> (7 - (flat & 7))) & 1) == 1)

    def on_barrier(self, x, y) -> np.ndarray:
        """Whether points (local metres) lie on a barrier."""
        return self._on_barrier(*self._cell(*self._svy21(x, y)))

    def step_off(self, x, y) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """Points moved off a barrier to the centre of the nearest open cell within
        BARRIER_MOVE_MAX_M, and the metres moved. Points not on one, or with no open ground
        that near (inside a reservoir, say), stay where they are."""
        x, y = np.array(x, dtype=np.float64, ndmin=1), np.array(y, dtype=np.float64, ndmin=1)
        sx, sy = self._svy21(x, y)
        r, c = self._cell(sx, sy)
        moved = np.zeros(len(x))
        todo = np.nonzero(self._on_barrier(r, c))[0]
        for dy, dx in self._offsets:
            if not len(todo):
                break
            free = ~self._on_barrier(r[todo] + dy, c[todo] + dx)
            hit = todo[free]
            # SVY21 and the local projection agree to 0.03% in scale and 0.03° in bearing
            ex = self.x0 + (c[hit] + dx + 0.5) * self.res - sx[hit]
            ey = self.y0 + (r[hit] + dy + 0.5) * self.res - sy[hit]
            x[hit] += ex
            y[hit] += ey
            moved[hit] = np.hypot(ex, ey)
            todo = todo[~free]
        return x, y, moved

    def clear(self, ax, ay, bx, by) -> np.ndarray:
        """Whether each straight walk from (ax, ay) to (bx, by), in local metres, crosses no
        barrier. It is sampled every half cell; barrier samples may run from either end
        (a walk starting or ending on a road or a bridge) but not lie between open ones."""
        ax, ay, bx, by = (np.array(v, dtype=np.float64, ndmin=1) for v in (ax, ay, bx, by))
        (sax, say), (sbx, sby) = self._svy21(ax, ay), self._svy21(bx, by)
        n = np.maximum(2, np.ceil(np.hypot(sbx - sax, sby - say) / (self.res / 2)).astype(np.int64) + 1)
        out = np.ones(len(ax), bool)
        for s in range(0, len(ax), CHUNK):
            k = n[s:s + CHUNK]
            walk = np.repeat(np.arange(len(k)), k)
            first = np.cumsum(k) - k
            i = np.arange(len(walk))
            t = (i - first[walk]) / (k[walk] - 1)
            px = sax[s:s + CHUNK][walk] + (sbx - sax)[s:s + CHUNK][walk] * t
            py = say[s:s + CHUNK][walk] + (sby - say)[s:s + CHUNK][walk] * t
            blocked = self._on_barrier(*self._cell(px, py))
            open_first = np.minimum.reduceat(np.where(blocked, len(walk), i), first)
            open_last = np.maximum.reduceat(np.where(blocked, -1, i), first)
            any_open = open_last >= 0
            count = np.cumsum(blocked)  # barrier samples between the first and last open ones
            between = count[np.where(any_open, open_last, 0)] - count[np.where(any_open, open_first, 0)]
            out[s:s + CHUNK] = ~any_open | (between == 0)
        return out


class Pool:
    """Network nodes that straight walks can join: graph ids, coordinates and a search tree."""

    def __init__(self, ids, x, y) -> None:
        from scipy.spatial import cKDTree
        self.ids = np.asarray(ids)
        self.xy = np.column_stack([x, y]).astype(np.float64)
        self.tree = cKDTree(self.xy)


def snap(barriers: Barriers | None, x, y, targets, fallback: bool = False, step: bool = True):
    """Links from points to network nodes by straight walks that cross no barrier.

    ``targets`` lists (pool, k, max_m): from each pool, a point's k nearest nodes within
    max_m that it can walk to, nearest first (4k candidates are tried). A point on a
    barrier first steps off it. Returns node ids and walking metres, one column per link
    (inf where a point has fewer), where each point stands once off any barrier (x, y),
    and how far it stepped. Metres are the step plus the straight line, times
    STRAIGHT_LINE_DETOUR. With ``fallback``, a point that no barrier-free walk joins to
    anything (hemmed in between carriageways, say) gets plain straight links instead.
    Without ``step``, points stay put: map cells, 50-250 m across, just start their walks
    wherever their centre falls (on a river, a road, or a bridge over them).
    """
    x, y = np.array(x, dtype=np.float64, ndmin=1), np.array(y, dtype=np.float64, ndmin=1)
    if barriers is not None and fallback:
        nodes, metres, sx, sy, moved = snap(barriers, x, y, targets, step=step)
        stuck = ~np.isfinite(metres).any(axis=1)
        if stuck.any():
            nodes[stuck], metres[stuck], *_ = snap(None, x[stuck], y[stuck], targets)
            sx[stuck], sy[stuck], moved[stuck] = x[stuck], y[stuck], 0.0
        return nodes, metres, sx, sy, moved
    moved = np.zeros(len(x))
    if barriers is not None and step:
        x, y, moved = barriers.step_off(x, y)
    nodes, metres = [], []
    for pool, k, max_m in targets:
        d, i = pool.tree.query(np.column_stack([x, y]), k=4 * k, distance_upper_bound=max_m)
        d, i = d.reshape(len(x), -1), i.reshape(len(x), -1)
        usable = np.isfinite(d)
        i = np.where(usable, i, 0)
        if barriers is not None:
            rows, cols = np.nonzero(usable)
            usable[rows, cols] = barriers.clear(x[rows], y[rows], pool.xy[i[rows, cols], 0], pool.xy[i[rows, cols], 1])
        order = np.argsort(~usable, axis=1, kind="stable")[:, :k]  # usable ones first, still nearest first
        d, i, usable = (np.take_along_axis(a, order, axis=1) for a in (d, i, usable))
        nodes.append(pool.ids[i])
        metres.append(np.where(usable, (moved[:, None] + d) * config.STRAIGHT_LINE_DETOUR, np.inf))
    return np.hstack(nodes), np.hstack(metres), x, y, moved
