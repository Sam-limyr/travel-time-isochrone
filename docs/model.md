# Model assumptions

Every adjustment the travel-time model makes, with its value, where it comes from, and where
to change it. The [README](../README.md) explains how the pieces fit together; this page is
the reference. Constants in `CAPITALS` live in `isochrone/config.py` unless another file is
named. Files under `data/manual/` are hand-curated tables: restart the server after
editing them. A change marked **rebuild** needs `poetry run isochrone build` (about two
minutes).

## Walking

| What | Value | Why, or source | Where |
|---|---|---|---|
| Footpath network | OpenStreetMap footways, paths, steps, pedestrian streets, cycleways, service roads and ordinary roads with footpaths | OpenStreetMap extract. Excludes `foot=no/private/use_sidepath`, private access, trunk roads mapped without sidewalks, and expressways | `isochrone/osm.py`, **rebuild** |
| Walking speed | 4.8 km/h (slider 3–6.5) | Typical adult pace | `WALK_KMH_DEFAULT`, `WALK_KMH_MIN/MAX` |
| Stairs | ×1.4 the time of flat ground | Climbing is slower than walking | `STEPS_FACTOR` |
| HDB void decks | A link straight through a block wherever footpaths within 15 m of it meet on opposite sides, up to 100 m, up to 4 per footpath node; ×1.15 | HDB building footprints (data.gov.sg); weaving round lift lobbies and pillars. 76,600 links from 13,447 footprints | `VOIDDECK_*` in `isochrone/build.py`, `VOIDDECK_FACTOR`; **rebuild** |
| Straight walks off the footpaths | Straight line ×1.2 | Real routes are rarely straight | `STRAIGHT_LINE_DETOUR` |
| Barriers to straight walks | A straight walk may not cross an expressway, major (trunk or primary) road, river or canal, other water, at-grade railway, or fenced grounds (schools, prisons, military land, airfields, golf courses), except where OpenStreetMap maps a crossing: a pedestrian crossing, junction, overhead bridge, underpass or road bridge. It may start or end on one (a point on a road, a bridge, school grounds) | The barrier-aware distance of the hdb-resale-analysis project: its 3 m barrier grid, copied into `data/raw/barriers` | `isochrone/barriers.py`, `BARRIERS`; `isochrone fetch --only barriers`, then **rebuild** |
| A point on a barrier | Steps off to the nearest open ground within 60 m first (a kerbside bus stop joins the footpath on its own side). Map cells don't step: they start from wherever their centre falls | As the hdb-resale-analysis project moves blocks and exits | `BARRIER_MOVE_MAX_M` |
| A point no barrier-free walk can join | Falls back to plain straight links (for clicked points, stops, exits and key places). A map cell in that position is left blank | Points must connect; blank cells are more honest than a walk across an expressway (about 1.9% of cells, mostly fringe land) | `barriers.snap(fallback=…)` |
| Map cell to the network | Its 4 nearest footpath nodes within 400 m, plus its nearest station exit and nearest bus stop within 400 m, each by a barrier-aware straight walk. Cells that reach none are blank | Joining exits and stops directly keeps a block beside an exit from depending on OpenStreetMap having the last few metres of path | `GRID_SNAP_K`, `GRID_MAX_SNAP_M`, `ACCESS_SNAP_M`; **rebuild** |
| Clicked point to the network | As a map cell, but footpaths up to 1,000 m away. It must be on land: inside URA's outline, or within 150 m of it (piers, new reclamation) | | `ORIGIN_K`, `FOOTPATH_K`, `ORIGIN_MAX_M`, `LAND_MARGIN_M` in `isochrone/engine.py` |
| Walking straight to a spot near the start | Allowed where it's under 600 m walked (500 m straight) and crosses no barrier, if faster than the network | Open ground near the start | `DIRECT_MAX_M` in `isochrone/engine.py` |
| Bus stop to the network | Its 2 nearest barrier-free footpath nodes within 150 m. Stops farther than 150 m from any footpath are dropped | | `STOP_SNAP_M` in `isochrone/build.py`; **rebuild** |
| Station exit to the network | Its 2 nearest barrier-free footpath nodes within 300 m | LTA's exit coordinates, from the train timetable feed | `ENTRANCE_SNAP_M` in `isochrone/build.py`; **rebuild** |
| Isolated footpaths | Pieces of the network with fewer than 1,000 nodes are ignored when joining points | They're usually private or mis-mapped | `MAJOR_COMPONENT` in `isochrone/build.py`; **rebuild** |

## Buses

| What | Value | Why, or source | Where |
|---|---|---|---|
| Services and stops | 777 service directions, 5,194 stops | LTA DataMall. Services without published headways (22 directions, mostly short workings) are left out | `isochrone/transit.py` |
| Wait at each boarding | Best case 0; average half the middle of the published range (5 min for "08-12"); worst case the top of the range (12 min) | LTA's dispatch frequency per service and time band, its own timetable | `transit.headway_waits` |
| Running time between stops | 20 s dwell plus cruising at a speed rising from 22 km/h on hops of 0.4 km or less to 50 km/h on hops of 3 km or more, rescaled locally (over about 7 hops) by LTA's scheduled first and last bus times; ratios outside 0.6–1.8 are treated as data errors | The schedules are rounded to the minute and sometimes mix trips, so they only adjust a physical model | `BUS_DWELL_S`, `BUS_CRUISE_KMH`, `BUS_SCHEDULE_FACTOR_RANGE` |
| Traffic | ×1.10 AM peak, ×1.00 midday, ×1.12 PM peak, ×0.92 evening | Peak congestion | `BUS_BAND_FACTOR` |
| One service per boarding | A passenger waits for the best single service, not whichever comes first | Simplification; busy corridors look slightly slower | |

## Trains

| What | Value | Why, or source | Where |
|---|---|---|---|
| Where and when trains run | LTA's GTFS timetable for a reference weekday (6,032 trips, 426 platforms). Each trip counts in the time band it starts in; riding through a station follows platform sequences real trips run | LTA DataMall GTFS Schedule (Train) | `transit.build_rail`; **rebuild** |
| In-train time | The timetable's running time and 40 s dwells, ×0.77 EWL, ×0.94 NSL, ×0.78 NEL, ×0.80 CCL, ×0.76 DTL, ×0.85 TEL, ×1.0 LRTs | The feed rounds up to whole minutes (103 min for the whole EWL against ~80). Factors fitted against the mrt.sg station-to-station matrix | `RAIL_RUNTIME_FACTOR` |
| Wait at each boarding | From published frequencies: best 0, average half the middle of the range, worst the top | Operators' per-line figures as listed on SGWiki; LTA quotes 2–3 min at peak and 5–7 off-peak | `data/manual/train_frequencies.csv` |
| Frequencies (minutes between trains, peak / off-peak) | NSL Jurong East–Yishun 2–5 / 4–5; NSL Yishun–Marina South Pier 2–3 / 4–5; EWL Pasir Ris–Joo Koon 2–3 / 4–5; EWL Joo Koon–Tuas Link 4–6 / 8–10; Changi Airport branch 7–12 all day; NEL 2–4 / 5–6; CCL 3–5 / 5–7; DTL 3–4 / 5–6; TEL 3–5 / 5–6; Sengkang and Punggol LRT 3–4 / 5–6 | As above. The AM and PM peak bands use peak figures throughout, though operators' peaks are 07:30–09:30 and 17:30–19:30 | `data/manual/train_frequencies.csv`; `BANDS[…]["rail"]` |
| Bukit Panjang LRT waits | From the timetable's gaps: expected wait from the gaps' spread, worst the longest gap | It publishes no frequencies | `transit.gap_stats`, `transit.combined_waits` |
| Street to platform (entering or leaving) | 30 s at elevated and at-grade stations and the LRTs; underground, 75 s on the NSL and EWL, 90 s on the NEL and CCL, 120 s on the DTL and TEL; or 4 s per metre where a platform's depth is published (31 stations, from Novena at 15 m, 1 min, to Bencoolen at 43 m, 2.9 min). Scaled with walking speed (given at 4.8 km/h) | No per-station timings are published. Escalators run at 0.75 m/s at peak: up a 30° slope that's 2.7 s per metre of rise, and the walks between flights make it about 4 s. Depths from the stations' Wikipedia articles (LTA and press figures) | `data/manual/station_access.csv`, `STATION_ACCESS_S_PER_M` |
| Exits away from the platform | The station time covers exits within 50 m of the platform's centre plus the escalators' horizontal run (1.73 m per metre of depth); from farther exits the rest of the distance is walked (straight line ×1.2) | Long underpasses, the far side of an interchange | `STATION_SPAN_M`, `ESCALATOR_RUN_PER_M` |
| Changing lines | Measured leisurely walking times between platforms: 72 directional timings at 30 interchanges, 10 s (Jurong East, cross-platform) to 380 s (Tampines, EWL to DTL), ×(4.0 km/h ÷ walking speed) | Community-measured on Reddit, at 4.0 km/h | `data/manual/mrt_transfer_times.csv`, `LEISURELY_WALK_KMH` |
| Interchanges without a timing | 180 s between lines, 60 s between platforms of the same line (leisurely, so scaled the same way) | | `DEFAULT_INTERCHANGE_S`, `DEFAULT_SAME_LINE_TRANSFER_S` |

## Driving

| What | Value | Why, or source | Where |
|---|---|---|---|
| Roads | OpenStreetMap roads cars may use, with one-way rules (tags, roundabouts, expressways) | | `isochrone/osm.py`; **rebuild** |
| Speed | By road class and time band, e.g. expressways 56 / 70 / 54 / 76 km/h (AM peak / midday / PM peak / evening), trunk roads 34 / 40 / 32 / 42, residential roads 18 / 20 / 18 / 21; never above a road's tagged speed limit | Peaks calibrated to LTA's measured peak-hour averages (55 km/h on expressways, 29 km/h on arterial roads in 2025); off-peak moderately faster | `CAR_SPEEDS` |
| Parking | 0, 2 or 5 minutes added at the destination | Finding a space and walking from the car | `PARKING_OPTIONS_MIN` |
| Walking to and from the car | Barrier-aware straight walk to the 4 nearest roads within 1,000 m | | `DRIVE_SNAP_M` in `isochrone/build.py` |

## Maps and scores

| What | Value | Why, or source | Where |
|---|---|---|---|
| Grid | 250 m, 100 m or 50 m cells; a cell's time is the fastest of its links plus its walk | | `RESOLUTIONS` |
| Cut-off | 180 minutes; times are sent in tenths of a minute | | `MAX_MINUTES` |
| Land | URA Master Plan 2019 outline without the sea: 785 km², reservoirs and offshore islands included | Share-of-land figures | `data/raw/boundary` |
| Several places | Each place gets one search backwards over the reversed network; each spot's value is the weighted average of its times to the places. A spot that can't reach one of them within 180 minutes shows as unreachable | Trips *to* a place differ from trips from it (one-way roads, bus loops, where you wait) | `isochrone/engine.py`, `web/app.js` |
| Key destinations score | The weighted average time from a start point to a profile's places; a place beyond 180 minutes counts as 180 | | `data/manual/key_destinations.toml` |
| Profile map | Every spot's key-destinations score: one backwards search per place, weighted average per cell, unreachable places counted as 180 minutes. Matches the score from a start point in that cell to within about half a minute | | `Engine.profile_isochrone` |
| Profiles | General (the-fastest-journey's sample list, unchanged), and seven illustrations: office job in the CBD, office job outside it, industrial job, frequent trips to JB, sports and fitness, student, visitor. Places that aren't stations use coordinates from OpenStreetMap | | `data/manual/key_destinations.toml` |

## What the model leaves out

- **Live conditions**: no real-time arrivals, disruptions or traffic. Weekdays only.
- **Queues**: at escalators after a crowded train, at bus stops, at fare gates.
- **Walking inside buildings**: straight walks may cut through private compounds that
  aren't in the barrier set (condominium grounds, factories); the ×1.2 allowance covers
  only ordinary detours.
- **Cross-border and other services**: the Malaysian end of cross-border buses, ferries,
  the Sentosa Express and the Changi Airport Skytrain.

## Checks

- `scripts/validate_mrt.py`: platform-to-platform times against the mrt.sg matrix
  (20,306 station pairs).
- `scripts/validate_walking.py`: the walk from each HDB block to its nearest MRT exit against
  the hdb-resale-analysis project's barrier-aware distance (9,200 blocks within 1.5 km of an
  exit). The model's walk is 1.21× it at the median, which is the straight-walk allowance,
  and more than 1.5× it for 6.5% of blocks (14.8% before barrier-aware walking).
- `poetry run pytest`: unit and integration tests, including the rules above.
