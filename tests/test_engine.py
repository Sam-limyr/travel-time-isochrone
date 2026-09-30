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
    assert 22 < minutes_at(r, *PLACES["jurong_east"]) < 50
    assert 18 < minutes_at(r, *PLACES["bishan"]) < 40
    assert 30 < minutes_at(r, *PLACES["changi_airport"]) < 70


def test_wait_assumptions_are_ordered(engine):
    best, avg, worst = (iso(engine, wait=w) for w in ("best", "avg", "worst"))
    for lon, lat in PLACES.values():
        assert minutes_at(best, lon, lat) <= minutes_at(avg, lon, lat) <= minutes_at(worst, lon, lat)


def test_faster_walking_is_never_slower(engine):
    slow, fast = iso(engine, walk_kmh=3.5), iso(engine, walk_kmh=6.0)
    for lon, lat in PLACES.values():
        assert minutes_at(fast, lon, lat) <= minutes_at(slow, lon, lat)


def test_train_waits_follow_published_peak_and_off_peak_frequencies(engine):
    codes = engine.meta["names"]["platform_code"]
    nsl = [h for h, (a, b) in enumerate(engine.tr["hop"]) if codes[a].startswith("NS") and codes[b].startswith("NS")]
    w = engine.tr["hop_wait"][nsl] / 60  # (hops, bands, best/avg/worst) minutes
    am, midday, pm, evening = range(4)
    assert np.nanmax(w[:, :, 0]) == 0                                   # best case: no waiting
    assert np.nanmax(w[:, am, 1]) < np.nanmin(w[:, midday, 1])           # peak trains every 2-5 min, off-peak 4-5
    assert np.nanmax(w[:, pm, 2]) < np.nanmin(w[:, evening, 2]) + 0.01   # worst case follows the band too
    assert set(engine.rail_sections.values()) and min(engine.rail_sections.values()) > 0  # every row applies


def test_peak_hours_are_faster_by_public_transport(engine):
    peak, midday = iso(engine, band="am_peak"), iso(engine, band="midday")
    assert peak["area_km2"]["45"] > midday["area_km2"]["45"]


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


def test_land_cells_include_land_without_footpaths(engine):
    g = iso(engine)["grid"]
    mapped = int((np.frombuffer(base64.b64decode(g["data"]), np.uint16) != 65535).sum())
    land_km2 = g["land_cells"] * (g["res_m"] / 1000) ** 2
    assert mapped < g["land_cells"]  # forest, airfields etc. are land but not mapped
    assert 700 < land_km2 < 800      # URA's outline, reservoirs and offshore islands included


def test_key_destinations_score(engine):
    from isochrone.engine import Request
    profiles = iso(engine)["key_destinations"]
    assert not [p["skipped"] for p in engine.key_profiles if p["skipped"]]  # every place name resolves
    assert set(profiles) == {p["key"] for p in engine.key_profiles}
    k = profiles["general"]
    assert sum(g["weight"] for g in k["groups"]) == pytest.approx(100)
    assert all(p["unreachable"] == 0 for p in profiles.values())
    assert next(i for i in k["items"] if i["name"] == "Raffles Place")["minutes"] < 5  # we start there
    tuas = engine.isochrone(Request(lon=103.6369, lat=1.3404))["key_destinations"]
    assert tuas["general"]["weighted_min"] > k["weighted_min"] + 20
    # each destination's time is the router's total for that trip
    item = next(i for i in k["items"] if i["name"] == "Changi Airport")
    route = engine.route(Request(lon=RAFFLES[0], lat=RAFFLES[1]), item["lon"], item["lat"])
    assert item["minutes"] == pytest.approx(route["total_s"] / 60, abs=0.1)


def test_key_destination_profiles_favour_their_places(engine):
    from isochrone.engine import Request
    score = lambda lon, lat, key: engine.isochrone(Request(lon=lon, lat=lat))["key_destinations"][key]["weighted_min"]
    woodlands = PLACES["woodlands"]
    assert score(*RAFFLES, "cbd") < score(*RAFFLES, "general") < score(*woodlands, "general")
    assert score(*woodlands, "jb") < score(*RAFFLES, "jb")                 # near the Causeway
    assert score(*PLACES["jurong_east"], "industrial") < score(*RAFFLES, "industrial")
    assert score(103.7650, 1.3150, "student") < score(*RAFFLES, "student")  # Clementi: NUS and the polys


def test_profile_map_is_the_key_destinations_score_everywhere(engine):
    from isochrone.engine import Request
    anywhere = Request(lon=0, lat=0, res="high")  # a profile map has no start point
    pm = engine.profile_isochrone(anywhere, "cbd")
    assert pm["places"] == len(next(p for p in engine.key_profiles if p["key"] == "cbd")["place_weights"])
    for lon, lat in (RAFFLES, PLACES["bishan"], PLACES["woodlands"]):
        score = engine.key_destinations_at(Request(lon=lon, lat=lat))["cbd"]["weighted_min"]
        assert minutes_at(pm, lon, lat) == pytest.approx(score, abs=1.0)
    again = engine.profile_isochrone(anywhere, "cbd")
    assert again["timing_ms"]["cached"] and again["grid"]["data"] == pm["grid"]["data"]
    with pytest.raises(ValueError):
        engine.profile_isochrone(anywhere, "astronaut")


def test_times_to_a_place_match_the_router(engine):
    from isochrone.engine import Request
    to = engine.isochrone(Request(lon=RAFFLES[0], lat=RAFFLES[1], direction="to", res="high"))
    assert to["key_destinations"] is None
    for lon, lat in PLACES.values():  # searched backwards from Raffles, routed forwards to it
        route = engine.route(Request(lon=lon, lat=lat), *RAFFLES)
        assert minutes_at(to, lon, lat) == pytest.approx(route["total_s"] / 60, abs=1.0)


def test_overlapping_queries_are_safe():
    """The server answers on several threads; overlapping queries on a fresh engine used to
    segfault inside GEOS (the land check)."""
    from concurrent.futures import ThreadPoolExecutor

    from isochrone.engine import Engine, Request
    fresh = Engine()
    with ThreadPoolExecutor(6) as pool:
        futures = [pool.submit(fresh.isochrone, Request(lon=lon, lat=lat, direction="to")) for lon, lat in PLACES.values()]
        futures += [pool.submit(fresh.route, Request(lon=RAFFLES[0], lat=RAFFLES[1]), lon, lat) for lon, lat in PLACES.values()]
        futures += [pool.submit(fresh.profile_isochrone, Request(lon=0, lat=0, res="low"), key) for key in ("cbd", "jb")]
        assert all(f.result() for f in futures)


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


def test_station_access_by_depth_and_line(engine):
    from isochrone.engine import K_STATION_IN, K_STATION_OUT, Request
    codes = engine.meta["names"]["platform_code"]
    access = lambda code: engine.platform_access_s[codes.index(code)]
    assert access("EW23") == 30    # Clementi, elevated
    assert access("NS24") == 75    # Dhoby Ghaut, underground North-South Line
    assert access("DT5") == 120    # Beauty World, Downtown Line
    assert access("DT21") == pytest.approx(43 * config.STATION_ACCESS_S_PER_M)  # Bencoolen, 43 m down
    assert not [r["stations"] for r in engine.station_access if not r["codes"]]  # every row applies
    # the same both ways, and scaled with walking speed
    req = Request(lon=RAFFLES[0], lat=RAFFLES[1])
    brisk = Request(lon=RAFFLES[0], lat=RAFFLES[1], walk_kmh=config.WALK_KMH_DEFAULT * 1.25)
    ins, outs = engine.t_sel[K_STATION_IN], engine.t_sel[K_STATION_OUT]
    w, w_brisk = engine.transit_weights(req), engine.transit_weights(brisk)
    assert np.allclose(np.sort(w[ins]), np.sort(w[outs]))
    assert np.allclose(w_brisk[ins], w[ins] / 1.25)
    assert w[ins].min() == pytest.approx(30)


def test_invalid_requests_raise(engine):
    from isochrone.engine import Request
    with pytest.raises(ValueError):
        engine.isochrone(Request(lon=RAFFLES[0], lat=RAFFLES[1], band="night"))
    with pytest.raises(ValueError):
        engine.isochrone(Request(lon=RAFFLES[0], lat=RAFFLES[1], direction="sideways"))
    with pytest.raises(ValueError):
        engine.isochrone(Request(lon=103.70, lat=1.10))  # at sea, far from any footpath
    with pytest.raises(ValueError):
        engine.isochrone(Request(lon=103.7650, lat=1.4620))  # Johor Bahru, across the Causeway
    with pytest.raises(ValueError):
        engine.route(Request(lon=RAFFLES[0], lat=RAFFLES[1]), 103.7650, 1.4620)
