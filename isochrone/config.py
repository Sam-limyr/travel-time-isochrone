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
