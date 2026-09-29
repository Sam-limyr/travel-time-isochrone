# Manually curated data

## `mrt_transfer_times.csv`

Directional in-station walking times (seconds) between the platforms of MRT/LRT
interchanges, measured at a **leisurely** pace by a Reddit user. Columns:

| Column | Meaning |
|---|---|
| `Station Name` | Interchange name |
| `Start Line` / `Start Code` | Line and station code you alight at |
| `End Line` / `End Code` | Line and station code you walk to |
| `Transfer time in seconds` | Leisurely walking time between the two platforms |

The router scales these times by the walking-speed slider (see
`isochrone/config.py: LEISURELY_WALK_KMH`), so a brisker walker transfers faster.
Interchanges missing from this file fall back to a default transfer time.

## `train_frequencies.csv`

Published train frequencies, the source of train waiting times: minutes between trains
at peak (the AM and PM peak bands) and off-peak (midday and evening), per line or line
section. Collected in September 2026 from the per-line pages of SGWiki
(sgwiki.miraheze.org), which list the operators' frequencies with peak hours 07:30–09:30
and 17:30–19:30; they agree with LTA's network-wide "2 to 3 minutes during the peak
hours of 7am to 9am and about 5 to 7 minutes during off-peak times".

| Column | Meaning |
|---|---|
| `line`, `section` | Label (shown in the app's About panel) |
| `stations` | Station codes the row covers: ranges such as `EW29-EW33`, or prefixes such as `CG` (all codes starting with it), separated by spaces |
| `peak_min`, `offpeak_min` | Headway range in minutes, e.g. `2-3` |
| `source` | Where the figures come from |

A track segment takes the first row that covers both of its stations, so sections that
share an end station (Joo Koon, Yishun) are split by row order. A wait is then derived
like a bus's: half the middle of the range on average, the top of the range at worst.
Segments with no row (the Bukit Panjang LRT, which publishes no frequencies) keep waits
from the GTFS timetable's gaps. Restart the server after editing.

## `key_destinations.csv`

The weighted places behind the **Key destinations** score: the app reports the
weighted average travel time from the start point to them. The sample rows are
MRT stations with the general-purpose destination weights from the
*the-fastest-journey* project (`mrt_distance.py`, `get_weights()`), which sum to 100.

| Column | Meaning |
|---|---|
| `group` | Heading the row is summarised under (e.g. `Work: CBD`) |
| `name` | Place name; with no coordinates, the MRT/LRT station of that name |
| `weight` | Relative importance; any scale, shown as shares of the total |
| `lon`, `lat` | Optional coordinates, for places that aren't stations |

A station that appears in two groups (Orchard is both a CBD workplace and a
shopping area) simply has two rows, and its weights add up. Names that match no
station are skipped, and the app notes them. Restart the server after editing.
