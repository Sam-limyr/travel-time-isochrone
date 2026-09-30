#!/usr/bin/env bash
# One-step launcher for macOS, Linux and Windows (Git Bash): sets up the Poetry
# environment on first run, builds the travel networks if needed, then serves the
# app and opens the browser.
#
#   ./run.sh                          # http://127.0.0.1:8000
#   ./run.sh --port 8080 --no-browser
set -euo pipefail

case "$(uname -s)" in
  Darwin) os=mac ;;
  MINGW* | MSYS* | CYGWIN*) os=windows ;;
  *) os=linux ;;
esac

# Double-clicking run.sh in Explorer passes a Windows path (C:\...).
script=$0
case "$script" in *\\*) script=$(cygpath -u "$script") ;; esac
cd "$(dirname "$script")"

# A double-clicked run.sh gets a window of its own that closes when the script
# ends; on failure (not Ctrl-C or a closed window), keep it open so the message can be read.
pause_on_failure() {
  status=$?
  case "$status" in 0 | 129 | 130 | 143) return ;; esac
  if [ "${SHLVL:-1}" -le 1 ] && [ -t 0 ]; then
    printf '\nPress Enter to close this window.' >&2
    read -r _ || true
  fi
}
trap pause_on_failure EXIT

# In PowerShell, `bash` is WSL's bash when WSL is installed. It is Linux and
# would set up a Linux .venv over the Windows one in this folder.
if [ "$os" = linux ] && grep -qi microsoft /proc/version 2>/dev/null; then
  case "$PWD" in
    /mnt/[a-z]/*)
      echo "This is WSL's bash, running on a Windows folder. Use Git Bash instead; from PowerShell:" >&2
      echo '  & "C:\Program Files\Git\bin\bash.exe" run.sh' >&2
      echo "(To run the app inside WSL, clone the project into WSL's own file system.)" >&2
      exit 1 ;;
  esac
fi

port=8000
open=--open
while [ $# -gt 0 ]; do
  case "$1" in
    --port) [ $# -ge 2 ] || { echo "--port needs a number" >&2; exit 2; }; port=$2; shift 2 ;;
    --port=*) port=${1#*=}; shift ;;
    --no-browser) open=""; shift ;;
    -h | --help) sed -n '2,7p' "$(basename "$script")"; exit 0 ;;
    *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
  esac
done

# Stream progress as it happens: Git Bash's terminal is a pipe to Windows programs,
# so Python would otherwise buffer its output. UTF-8 keeps any non-ASCII text intact there.
export PYTHONUNBUFFERED=1
if [ "$os" = windows ]; then export PYTHONUTF8=1; fi

if ! command -v poetry >/dev/null 2>&1; then
  echo "Poetry 2.x is required (https://python-poetry.org/docs/#installation). Install it with:" >&2
  case "$os" in
    mac) echo "  brew install poetry" >&2 ;;
    windows) echo "  curl -sSL https://install.python-poetry.org | py -     (then add Poetry to PATH as it says)" >&2 ;;
    *) echo "  pipx install poetry" >&2 ;;
  esac
  echo "then run ./run.sh again." >&2
  exit 1
fi

# First run: have Poetry create .venv with the newest Python 3.12-3.14 it can find
# (it searches PATH, the Windows registry, pyenv and uv). The default python3 is
# often too old: 3.9 on macOS, whatever is first on PATH on Windows.
if [ ! -d .venv ]; then
  if [ -n "${PYTHON:-}" ]; then
    poetry env use "$PYTHON"
  elif ! poetry env use 3.14 2>/dev/null && ! poetry env use 3.13 2>/dev/null && ! poetry env use 3.12; then
    echo "Python 3.12, 3.13 or 3.14 is required. Install it with:" >&2
    case "$os" in
      mac) echo "  brew install python@3.13        (or the installer from python.org)" >&2 ;;
      windows) echo "  winget install Python.Python.3.13        (or the installer from python.org)" >&2 ;;
      *) echo "  your package manager, pyenv or uv" >&2 ;;
    esac
    echo "or point to one: PYTHON=/path/to/python3.13 ./run.sh" >&2
    exit 1
  fi
fi

poetry install --no-interaction

# Run the app with the environment's own Python rather than `poetry run`: on Windows
# that goes through cmd.exe, and closing the terminal would leave the server running.
python=$(poetry env info --executable | tr -d '\r')
if [ "$os" = windows ]; then python=$(cygpath -u "$python"); fi

# Builds the walking, driving and transit networks the first time (about two minutes, and
# a ~40 MB OpenStreetMap download), and again after an update changes their format.
"$python" -m isochrone build --if-needed

# Ctrl-C or closing the terminal must stop the server too. The server runs in the
# background so these signals reach this script; on Windows it is a native process
# that bash's signals can't reach, so its whole process tree is ended instead.
server=""
stop_server() {
  [ -n "$server" ] || return 0
  if [ "$os" = windows ]; then
    taskkill //F //T //PID "$(cat "/proc/$server/winpid")" >/dev/null 2>&1 || true
  else
    kill "$server" 2>/dev/null || true
    wait "$server" 2>/dev/null || true  # let uvicorn finish shutting down before the prompt returns
  fi
}
trap 'stop_server; exit 129' HUP
trap 'stop_server; exit 130' INT
trap 'stop_server; exit 143' TERM

# shellcheck disable=SC2086  # $open is deliberately empty or a single flag
"$python" -m isochrone serve --port "$port" $open &
server=$!
wait "$server"
