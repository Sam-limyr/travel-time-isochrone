"""Integration tests against the built network (skipped until `python -m isochrone build` has run)."""
import base64

import numpy as np
import pytest

from isochrone import config

pytestmark = pytest.mark.skipif(not (config.BUILD / "meta.json").exists(), reason="network not built")

RAFFLES = (103.8515, 1.2840)
PLACES = {"jurong_east": (103.7422, 1.3332), "changi_airport": (103.9884, 1.3574),
          "woodlands": (103.7864, 1.4370), "bishan": (103.8484, 1.3508)}


@pytest.fixture(scope="module")
def engine():
    from isochrone.engine import Engine
    return Engine()


def minutes_at(result, lon, lat):
    g = result["grid"]
    arr = np.frombuffer(base64.b64decode(g["data"]), np.uint16).reshape(g["ny"], g["nx"])
    west, south, east, north = g["bounds"]
    col = int((lon - west) / (east - west) * g["nx"])
    row = int((north - lat) / (north - south) * g["ny"])
    v = int(arr[row, col])
    return None if v >= 65534 else v / 10


def iso(engine, **kw):
    from isochrone.engine import Request
    return engine.isochrone(Request(lon=RAFFLES[0], lat=RAFFLES[1], **kw))


def test_plausible_transit_times(engine):
    r = iso(engine)
    assert minutes_at(r, *RAFFLES) < 5
    assert 25 < minutes_at(r, *PLACES["jurong_east"]) < 50
    assert 20 < minutes_at(r, *PLACES["bishan"]) < 40
    assert 40 < minutes_at(r, *PLACES["changi_airport"]) < 70


def test_wait_assumptions_are_ordered(engine):
    best, avg, worst = (iso(engine, wait=w) for w in ("best", "avg", "worst"))
    for lon, lat in PLACES.values():
        assert minutes_at(best, lon, lat) <= minutes_at(avg, lon, lat) <= minutes_at(worst, lon, lat)


def test_faster_walking_is_never_slower(engine):
    slow, fast = iso(engine, walk_kmh=3.5), iso(engine, walk_kmh=6.0)
    for lon, lat in PLACES.values():
        assert minutes_at(fast, lon, lat) <= minutes_at(slow, lon, lat)


def test_removing_rail_slows_long_trips(engine):
    both, bus_only = iso(engine), iso(engine, rail=False)
    assert minutes_at(bus_only, *PLACES["jurong_east"]) > minutes_at(both, *PLACES["jurong_east"]) + 10


def test_car_is_faster_in_the_evening_than_the_am_peak(engine):
    am, eve = iso(engine, mode="car", band="am_peak"), iso(engine, mode="car", band="evening")
    t_am, t_eve = minutes_at(am, *PLACES["woodlands"]), minutes_at(eve, *PLACES["woodlands"])
    assert 15 < t_eve < t_am < 60


def test_parking_allowance_adds_time(engine):
    none, five = iso(engine, mode="car", parking_min=0), iso(engine, mode="car", parking_min=5)
    t0, t5 = minutes_at(none, *PLACES["bishan"]), minutes_at(five, *PLACES["bishan"])
    assert t5 == pytest.approx(t0 + 5, abs=0.2)


def test_resolutions_agree(engine):
    times = [minutes_at(iso(engine, res=res), *PLACES["changi_airport"]) for res in ("low", "med", "high")]
    assert max(times) - min(times) < 6


def test_route_legs_sum_to_total(engine):
    from isochrone.engine import Request
    req = Request(lon=RAFFLES[0], lat=RAFFLES[1])
    route = engine.route(req, *PLACES["jurong_east"])
    assert route["reachable"]
    assert sum(leg["seconds"] for leg in route["legs"]) == pytest.approx(route["total_s"], abs=len(route["legs"]) + 1)
    assert any(leg["type"] == "train" for leg in route["legs"])


def test_transfer_times_scale_with_walking_speed(engine):
    from isochrone.engine import K_TRANSFER, Request
    sel = engine.t_sel[K_TRANSFER]
    at_leisure = engine.transit_weights(Request(lon=RAFFLES[0], lat=RAFFLES[1], walk_kmh=config.LEISURELY_WALK_KMH))[sel]
    brisk = engine.transit_weights(Request(lon=RAFFLES[0], lat=RAFFLES[1], walk_kmh=config.LEISURELY_WALK_KMH * 1.5))[sel]
    assert np.allclose(at_leisure, np.maximum(engine.t_param[sel], 1e-3))
    assert np.allclose(brisk, np.maximum(engine.t_param[sel] / 1.5, 1e-3))


def test_invalid_requests_raise(engine):
    from isochrone.engine import Request
    with pytest.raises(ValueError):
        engine.isochrone(Request(lon=RAFFLES[0], lat=RAFFLES[1], band="night"))
    with pytest.raises(ValueError):
        engine.isochrone(Request(lon=103.70, lat=1.10))  # at sea, far from any footpath
    with pytest.raises(ValueError):
        engine.isochrone(Request(lon=103.7650, lat=1.4620))  # Johor Bahru, across the Causeway
    with pytest.raises(ValueError):
        engine.route(Request(lon=RAFFLES[0], lat=RAFFLES[1]), 103.7650, 1.4620)
