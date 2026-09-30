"""Compare the model's walk from each HDB block to its nearest MRT exit with the
barrier-aware distance of the sibling hdb-resale-analysis project.

Usage: python scripts/validate_walking.py blocks.csv
The CSV needs columns address_key, lat, lon and barrier_m (barrier-aware metres to the
nearest open exit, blank beyond 1.5 km). That project keeps them in parquet files; export
them with its own environment (it has pandas), from its folder:

    .venv\\Scripts\\python.exe -c "import pandas as pd; b = pd.read_parquet('data/mrt/block_mrt_barrier_distance.parquet'); b = b[b.snapshot == b.snapshot.max()]; pd.read_csv('data/geocode/blocks.csv')[['address_key', 'lat', 'lon']].merge(b[['address_key', 'barrier_m']]).to_csv('blocks.csv', index=False)"

Only walking is switched on, at 3.6 km/h so that seconds equal metres, including the
model's 1.2 allowance on straight walks and its stairs and void-deck factors.
"""
import csv
import sys

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from isochrone import geo
from isochrone.engine import K_ENT_LINK, K_STOP_LINK, K_WALK, OFF, ORIGIN_K, Engine, Request


def main(path: str) -> None:
    eng = Engine()
    w = eng.transit_weights(Request(lon=103.85, lat=1.29, walk_kmh=3.6))
    w = np.where(np.isin(eng.t_kind, [K_WALK, K_STOP_LINK, K_ENT_LINK]), w, OFF)
    g = eng.t_graph
    graph = csr_matrix((np.concatenate([w, np.full(ORIGIN_K, OFF)]), g.indices, g.indptr), shape=(g.n, g.n))
    exits = eng.offsets["entrance"] + np.arange(len(eng.entrance_x))
    dist = dijkstra(graph, directed=True, indices=exits, min_only=True, limit=5000)  # walking is symmetric

    rows = [r for r in csv.DictReader(open(path, encoding="utf-8")) if r.get("barrier_m")]
    x, y = geo.to_xy([float(r["lon"]) for r in rows], [float(r["lat"]) for r in rows])
    nodes, metres, *_ = eng._snap("transit", x, y)
    ours = (dist[nodes] + metres).min(axis=1)
    barrier = np.array([float(r["barrier_m"]) for r in rows])
    ok = np.isfinite(ours)
    ratio = ours[ok] / barrier[ok]
    print(f"{ok.sum()} blocks within 1.5 km of an exit: model median {np.median(ours[ok]):.0f} m, "
          f"barrier-aware median {np.median(barrier[ok]):.0f} m")
    print("model / barrier-aware: median {:.2f}, 90th percentile {:.2f}, 99th {:.2f}".format(
        *np.percentile(ratio, [50, 90, 99])))
    print(f"over 1.5x: {np.mean(ratio > 1.5):.1%}, over 2x: {np.mean(ratio > 2):.1%}")


if __name__ == "__main__":
    main(sys.argv[1])
