#!/usr/bin/env bash
# One-step launcher for macOS (and Linux): sets up the Poetry environment on first
# run, builds the travel networks if needed, then serves the app and opens the browser.
#
#   ./run.sh                          # http://127.0.0.1:8000
#   ./run.sh --port 8080 --no-browser
set -euo pipefail
cd "$(dirname "$0")"

PORT=8000
OPEN="--open"
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --port=*) PORT="${1#*=}"; shift ;;
    --no-browser) OPEN=""; shift ;;
    -h|--help) sed -n '2,6p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

if ! command -v poetry >/dev/null 2>&1; then
  cat >&2 <<'MSG'
Poetry is required. Install it with one of:
  brew install poetry
  pipx install poetry
  curl -sSL https://install.python-poetry.org | python3 -
then re-run ./run.sh
MSG
  exit 1
fi

# First run: point Poetry at a Python 3.12-3.14 interpreter. macOS's built-in
# /usr/bin/python3 is too old; Homebrew (brew install python@3.13) or python.org work.
if [ ! -d .venv ]; then
  found=""
  for cand in "${PYTHON:-}" python3.14 python3.13 python3.12 python3; do
    [ -n "$cand" ] || continue
    if command -v "$cand" >/dev/null 2>&1 &&
       "$cand" -c 'import sys; sys.exit(0 if (3, 12) <= sys.version_info[:2] < (3, 15) else 1)' >/dev/null 2>&1; then
      found="$(command -v "$cand")"
      break
    fi
  done
  if [ -z "$found" ]; then
    echo "Python 3.12, 3.13 or 3.14 is required (e.g. brew install python@3.13)." >&2
    exit 1
  fi
  poetry env use "$found"
fi

poetry install --no-interaction

if [ ! -f data/build/meta.json ]; then
  echo "Building the walking, driving and transit networks (about a minute; downloads ~40 MB of OpenStreetMap data) ..."
  poetry run isochrone build
fi

# shellcheck disable=SC2086  # $OPEN is deliberately empty or a single flag
exec poetry run isochrone serve --port "$PORT" $OPEN
