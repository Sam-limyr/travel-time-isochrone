# One-step launcher for Windows: sets up the Python environment on first run,
# builds the travel networks if needed, then serves the app and opens the browser.
#
#   powershell -ExecutionPolicy Bypass -File run.ps1          # http://127.0.0.1:8000
#   powershell -ExecutionPolicy Bypass -File run.ps1 -Port 8080 -NoBrowser
param([int]$Port = 8000, [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $python)) {
    Write-Host "Creating virtual environment (.venv) with Python 3.12 ..."
    py -3.12 -m venv .venv
    if (-not $?) { throw "Python 3.12 is required (install it from python.org, then re-run)." }
    & $python -m pip install -q --upgrade pip
}
& $python -m pip install -q -r requirements.txt
if (-not $?) { throw "Installing Python packages failed." }

if (-not (Test-Path "data\build\meta.json")) {
    Write-Host "Building the walking, driving and transit networks (about a minute; downloads ~40 MB of OpenStreetMap data) ..."
    & $python -m isochrone build
    if (-not $?) { throw "Network build failed." }
}

$serveArgs = @("-m", "isochrone", "serve", "--port", $Port)
if (-not $NoBrowser) { $serveArgs += "--open" }
& $python @serveArgs
