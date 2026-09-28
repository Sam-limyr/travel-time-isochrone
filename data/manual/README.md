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
