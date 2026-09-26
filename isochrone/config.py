"""Paths, data sources and model constants shared by the pipeline and the server."""
from __future__ import annotations

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
RAW = DATA / "raw"        # small public datasets, committed to git
MANUAL = DATA / "manual"  # hand-curated inputs, committed to git
CACHE = DATA / "cache"    # large downloads (gitignored)
BUILD = DATA / "build"    # derived network artefacts (gitignored)
WEB = ROOT / "web"

# --- Data sources -----------------------------------------------------------
DATAMALL_BASE = "https://datamall2.mytransport.sg/ltaodataservice/"
OSM_PBF_URL = "http://download.openstreetmap.fr/extracts/asia/singapore-latest.osm.pbf"
HDB_DATASET_ID = "d_16b157c52ed637edd6ba1232e026258d"  # "HDB Existing Building" on data.gov.sg
REGION_DATASET_ID = "d_bf4d24df9129d5a8ff8cf82e20959ee0"  # URA "Master Plan 2019 Region Boundary (No Sea)"
DATAGOV_API = "https://api-open.data.gov.sg/v1/public/api/datasets/"
BUSROUTER_BASE = "https://data.busrouter.sg/v1/"

# Bounding box used to clip the OSM extract (lon/lat). Covers mainland Singapore,
# Sentosa and the southern islands that are reachable on foot.
BBOX = (103.59, 1.20, 104.05, 1.48)


# --- Time-of-day bands ----------------------------------------------------------
# These match the dispatch-frequency bands LTA publishes for buses. The train
# timetable is sampled over the same windows (seconds after midnight).
BANDS = {
    "am_peak": {"label": "AM peak", "hours": "06:30–08:30", "lta_freq": "AM_Peak_Freq",
                "window": (6 * 3600 + 1800, 8 * 3600 + 1800)},
    "midday": {"label": "Midday", "hours": "08:30–17:00", "lta_freq": "AM_Offpeak_Freq",
               "window": (8 * 3600 + 1800, 17 * 3600)},
    "pm_peak": {"label": "PM peak", "hours": "17:00–19:00", "lta_freq": "PM_Peak_Freq",
                "window": (17 * 3600, 19 * 3600)},
    "evening": {"label": "Evening", "hours": "19:00–23:00", "lta_freq": "PM_Offpeak_Freq",
                "window": (19 * 3600, 23 * 3600)},
}
BAND_KEYS = tuple(BANDS)

# Waiting-time assumptions applied at every boarding.
#   best:  the vehicle arrives just as you reach the stop (no wait)
#   avg:   you turn up at a random time (expected wait from the headways)
#   worst: you just missed one (wait the longest published/scheduled gap)
WAIT_MODES = ("best", "avg", "worst")

# --- Walking ------------------------------------------------------------------
WALK_KMH_DEFAULT = 4.8
WALK_KMH_MIN, WALK_KMH_MAX = 3.0, 6.5
# The Reddit interchange timings (data/manual) were walked at a leisurely pace;
# transfer times are scaled by LEISURELY_WALK_KMH / chosen walking speed.
LEISURELY_WALK_KMH = 4.0
STEPS_FACTOR = 1.4        # stairs take longer than the same horizontal distance
VOIDDECK_FACTOR = 1.15    # weaving round lift lobbies and pillars under an HDB block
STRAIGHT_LINE_DETOUR = 1.2  # applied to off-network straight-line walks (snapping)

# --- Buses --------------------------------------------------------------------
# Running times come from LTA's scheduled first/last-bus arrival times at each stop.
# These factors scale them to the time band (peaks are slower, evenings quicker).
BUS_BAND_FACTOR = {"am_peak": 1.10, "midday": 1.00, "pm_peak": 1.12, "evening": 0.92}
# Physical prior for stop-to-stop times: dwell + cruising at a speed that rises
# from BUS_CRUISE_KMH[0] on short hops to [1] on long (expressway) hops.
BUS_DWELL_S = 20.0
BUS_CRUISE_KMH = (22.0, 50.0)
BUS_SCHEDULE_FACTOR_RANGE = (0.6, 1.8)  # schedule/prior ratios outside this are treated as data errors

# --- Trains -------------------------------------------------------------------
STATION_ENTRY_S = 60.0    # fare gates + escalators, street to platform
STATION_EXIT_S = 45.0
STATION_WALK_EXTRA_M = 30.0  # platform-level walking on top of exit-to-platform distance
DEFAULT_INTERCHANGE_S = 180.0  # leisurely transfer time where the CSV has no entry
DEFAULT_SAME_LINE_TRANSFER_S = 60.0  # switching platforms on the same line

# --- Driving ------------------------------------------------------------------
# Typical-congestion speeds (km/h) by road class and band. They fold in junction
# delays and are in line with LTA's published peak averages (~60 km/h on
# expressways, ~28 km/h on arterial roads).
CAR_SPEEDS = {
    #                am   mid  pm   eve
    "motorway":      (58, 72, 55, 78),
    "motorway_link": (38, 45, 36, 48),
    "trunk":         (34, 40, 32, 44),
    "trunk_link":    (28, 32, 27, 34),
    "primary":       (27, 32, 26, 35),
    "primary_link":  (23, 27, 22, 29),
    "secondary":     (25, 29, 24, 32),
    "secondary_link": (22, 25, 21, 27),
    "tertiary":      (23, 26, 22, 28),
    "tertiary_link": (20, 23, 20, 24),
    "unclassified":  (21, 23, 20, 24),
    "residential":   (18, 20, 18, 21),
    "living_street": (10, 10, 10, 10),
    "service":       (12, 13, 12, 14),
    "road":          (18, 20, 18, 21),
}
PARKING_OPTIONS_MIN = (0, 2, 5)  # parking + walk-from-car allowance added at the destination

# --- Heatmap grid ---------------------------------------------------------------
RESOLUTIONS = {"low": 250, "med": 100, "high": 50}  # cell size in metres
GRID_SNAP_K = 4          # nearest network nodes considered per cell
GRID_MAX_SNAP_M = 400.0  # cells farther than this from any footpath are left blank
MAX_MINUTES = 180        # routing cut-off


def lta_account_key() -> str | None:
    """Read the LTA DataMall key from the environment or the gitignored .env file."""
    key = os.environ.get("LTA_ACCOUNT_KEY")
    env = ROOT / ".env"
    if not key and env.exists():
        for line in env.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == "LTA_ACCOUNT_KEY":
                key = value.strip()
    return key or None
