"""Compare modelled station-to-station MRT times with an external matrix.

Usage: python scripts/validate_mrt.py path/to/travel_times.csv
The CSV needs columns from_station_name, to_station_name, trip_duration_in_minutes
(e.g. the mrt.sg matrix from the sibling "the-fastest-journey" project).

Times are platform-to-platform within the rail network (no leaving a station to
walk elsewhere), with interchange walks at the leisurely pace they were measured
at. Reported for both "best" (no waiting) and "avg" (expected waits at every
boarding, including the first) assumptions.
"""
import csv
import json
import re
import sys
from collections import defaultdict

import numpy as np
from scipy.sparse import csr_matrix
from scipy.sparse.csgraph import dijkstra

from isochrone import config
from isochrone.engine import K_ENT_LINK, K_STATION_IN, K_STATION_OUT, K_WALK, OFF, ORIGIN_K, Engine, Request


def norm(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def main(path: str) -> None:
    eng = Engine()
    meta = json.loads((config.BUILD / "meta.json").read_text(encoding="utf-8"))
    stations = json.loads((config.BUILD / "overlays" / "mrt_stations.geojson").read_text(encoding="utf-8"))
    code_to_name = {}
    for f in stations["features"]:
        for code in f["properties"]["codes"].split(" / "):
            code_to_name[code] = norm(f["properties"]["name"])
    platforms = defaultdict(list)
    for i, code in enumerate(meta["names"]["platform_code"]):
        platforms[code_to_name[code]].append(eng.offsets["platform"] + i)

    ref = defaultdict(dict)
    with open(path, encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if r["from_station_name"] != r["to_station_name"]:
                ref[norm(r["from_station_name"])][norm(r["to_station_name"])] = float(r["trip_duration_in_minutes"])

    g = eng.t_graph
    street = np.isin(eng.t_kind, [K_WALK, K_ENT_LINK, K_STATION_IN, K_STATION_OUT])
    for wait in ("best", "avg"):
        req = Request(lon=103.85, lat=1.29, wait=wait, walk_kmh=config.LEISURELY_WALK_KMH, bus=False)
        w = np.where(street, OFF, eng.transit_weights(req))
        graph = csr_matrix((np.concatenate([w, np.full(ORIGIN_K, OFF)]), g.indices, g.indptr), shape=(g.n, g.n))
        rows = []
        for a, dests in ref.items():
            if a not in platforms:
                continue
            dist = dijkstra(graph, directed=True, indices=platforms[a], min_only=True)
            for b, minutes in dests.items():
                if b in platforms:
                    rows.append((a, b, minutes, dist[platforms[b]].min() / 60))
        ref_m = np.array([r[2] for r in rows])
        ours = np.array([r[3] for r in rows])
        diff = ours - ref_m
        print(f"\n[{wait} waits] {len(rows)} station pairs compared")
        print(f"model - reference: mean {diff.mean():+.1f} min, median {np.median(diff):+.1f}, "
              f"MAE {np.abs(diff).mean():.1f}, within 3 min {np.mean(np.abs(diff) <= 3):.0%}, "
              f"within 5 min {np.mean(np.abs(diff) <= 5):.0%}, ratio median {np.median(ours / ref_m):.2f}")
        worst = np.argsort(-np.abs(diff))[:12]
        print("largest differences (model vs reference, minutes):")
        for i in worst:
            print(f"  {rows[i][0]:>18} -> {rows[i][1]:<18} {ours[i]:5.1f} vs {ref_m[i]:5.1f}")


if __name__ == "__main__":
    main(sys.argv[1])
