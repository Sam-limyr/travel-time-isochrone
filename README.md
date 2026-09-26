# SG Isochrones

Click anywhere in Singapore and see how long it takes to get everywhere else, as a
travel-time heatmap. Public transport covers walking, buses, MRT and LRT; a separate
car mode uses typical traffic for the time of day. Everything runs locally.

![Travel times from Toa Payoh in the weekday AM peak, with the fastest route to Changi Airport](docs/screenshot.png)

## Quick start (Windows)

Double-click **`run.cmd`**, or from a terminal:

```powershell
powershell -ExecutionPolicy Bypass -File run.ps1              # opens http://127.0.0.1:8000
powershell -ExecutionPolicy Bypass -File run.ps1 -Port 8080 -NoBrowser
```

On first run this creates a Python 3.12 virtual environment in `.venv`, installs the
packages in `requirements.txt`, and builds the networks (about a minute, and it
downloads the ~40 MB OpenStreetMap extract). Later runs start in a few seconds.

Manual equivalent (any OS):

```bash
python3.12 -m venv .venv && .venv/Scripts/python -m pip install -r requirements.txt   # .venv/bin/python on macOS/Linux
.venv/Scripts/python -m isochrone build     # networks -> data/build (gitignored)
.venv/Scripts/python -m isochrone serve --open
```

The map background loads tiles from OneMap (or Esri) over the internet; all routing
is computed locally.

## Using the app

- **Click** the map to set the start point (or drag the black pin). The heatmap shows the
  door-to-door travel time from there to every 50–250 m cell of land.
- **Right-click** a point (or use *Route* in the places table) to see the fastest itinerary
  there: walks, bus services, train lines, interchanges and waits.
- **Travel by**: public transport (walk + bus + MRT/LRT) or car.
- **Time of day**: weekday AM peak (06:30–08:30), midday, PM peak (17:00–19:00) or
  evening (19:00–23:00). These match the headway bands LTA publishes for buses.
- **Waiting at stops**: *best case* (every bus and train turns up as you arrive),
  *average* (you arrive at a random time) or *worst case* (you just missed each one).
- **Walking speed** slider (3–6.5 km/h). MRT interchange walks scale with it too (below).
- **Buses / MRT / HDB void-deck shortcuts** can each be switched off to compare.
- **Parking allowance** (car mode): 0, 2 or 5 minutes added at the destination.
- **Resolution**: low (250 m), medium (100 m) or high (50 m) grid cells.
- **Display**: a smooth ramp or five bands (5/10/15/20-minute width), opacity, and
  overlays for MRT/LRT lines, bus routes and bus stops. Hover for exact times, station
  names and the bus services on a road.
- The **Reachable area** and **Travel time to places** tables give the same information
  as text. Settings, start point and map view are kept in the URL, so links can be shared.

## How travel times are calculated

Each click runs one shortest-path search (Dijkstra) over a single graph, and the result
is sampled onto the grid.

**Walking** uses the real footpath and road network from OpenStreetMap (footways,
covered linkways, overhead bridges, steps and so on; expressways and private roads are
excluded). **HDB void decks**: HDB's building footprints are used to add a walking link
straight through each block wherever footpaths meet it on opposite sides (76,600 links,
from 13,447 building footprints). In estates this adds 3–12% to the area walkable within 15
minutes and saves up to about 6 minutes for some destinations.

**Buses** (777 service directions, 5,194 stops) come from LTA DataMall. Boarding costs a
wait taken from LTA's published dispatch-frequency range for the time band, e.g. "08-12"
minutes:

| Wait setting | Wait per boarding |
|---|---|
| Best case | 0 |
| Average | half the middle of the range (5 min for 08-12) |
| Worst case | the top of the range (12 min) |

Stop-to-stop running times combine a physical model (20 s dwell plus a cruising speed
that rises from 22 km/h on short hops to 50 km/h on long expressway hops) with LTA's
scheduled arrival times at each stop. The schedules are rounded to the minute and
sometimes mix trips, so they only rescale the model locally where they agree with it.
Peak-band factors (×1.10 AM, ×1.12 PM, ×0.92 evening) cover traffic.

**Trains** use LTA's official GTFS timetable (on DataMall since August 2026) for a
reference weekday: 6,032 trips over 426 platforms. The network is modelled per track
segment, so segments served by several service patterns (for example the Circle Line)
get their combined frequency. Riding through a station is only allowed along platform
sequences that real trips run. Expected and worst-case waits come from the actual gaps
between departures, so irregular service waits longer than half the headway. Station
access uses LTA's exit coordinates plus 60 s to enter and 45 s to leave (fare gates and
escalators).

**Interchanges** use the Reddit-measured leisurely walking times in
[`data/manual/mrt_transfer_times.csv`](data/manual/mrt_transfer_times.csv), scaled by
`4.0 km/h ÷ your walking speed`. At the default 4.8 km/h a 200 s leisurely transfer
takes 167 s. They're matched by station code, falling back to name and line for the
renumbered Bayfront and Marina Bay (CE1/CE2 are now CC34/CC33). Interchanges not in the
file default to 180 s, or 60 s for switching platforms on the same line.

**Driving** uses OpenStreetMap roads with one-way rules. Speeds depend on road class and
time band, with peaks calibrated to LTA's measured peak-hour averages: 55 km/h on
expressways and 29 km/h on arterial roads in 2025
([`data/raw/lta/average_peak_speeds.csv`](data/raw/lta/average_peak_speeds.csv)). The
start and destination are joined to the nearest road on foot.

**The grid**: each cell takes the fastest of its four nearest network nodes plus the
remaining walk (straight line × 1.2). Cells more than 400 m from any footpath (forest
interiors, runways, military areas) are left blank. Resolution barely affects speed: a
new search takes about 0.1 s, and sampling it takes 5–40 ms. Changing resolution, the
parking allowance or the colour scale reuses the last search.

### Validation

`scripts/validate_mrt.py` compares modelled station-to-station times with an external
matrix, such as the mrt.sg matrix used in the sibling *the-fastest-journey* project. With
average waits, across 20,306 station pairs:

| Measure | Result |
|---|---|
| Median difference | +0.2 min |
| Mean absolute difference | 3.2 min |
| Pairs within 5 min | 80% |

The largest differences are long East–West Line trips: LTA's timetable schedules
102.7 min end to end, while mrt.sg estimated 83 min. `scripts/probe.py` prints times to
well-known places from any origin, and `pytest` runs 37 unit and integration tests.

## Data sources and freshness

| Data | Source | As of |
|---|---|---|
| Bus stops, services, routes, headways | LTA DataMall | fetched 26 Sep 2026 |
| MRT/LRT timetable, platforms, station exits | LTA DataMall GTFS Schedule (Train) | published 26 Sep 2026; service day Mon 28 Sep 2026 |
| Footpaths and roads | OpenStreetMap (openstreetmap.fr extract) | 25 Sep 2026 |
| MRT/LRT line shapes (overlay) | OpenStreetMap route relations | 25 Sep 2026 |
| HDB building footprints | HDB via data.gov.sg | fetched 26 Sep 2026 |
| Singapore land outline | URA Master Plan 2019 region boundary (no sea) | 2019 |
| Peak-hour road speeds | LTA via data.gov.sg | to 2025 |
| Bus route lines (overlay) | [busrouter.sg](https://busrouter.sg) mirror of LTA data | 1 Sep 2026 |

The train timetable reflects the network open today, including Circle Line Stage 6
(Keppel, Cantonment, Prince Edward Road). Stations not yet in LTA's timetable, such as
the TEL Stage 5 and DTL extension stations, aren't included.

**Small datasets are committed** under `data/raw` (about 10 MB). The OSM extract, raw HDB
GeoJSON and built networks live in the gitignored `data/cache` and `data/build`.

### Refreshing the data

LTA DataMall needs a free account key ([datamall.lta.gov.sg](https://datamall.lta.gov.sg)).
Put it in a `.env` file in the project root (gitignored):

```
LTA_ACCOUNT_KEY=your-key
```

Then:

```bash
.venv/Scripts/python -m isochrone fetch                 # all sources
.venv/Scripts/python -m isochrone fetch --only osm --force   # just a newer OSM extract
.venv/Scripts/python -m isochrone build                 # rebuild networks (~45 s)
```

Sources: `lta`, `speeds`, `osm`, `hdb`, `boundary`, `busrouter`. Only `lta` needs the key.
Each fetch records its time and licence in `data/raw/sources.json`. The build uses today's
date, or the next weekday, as the timetable service day.

## Project layout

```
isochrone/
  config.py      model constants: bands, speeds, waits, grid sizes, data sources
  fetch.py       downloaders (LTA DataMall, OSM, data.gov.sg, busrouter)
  osm.py         OSM -> walk and drive graphs
  transit.py     bus and train service models
  build.py       assembles networks, void-deck links, grids and overlays -> data/build
  engine.py      per-request edge weights, Dijkstra, grid sampling, itineraries
  server.py      FastAPI app (API + static front end)
web/             index.html, app.js, style.css, vendored MapLibre GL JS 5.24
data/raw/        cached public datasets (committed)
data/manual/     hand-curated inputs (MRT interchange timings)
scripts/         probe.py, validate_mrt.py, screenshot.py (dev tools)
tests/           pytest suite
```

Tunable assumptions (speeds, dwell and access times, band factors, grid sizes) are all in
`isochrone/config.py`.

## API

| Endpoint | Parameters |
|---|---|
| `GET /api/isochrone` | `lat`, `lon`, `mode` (`transit`/`car`), `band` (`am_peak`/`midday`/`pm_peak`/`evening`), `wait` (`best`/`avg`/`worst`), `walk_kmh`, `res` (`low`/`med`/`high`), `bus`, `rail`, `voiddeck`, `parking`. Returns the grid as base64 little-endian uint16 in tenths of a minute (65535 = no data, 65534 = not reached within 180 min), plus reachable areas. |
| `GET /api/route` | Same as above plus `to_lat`, `to_lon`. Returns itinerary legs with times and geometry. |
| `GET /api/meta` | Bands, options, data sources and build statistics. |
| `GET /api/overlays/{mrt_lines,mrt_stations,bus_routes,bus_stops}` | GeoJSON. |

## Limitations

- **Typical, not live**: no real-time bus arrivals, train disruptions or live traffic.
  Weekdays only.
- **One bus service per boarding**: at a stop served by several services going your way,
  real passengers take whichever comes first; the model waits for the best single
  service, so busy corridors can look slightly slower than they are.
- **Timetable start points**: LTA's timetable (v0.1) starts every train at a terminus, so
  trips are grouped into time bands by departure from the terminus. This avoids false
  early-morning gaps mid-line, but can shift a band's service level by up to one trip
  length.
- **Excluded services**: bus services with no published headways (20 directions, mostly
  short workings such as 7A and a few City Direct runs), the Malaysian end of cross-border
  buses, ferries, Sentosa Express and the Changi Airport Skytrain.
- **Drawn geometry**: train legs in the route view are straight lines between stations,
  and bus legs are drawn stop to stop.
- **Basemap quirk**: OneMap draws a Pedra Branca inset box in the sea at low zoom. The
  Esri grey basemap avoids it and puts place names above the heatmap.

## Licences and attribution

Contains information from LTA DataMall, HDB and URA, accessed via data.gov.sg under the
[Singapore Open Data Licence v1.0](https://data.gov.sg/open-data-licence). Map data ©
OpenStreetMap contributors, available under the
[ODbL](https://www.openstreetmap.org/copyright). Basemap © OneMap / Singapore Land
Authority, or © Esri. Bus route lines are from
[cheeaun/sgbusdata](https://github.com/cheeaun/sgbusdata). MapLibre GL JS is BSD-3-Clause
licensed (`web/vendor/maplibre-gl/LICENSE.txt`). The MRT interchange timings are
community-measured (Reddit).
