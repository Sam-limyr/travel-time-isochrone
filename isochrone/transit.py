"""Bus and train service models built from LTA DataMall data.

Buses: one "pattern" per service direction. Boarding waits come from LTA's headway
ranges for each time band; running times from LTA's scheduled arrival times.

Trains: modelled per track segment ("hop" = consecutive platform pair) from the
official GTFS timetable, so segments shared by several service patterns (e.g. the
Circle Line) get their combined frequency. Riding on through a station is only
allowed along platform sequences that real trips run.
"""
from __future__ import annotations

import csv
import io
import re
import zipfile
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date

import numpy as np

from . import config, geo

BANDS = config.BAND_KEYS
NB = len(BANDS)
NW = len(config.WAIT_MODES)


# --- Buses --------------------------------------------------------------------

def parse_headway(value: str) -> tuple[float, float] | None:
    """LTA frequency strings: '08-12' -> (8, 12); '10' -> (10, 10); '-' -> None."""
    nums = [int(n) for n in re.findall(r"\d+", value or "")]
    if not nums or max(nums) == 0:
        return None
    lo, hi = (nums[0], nums[-1]) if len(nums) > 1 else (nums[0], nums[0])
    lo = lo or hi
    return float(min(lo, hi)), float(max(lo, hi))


def bus_waits(headway: tuple[float, float] | None) -> list[float]:
    """Wait in seconds for (best, avg, worst); NaN when the service does not run."""
    if headway is None:
        return [np.nan] * NW
    lo, hi = headway
    return [0.0, (lo + hi) / 2 / 2 * 60, hi * 60]


def _hhmm(value: str) -> int | None:
    value = (value or "").strip()
    if len(value) != 4 or not value.isdigit():
        return None
    return int(value[:2]) * 60 + int(value[2:])


def _schedule_minutes(values: list[str]) -> np.ndarray | None:
    """Scheduled arrival minutes along a route, unwrapped past midnight and made monotonic."""
    t = [_hhmm(v) for v in values]
    if any(x is None for x in t):
        return None
    out = np.array(t, dtype=np.float64)
    for i in range(1, len(out)):
        while out[i] < out[i - 1] - 600:
            out[i] += 1440
        out[i] = max(out[i], out[i - 1])
    return out


def model_hop_seconds(hop_km: np.ndarray) -> np.ndarray:
    """Physical prior: dwell plus cruising, faster on long (expressway) hops."""
    lo, hi = config.BUS_CRUISE_KMH
    cruise = lo + (hi - lo) * np.clip((hop_km - 0.4) / 2.6, 0, 1)
    return config.BUS_DWELL_S + hop_km / cruise * 3600


def hop_times(dist_km: np.ndarray, sched: list[np.ndarray | None], straight_km: np.ndarray) -> np.ndarray:
    """Seconds between consecutive stops.

    LTA's scheduled first/last-bus times are rounded to the minute and sometimes
    belong to different trips at different stops (e.g. a last bus that starts
    mid-route), so they are used only to scale a physical prior: over a window of
    ~7 hops, factor = scheduled time / prior time. Implausible factors are
    discarded and replaced by the route's median factor.
    """
    hop_km = np.diff(dist_km)
    bad = (hop_km <= 0) | (hop_km < straight_km * 0.9) | (hop_km > straight_km * 3 + 1.0)
    hop_km = np.where(bad, straight_km * 1.3, hop_km)
    prior = model_hop_seconds(hop_km)
    n = len(hop_km)
    lo, hi = config.BUS_SCHEDULE_FACTOR_RANGE
    factors = np.full(n, np.nan)
    for i in range(n):
        j0, j1 = max(0, i - 3), min(n, i + 4)
        expected = prior[j0:j1].sum()
        found = [(t[j1] - t[j0]) * 60 / expected for t in sched if t is not None]
        found = [f for f in found if lo <= f <= hi]
        if found:
            factors[i] = float(np.mean(found))
    valid = ~np.isnan(factors)
    factors[~valid] = np.median(factors[valid]) if valid.any() else 1.0
    return prior * factors


@dataclass
class BusModel:
    stop_code: list[str]
    stop_x: np.ndarray
    stop_y: np.ndarray
    stop_name: list[str]
    pattern_name: list[str]                 # e.g. "10 (dir 1)"
    pattern_wait: np.ndarray                # (P, bands, wait modes) seconds, NaN = not running
    ride_pattern: np.ndarray                # (R,) pattern of each on-bus node
    ride_stop: np.ndarray                   # (R,) stop index of each on-bus node
    ride_hop_s: np.ndarray                  # (R,) base seconds to the next stop, NaN at the last stop
    stats: dict = field(default_factory=dict)


def build_bus(keep_stop) -> BusModel:
    """``keep_stop(x, y) -> bool mask``: which stops are usable (on land, near a footpath)."""
    lta = config.RAW / "lta"
    with open(lta / "bus_stops.csv", encoding="utf-8") as fh:
        stops = list(csv.DictReader(fh))
    x, y = geo.to_xy([float(s["Longitude"]) for s in stops], [float(s["Latitude"]) for s in stops])
    usable = keep_stop(x, y)
    stops = [s for s, ok in zip(stops, usable) if ok]
    x, y = x[usable], y[usable]
    stop_index = {s["BusStopCode"]: i for i, s in enumerate(stops)}

    with open(lta / "bus_services.csv", encoding="utf-8") as fh:
        services = {(r["ServiceNo"], r["Direction"]): r for r in csv.DictReader(fh)}
    routes: dict[tuple[str, str], list[dict]] = defaultdict(list)
    with open(lta / "bus_routes.csv", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            routes[(r["ServiceNo"], r["Direction"])].append(r)

    names, waits, ride_pat, ride_stop, ride_hop = [], [], [], [], []
    dropped_stops = no_headway = 0
    for key, rows in sorted(routes.items()):
        rows.sort(key=lambda r: int(r["StopSequence"]))
        svc = services.get(key)
        if svc is None:
            no_headway += 1
            continue
        heads = [parse_headway(svc[config.BANDS[b]["lta_freq"]]) for b in BANDS]
        if all(h is None for h in heads):
            no_headway += 1
            continue
        present = [r for r in rows if r["BusStopCode"] in stop_index]
        dropped_stops += len(rows) - len(present)
        if len(present) < 2:
            continue
        idx = np.array([stop_index[r["BusStopCode"]] for r in present])
        dist = np.array([float(r["Distance"] or 0) for r in present])
        straight = np.hypot(np.diff(x[idx]), np.diff(y[idx])) / 1000
        sched = [_schedule_minutes([r["WD_LastBus"] for r in present]),
                 _schedule_minutes([r["WD_FirstBus"] for r in present])]
        hops = hop_times(dist, sched, straight)

        p = len(names)
        names.append(f"{key[0]} (dir {key[1]})")
        waits.append([bus_waits(h) for h in heads])
        ride_pat.extend([p] * len(idx))
        ride_stop.extend(idx.tolist())
        ride_hop.extend(hops.tolist() + [np.nan])

    return BusModel(
        stop_code=[s["BusStopCode"] for s in stops], stop_x=x, stop_y=y,
        stop_name=[s["Description"] for s in stops],
        pattern_name=names, pattern_wait=np.array(waits, np.float32),
        ride_pattern=np.array(ride_pat, np.int32), ride_stop=np.array(ride_stop, np.int32),
        ride_hop_s=np.array(ride_hop, np.float32),
        stats={"stops": len(stops), "patterns": len(names), "ride_nodes": len(ride_pat),
               "route_stops_dropped": dropped_stops, "patterns_without_headways": no_headway},
    )


# --- Trains -------------------------------------------------------------------

def _gtfs_seconds(value: str) -> int:
    h, m, s = value.split(":")
    return int(h) * 3600 + int(m) * 60 + int(s)


def line_of(stop_code: str) -> str:
    """Line name as used in data/manual/mrt_transfer_times.csv."""
    for prefix, line in (("STC", "SKLRT"), ("SE", "SKLRT"), ("SW", "SKLRT"), ("PTC", "PGLRT"),
                         ("PE", "PGLRT"), ("PW", "PGLRT"), ("BP", "BPLRT"), ("NS", "NSL"), ("EW", "EWL"),
                         ("CG", "EWL"), ("NE", "NEL"), ("CC", "CCL"), ("CE", "CCL"), ("DT", "DTL"),
                         ("TE", "TEL")):
        if stop_code.startswith(prefix):
            return line
    return stop_code


def _norm(name: str) -> str:
    return re.sub(r"[^a-z]", "", name.lower())


def gap_stats(deps: np.ndarray, band_seconds: float) -> tuple[float, float, float]:
    """(best, expected, worst) wait in seconds at a stop served by departures ``deps``.

    For a random arrival the wait falls linearly from the full gap to zero, so the
    expected wait is sum(gap^2) / (2 * sum(gap)); irregular (bunched) service waits
    longer than half the mean headway. A lone departure counts as one per band.
    """
    d = np.unique(deps)
    if len(d) < 2:
        return 0.0, band_seconds / 2, band_seconds
    gaps = np.diff(d)
    return 0.0, float((gaps ** 2).sum() / (2 * gaps.sum())), float(gaps.max())


def combined_waits(pattern_deps: list[np.ndarray], band_seconds: float) -> tuple[float, float, float]:
    """Waits at a segment served by several service patterns in one band.

    Each pattern contributes its trip count over the band (its frequency) plus its
    irregularity: the ratios of its expected and worst waits to those of an evenly
    spaced service. Frequencies add up; irregularity is trip-weighted. Adding
    frequencies, rather than merging raw departure times, avoids false gaps where
    the feed hands a segment over from one pattern to another mid-band.
    """
    n_total = kappa = ratio = 0.0
    for deps in pattern_deps:
        n = len(np.unique(deps))
        _, expected, worst = gap_stats(deps, band_seconds)
        mean_gap = band_seconds if n < 2 else (deps.max() - deps.min()) / (n - 1)
        kappa += n * expected / (mean_gap / 2)
        ratio += n * worst / mean_gap
        n_total += n
    headway = band_seconds / n_total
    return 0.0, kappa / n_total * headway / 2, ratio / n_total * headway


@dataclass
class RailModel:
    platform_id: list[str]
    platform_code: list[str]
    platform_line: list[str]
    platform_station: list[str]        # parent station id
    platform_x: np.ndarray
    platform_y: np.ndarray
    entrance_id: list[str]
    entrance_x: np.ndarray
    entrance_y: np.ndarray
    access: np.ndarray                 # (A, 2) int: entrance idx, platform idx
    access_m: np.ndarray               # (A,) walking metres inside the station
    transfer: np.ndarray               # (T, 2) int: from platform, to platform
    transfer_s: np.ndarray             # (T,) leisurely seconds
    transfer_source: list[str]         # "csv" or "default"
    hop: np.ndarray                    # (H, 2) int: from platform, to platform
    hop_wait: np.ndarray               # (H, bands, wait modes) seconds, NaN = not running
    hop_run: np.ndarray                # (H, bands) seconds departure->arrival
    hop_dwell: np.ndarray              # (H, bands) seconds dwell at the destination platform
    through: np.ndarray                # (X, 2) int: hop -> next hop
    through_bands: np.ndarray          # (X,) uint8 bitmask of bands in which trips do this
    stations: list[dict]               # for the map overlay
    stats: dict = field(default_factory=dict)


def _active_services(z: zipfile.ZipFile, day: date) -> set[str]:
    def read(name):
        return list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig")))
    ymd = day.strftime("%Y%m%d")
    weekday = day.strftime("%A").lower()
    active = {c["service_id"] for c in read("calendar.txt")
              if c[weekday] == "1" and c["start_date"] <= ymd <= c["end_date"]}
    if "calendar_dates.txt" in z.namelist():
        for c in read("calendar_dates.txt"):
            if c["date"] == ymd:
                (active.add if c["exception_type"] == "1" else active.discard)(c["service_id"])
    return active


def build_rail(day: date) -> RailModel:
    z = zipfile.ZipFile(config.RAW / "lta" / "train_gtfs.zip")

    def read(name):
        return list(csv.DictReader(io.TextIOWrapper(z.open(name), encoding="utf-8-sig")))

    stops = {s["stop_id"]: s for s in read("stops.txt")}
    active = _active_services(z, day)
    trips = {t["trip_id"] for t in read("trips.txt") if t["service_id"] in active}
    seqs: dict[str, list[tuple[int, str, int, int]]] = defaultdict(list)
    for r in csv.DictReader(io.TextIOWrapper(z.open("stop_times.txt"), encoding="utf-8-sig")):
        if r["trip_id"] in trips:
            seqs[r["trip_id"]].append((int(r["stop_sequence"]), r["stop_id"],
                                       _gtfs_seconds(r["arrival_time"]), _gtfs_seconds(r["departure_time"])))

    # platforms actually served
    used = sorted({sid for rows in seqs.values() for _, sid, _, _ in rows})
    pidx = {sid: i for i, sid in enumerate(used)}
    pl = [stops[s] for s in used]
    px, py = geo.to_xy([float(s["stop_lon"]) for s in pl], [float(s["stop_lat"]) for s in pl])
    station_of = [s["parent_station"] or s["stop_id"] for s in pl]

    # Hops and through-movements. Each trip is assigned to the band in which it
    # *starts*: LTA's feed only starts trips at termini, so counting departures at a
    # mid-line station inside a clock window would show artificial gaps early in
    # the day. Band membership by start time describes each band's service level.
    hop_index: dict[tuple[int, int], int] = {}
    pattern_index: dict[tuple[str, ...], int] = {}
    hop_deps: list[list[tuple[int, int, int, int, int]]] = []  # per hop: (band, pattern, dep, run, dwell)
    through: dict[tuple[int, int], int] = defaultdict(int)
    for rows in seqs.values():
        rows.sort()
        band = _band_of(rows[0][3])
        if band is None:
            continue
        pattern = pattern_index.setdefault(tuple(r[1] for r in rows), len(pattern_index))
        prev_hop = None
        for (_, a, _, dep_a), (_, b, arr_b, dep_b) in zip(rows, rows[1:]):
            key = (pidx[a], pidx[b])
            h = hop_index.setdefault(key, len(hop_index))
            if h == len(hop_deps):
                hop_deps.append([])
            hop_deps[h].append((band, pattern, dep_a, arr_b - dep_a, dep_b - arr_b))
            if prev_hop is not None:
                through[(prev_hop, h)] |= 1 << band
            prev_hop = h

    H = len(hop_index)
    hop_wait = np.full((H, NB, NW), np.nan, np.float32)
    hop_run = np.full((H, NB), np.nan, np.float32)
    hop_dwell = np.full((H, NB), np.nan, np.float32)
    for h, recs in enumerate(hop_deps):
        arr = np.array(recs, np.float64)
        for b, band in enumerate(BANDS):
            sel = arr[arr[:, 0] == b]
            if len(sel) == 0:
                continue
            t0, t1 = config.BANDS[band]["window"]
            per_pattern = [sel[sel[:, 1] == p, 2] for p in np.unique(sel[:, 1])]
            hop_wait[h, b] = combined_waits(per_pattern, t1 - t0)
            hop_run[h, b] = np.median(sel[:, 3])
            hop_dwell[h, b] = np.median(sel[:, 4])

    # station entrances and street-to-platform access
    entrances = [s for s in stops.values() if s["location_type"] == "2"]
    ex, ey = geo.to_xy([float(s["stop_lon"]) for s in entrances], [float(s["stop_lat"]) for s in entrances])
    platforms_at: dict[str, list[int]] = defaultdict(list)
    for i, st in enumerate(station_of):
        platforms_at[st].append(i)
    access, access_m = [], []
    for e, s in enumerate(entrances):
        for p in platforms_at.get(s["parent_station"], []):
            access.append((e, p))
            access_m.append(np.hypot(ex[e] - px[p], ey[e] - py[p]) * config.STRAIGHT_LINE_DETOUR
                            + config.STATION_WALK_EXTRA_M)

    transfer, transfer_s, transfer_src = _transfers(pl, station_of, platforms_at)

    stations = []
    for st, plats in platforms_at.items():
        codes = sorted({pl[p]["stop_code"] for p in plats})
        lon, lat = geo.to_lonlat(np.mean(px[plats]), np.mean(py[plats]))
        stations.append({"id": st, "name": pl[plats[0]]["stop_name"], "codes": codes,
                         "lines": sorted({line_of(c) for c in codes}), "lon": float(lon), "lat": float(lat)})

    hops = np.array(sorted(hop_index, key=hop_index.get), np.int32).reshape(-1, 2)
    thr = np.array(list(through), np.int32).reshape(-1, 2)
    return RailModel(
        platform_id=used, platform_code=[s["stop_code"] for s in pl],
        platform_line=[line_of(s["stop_code"]) for s in pl], platform_station=station_of,
        platform_x=px, platform_y=py,
        entrance_id=[s["stop_id"] for s in entrances], entrance_x=ex, entrance_y=ey,
        access=np.array(access, np.int32).reshape(-1, 2), access_m=np.array(access_m, np.float32),
        transfer=np.array(transfer, np.int32).reshape(-1, 2), transfer_s=np.array(transfer_s, np.float32),
        transfer_source=transfer_src,
        hop=hops, hop_wait=hop_wait, hop_run=hop_run, hop_dwell=hop_dwell,
        through=thr, through_bands=np.array([through[tuple(t)] for t in thr], np.uint8),
        stations=stations,
        stats={"service_date": day.isoformat(), "active_services": sorted(active), "trips": len(seqs),
               "platforms": len(used), "entrances": len(entrances), "hops": H, "through_moves": len(thr),
               "transfers": len(transfer), "transfers_from_csv": transfer_src.count("csv")},
    )


def _band_of(start_seconds: float) -> int | None:
    for b, band in enumerate(BANDS):
        t0, t1 = config.BANDS[band]["window"]
        if t0 <= start_seconds < t1:
            return b
    return None


def _transfers(pl: list[dict], station_of: list[str], platforms_at: dict[str, list[int]]):
    """Platform-to-platform transfer edges with leisurely walking seconds."""
    by_code: dict[str, list[int]] = defaultdict(list)
    by_name_line: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, s in enumerate(pl):
        by_code[s["stop_code"]].append(i)
        by_name_line[(_norm(s["stop_name"]), line_of(s["stop_code"]))].append(i)

    def platforms(name: str, line: str, code: str) -> list[int]:
        # codes first (e.g. CG vs EW at Tanah Merah); names cover renumbered stations (CE1/CE2 -> CC34/CC33)
        return by_code.get(code) or by_name_line.get((_norm(name), line), [])

    pairs: dict[tuple[int, int], tuple[float, str]] = {}
    with open(config.MANUAL / "mrt_transfer_times.csv", encoding="utf-8-sig") as fh:
        for r in csv.DictReader(fh):
            src = platforms(r["Station Name"], r["Start Line"], r["Start Code"])
            dst = platforms(r["Station Name"], r["End Line"], r["End Code"])
            secs = float(r["Transfer time in seconds"])
            for a in src:
                for b in dst:
                    if a != b:
                        pairs[(a, b)] = (secs, "csv")
    for plats in platforms_at.values():
        for a in plats:
            for b in plats:
                if a == b or (a, b) in pairs:
                    continue
                same_line = line_of(pl[a]["stop_code"]) == line_of(pl[b]["stop_code"])
                pairs[(a, b)] = (config.DEFAULT_SAME_LINE_TRANSFER_S if same_line
                                 else config.DEFAULT_INTERCHANGE_S, "default")
    keys = sorted(pairs)
    return keys, [pairs[k][0] for k in keys], [pairs[k][1] for k in keys]
