"""Geometry helpers.

Everything is computed in a local equirectangular projection centred on Singapore
(metres). Over Singapore's extent its scale error is below 0.02 %, which is far
below the accuracy of anything else in the model.
"""
from __future__ import annotations

import json
import math

import numpy as np
import shapely
from shapely.geometry import shape

from . import config

LAT0 = 1.35
LON0 = 103.82
M_PER_DEG_LAT = 110_574.0
M_PER_DEG_LON = 111_320.0 * math.cos(math.radians(LAT0))


def to_xy(lon, lat):
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.asarray(lat, dtype=np.float64)
    return (lon - LON0) * M_PER_DEG_LON, (lat - LAT0) * M_PER_DEG_LAT


def to_lonlat(x, y):
    x = np.asarray(x, dtype=np.float64)
    y = np.asarray(y, dtype=np.float64)
    return x / M_PER_DEG_LON + LON0, y / M_PER_DEG_LAT + LAT0


def project_geom(geom):
    """Project a lon/lat shapely geometry into local metres."""
    return shapely.transform(geom, lambda c: np.column_stack(to_xy(c[:, 0], c[:, 1])))


def land_polygon_xy():
    """Singapore land outline (URA Master Plan 2019, no sea) in local metres."""
    path = config.RAW / "boundary" / "sg_land.geojson"
    geom = shape(json.loads(path.read_text(encoding="utf-8"))["geometry"])
    return project_geom(geom)


def decode_polyline(encoded: str, precision: int = 5) -> list[tuple[float, float]]:
    """Decode a Google encoded polyline into [(lon, lat), ...]."""
    coords, index, lat, lon = [], 0, 0, 0
    factor = 10 ** precision
    while index < len(encoded):
        for is_lon in (False, True):
            shift = result = 0
            while True:
                b = ord(encoded[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            delta = ~(result >> 1) if result & 1 else result >> 1
            if is_lon:
                lon += delta
            else:
                lat += delta
        coords.append((lon / factor, lat / factor))
    return coords
