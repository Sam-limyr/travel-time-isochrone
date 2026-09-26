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
