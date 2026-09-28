"""FastAPI app: JSON API plus the static web front end."""
from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.middleware.gzip import GZipMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config
from .engine import Engine, Request

OVERLAYS = {"mrt_lines", "mrt_stations", "bus_routes", "bus_stops"}
engine: Engine | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global engine
    engine = Engine()
    yield


app = FastAPI(title="Singapore travel-time isochrones", lifespan=lifespan)
app.add_middleware(GZipMiddleware, minimum_size=1024)


def _request(lat: float, lon: float, mode: str, band: str, wait: str, walk_kmh: float, res: str,
             bus: bool, rail: bool, voiddeck: bool, parking: float, direction: str = "from") -> Request:
    return Request(lon=lon, lat=lat, mode=mode, band=band, wait=wait, walk_kmh=walk_kmh, res=res,
                   bus=bus, rail=rail, voiddeck=voiddeck, parking_min=parking, direction=direction)


@app.get("/api/meta")
def meta() -> dict:
    m = engine.meta
    return {
        "bands": [{"key": k, "label": v["label"], "hours": v["hours"]} for k, v in config.BANDS.items()],
        "wait_modes": list(config.WAIT_MODES),
        "resolutions": [{"key": k, "res_m": m["grids"][k]["res_m"], "cells": m["grids"][k]["cells"]}
                        for k in config.RESOLUTIONS],
        "walk_kmh": {"default": config.WALK_KMH_DEFAULT, "min": config.WALK_KMH_MIN,
                     "max": config.WALK_KMH_MAX, "leisurely": config.LEISURELY_WALK_KMH},
        "parking_min": list(config.PARKING_OPTIONS_MIN),
        "max_minutes": config.MAX_MINUTES,
        "places": [{"name": n, "lon": lon, "lat": lat} for n, lon, lat in config.PLACES],
        "key_destinations": {"count": len(engine.key_dest), "skipped": engine.key_skipped},
        "service_date": m["service_date"],
        "built_at": m["built_at"],
        "sources": m["sources"],
        "stats": m["stats"],
    }


@app.get("/api/isochrone")
def isochrone(lat: float, lon: float, mode: str = "transit", band: str = "am_peak", wait: str = "avg",
              walk_kmh: float = config.WALK_KMH_DEFAULT, res: str = "med", bus: bool = True, rail: bool = True,
              voiddeck: bool = True, parking: float = 0, direction: str = "from") -> dict:
    try:
        return engine.isochrone(_request(lat, lon, mode, band, wait, walk_kmh, res, bus, rail, voiddeck, parking,
                                         direction))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/route")
def route(lat: float, lon: float, to_lat: float, to_lon: float, mode: str = "transit", band: str = "am_peak",
          wait: str = "avg", walk_kmh: float = config.WALK_KMH_DEFAULT, bus: bool = True, rail: bool = True,
          voiddeck: bool = True, parking: float = 0) -> dict:
    try:
        return engine.route(_request(lat, lon, mode, band, wait, walk_kmh, "med", bus, rail, voiddeck, parking),
                            to_lon, to_lat)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/api/overlays/{name}")
def overlay(name: str) -> FileResponse:
    if name not in OVERLAYS:
        raise HTTPException(status_code=404, detail="unknown overlay")
    return FileResponse(config.BUILD / "overlays" / f"{name}.geojson", media_type="application/geo+json",
                        headers={"Cache-Control": "max-age=3600"})


app.mount("/", StaticFiles(directory=config.WEB, html=True), name="web")
