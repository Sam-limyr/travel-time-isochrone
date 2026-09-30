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


# SVY21 (EPSG:3414), Singapore's national grid: a transverse Mercator on the WGS84
# ellipsoid. Only the barrier grid (data/raw/barriers) uses it.
_SVY21_A, _SVY21_F = 6378137.0, 1 / 298.257223563
_SVY21_LAT0, _SVY21_LON0 = 1 + 22 / 60, 103 + 50 / 60  # 1°22′N, 103°50′E
_SVY21_N0, _SVY21_E0, _SVY21_K = 38744.572, 28001.642, 1.0


def _meridian_arc(lat_rad):
    e2 = 2 * _SVY21_F - _SVY21_F ** 2
    e4, e6 = e2 * e2, e2 ** 3
    a0 = 1 - e2 / 4 - 3 * e4 / 64 - 5 * e6 / 256
    a2 = 3 / 8 * (e2 + e4 / 4 + 15 * e6 / 128)
    a4 = 15 / 256 * (e4 + 3 * e6 / 4)
    a6 = 35 * e6 / 3072
    return _SVY21_A * (a0 * lat_rad - a2 * np.sin(2 * lat_rad) + a4 * np.sin(4 * lat_rad) - a6 * np.sin(6 * lat_rad))


def to_svy21(lon, lat):
    """lon/lat (WGS84) to SVY21 easting and northing in metres (LINZ transverse Mercator series)."""
    lon = np.asarray(lon, dtype=np.float64)
    lat = np.radians(np.asarray(lat, dtype=np.float64))
    e2 = 2 * _SVY21_F - _SVY21_F ** 2
    s, c, t = np.sin(lat), np.cos(lat), np.tan(lat)
    rho = _SVY21_A * (1 - e2) / (1 - e2 * s * s) ** 1.5
    nu = _SVY21_A / np.sqrt(1 - e2 * s * s)
    psi, t2 = nu / rho, t * t
    w = np.radians(lon - _SVY21_LON0)
    w2 = w * w
    m = _meridian_arc(lat) - _meridian_arc(np.radians(_SVY21_LAT0))
    north = m + nu * s * c * (w2 / 2 + w2 * w2 / 24 * c ** 2 * (4 * psi ** 2 + psi - t2)
                              + w2 ** 3 / 720 * c ** 4 * (8 * psi ** 4 * (11 - 24 * t2) - 28 * psi ** 3 * (1 - 6 * t2)
                                                           + psi ** 2 * (1 - 32 * t2) - psi * 2 * t2 + t2 * t2))
    east = nu * w * c * (1 + w2 / 6 * c ** 2 * (psi - t2)
                         + w2 * w2 / 120 * c ** 4 * (4 * psi ** 3 * (1 - 6 * t2) + psi ** 2 * (1 + 8 * t2)
                                                     - psi * 2 * t2 + t2 * t2))
    return _SVY21_E0 + _SVY21_K * east, _SVY21_N0 + _SVY21_K * north


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
