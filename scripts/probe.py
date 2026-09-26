"""Print travel times from an origin to a few well-known places (sanity checks)."""
import base64
import sys
import time

import numpy as np

from isochrone import geo
from isochrone.engine import Engine, Request

PLACES = {
    "Raffles Place MRT": (103.8515, 1.2840), "Jurong East MRT": (103.7422, 1.3332),
    "Tampines MRT": (103.9455, 1.3534), "Changi Airport MRT": (103.9884, 1.3574),
    "Woodlands MRT": (103.7864, 1.4370), "Punggol MRT": (103.9022, 1.4052),
    "Bishan MRT": (103.8484, 1.3508), "Clementi MRT": (103.7650, 1.3150),
    "NUS (Kent Ridge)": (103.7764, 1.2966), "Sentosa (Beach Stn)": (103.8190, 1.2507),
    "Tuas Link MRT": (103.6369, 1.3404), "Bedok North (HDB)": (103.9290, 1.3330),
}


def sample(result, lon, lat):
    g = result["grid"]
    arr = np.frombuffer(base64.b64decode(g["data"]), np.uint16).reshape(g["ny"], g["nx"])
    west, south, east, north = g["bounds"]
    col = int((lon - west) / (east - west) * g["nx"])
    row = int((north - lat) / (north - south) * g["ny"])
    val = arr[row, col]
    return None if val >= 65534 else val / 10


def main():
    t = time.perf_counter()
    eng = Engine()
    print(f"engine loaded in {time.perf_counter() - t:.1f}s")
    origin = sys.argv[1] if len(sys.argv) > 1 else "Raffles Place MRT"
    lon, lat = PLACES[origin]
    for kwargs in ({}, {"wait": "best"}, {"wait": "worst"}, {"mode": "car"}, {"mode": "car", "band": "evening"},
                   {"rail": False}, {"bus": False}, {"res": "high"}):
        r = eng.isochrone(Request(lon=lon, lat=lat, **kwargs))
        times = {k: sample(r, *p) for k, p in PLACES.items() if k != origin}
        print(f"\n{origin} {kwargs or '(defaults: transit, AM peak, avg wait, 4.8 km/h)'} timing={r['timing_ms']} area={r['area_km2']}")
        print("   " + ", ".join(f"{k}: {v:.0f}" if v is not None else f"{k}: -" for k, v in times.items()))


if __name__ == "__main__":
    main()
