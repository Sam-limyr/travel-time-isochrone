"""Unit tests for the pure model functions (no network build needed)."""
import numpy as np
import pytest

from isochrone import geo, transit


@pytest.mark.parametrize("raw, expected", [
    ("08-12", (8.0, 12.0)), ("10", (10.0, 10.0)), ("5-8", (5.0, 8.0)), ("12-09", (9.0, 12.0)),
    ("-", None), ("", None), ("00-00", None),
])
def test_parse_headway(raw, expected):
    assert transit.parse_headway(raw) == expected


def test_bus_waits_best_avg_worst():
    assert transit.bus_waits((8, 12)) == [0.0, 300.0, 720.0]
    assert all(np.isnan(w) for w in transit.bus_waits(None))


def test_gap_stats_regular_and_bunched():
    regular = np.arange(0, 7200, 300.0)  # every 5 min
    assert transit.gap_stats(regular, 7200) == (0.0, 150.0, 300.0)
    # bunched pairs wait longer on average than an even service with the same trips
    best, bunched, worst = transit.gap_stats(np.array([0, 60, 600, 660, 1200.0]), 7200)
    assert bunched > 150 and worst == 540


def test_combined_waits_add_frequencies():
    reg = np.arange(0, 7200, 300.0)
    _, one, worst_one = transit.combined_waits([reg], 7200)
    _, two, worst_two = transit.combined_waits([reg, reg + 150], 7200)
    assert one == pytest.approx(150)
    assert two == pytest.approx(75)
    assert worst_two == pytest.approx(worst_one / 2)


def test_hop_times_ignore_mismatched_schedule():
    # 10 stops, 0.4 km apart; the "last bus" jumps 80 minutes mid-route (a different trip)
    dist = np.arange(10) * 0.4
    straight = np.full(9, 0.35)
    bad = np.array([0, 1, 2, 3, 4, 84, 85, 86, 87, 88], float)
    good = np.array([0, 1.4, 2.8, 4.2, 5.6, 7.0, 8.4, 9.8, 11.2, 12.6])
    hops = transit.hop_times(dist, [bad, good], straight)
    assert hops.sum() / 60 == pytest.approx(12.6, rel=0.15)


def test_model_hop_seconds_is_monotonic_and_faster_on_long_hops():
    km = np.array([0.2, 0.4, 1.0, 3.0, 8.0])
    secs = transit.model_hop_seconds(km)
    assert np.all(np.diff(secs) > 0)
    speed = km / (secs / 3600)
    assert np.all(np.diff(speed) > 0)


@pytest.mark.parametrize("code, line", [
    ("NS1", "NSL"), ("EW24", "EWL"), ("CG1", "EWL"), ("CC34", "CCL"), ("CE2", "CCL"), ("DT35", "DTL"),
    ("TE20", "TEL"), ("BP6", "BPLRT"), ("STC", "SKLRT"), ("SW4", "SKLRT"), ("PTC", "PGLRT"), ("PE5", "PGLRT"),
])
def test_line_of(code, line):
    assert transit.line_of(code) == line


def test_projection_round_trip():
    lon, lat = np.array([103.6, 103.85, 104.05]), np.array([1.2, 1.35, 1.47])
    x, y = geo.to_xy(lon, lat)
    lon2, lat2 = geo.to_lonlat(x, y)
    assert np.allclose(lon, lon2) and np.allclose(lat, lat2)
    # 0.01 degree of latitude is ~1.1 km
    assert geo.to_xy(103.8, 1.36)[1] - geo.to_xy(103.8, 1.35)[1] == pytest.approx(1105.74, rel=1e-3)


def test_decode_polyline_google_example():
    coords = geo.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@")
    assert coords == [(-120.2, 38.5), (-120.95, 40.7), (-126.453, 43.252)]


def test_car_peak_speeds_match_lta_measurements():
    """Peak-band speeds for expressways and arterial roads stay near LTA's latest measured averages."""
    import csv

    from isochrone import config

    with open(config.RAW / "lta" / "average_peak_speeds.csv", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["ave_speed_expressway"] and r["ave_speed_arterial_roads"]]
    latest = max(rows, key=lambda r: int(r["year"]))
    am, pm = config.BAND_KEYS.index("am_peak"), config.BAND_KEYS.index("pm_peak")
    peak = lambda cls: (config.CAR_SPEEDS[cls][am] + config.CAR_SPEEDS[cls][pm]) / 2
    arterial = (peak("trunk") + peak("primary")) / 2
    assert peak("motorway") == pytest.approx(float(latest["ave_speed_expressway"]), abs=3)
    assert arterial == pytest.approx(float(latest["ave_speed_arterial_roads"]), abs=3)
