# One-step launcher for Windows: sets up the Poetry environment on first run,
# builds the travel networks if needed, then serves the app and opens the browser.
#
#   powershell -ExecutionPolicy Bypass -File run.ps1              # http://127.0.0.1:8000
#   powershell -ExecutionPolicy Bypass -File run.ps1 -Port 8080 -NoBrowser
param([int]$Port = 8000, [switch]$NoBrowser)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command poetry -ErrorAction SilentlyContinue)) {
    throw "Poetry is required. Install it with 'py -3.12 -m pip install --user poetry' (or see https://python-poetry.org/docs/#installation), then re-run."
}

# First run: point Poetry at a Python 3.12-3.14 interpreter via the py launcher, if there is one.
# (Without it, Poetry searches for a compatible Python itself.)
if (-not (Test-Path ".venv") -and (Get-Command py -ErrorAction SilentlyContinue)) {
    $installed = (py -0p) -join "`n"
    foreach ($v in "3.14", "3.13", "3.12") {
        if ($installed -match "-(V:)?$([regex]::Escape($v))\b") {
            $exe = py -$v -c "import sys; print(sys.executable)"
            poetry env use $exe
            break
        }
    }
}

poetry install --no-interaction
if ($LASTEXITCODE -ne 0) { throw "poetry install failed." }

if (-not (Test-Path "data\build\meta.json")) {
    Write-Host "Building the walking, driving and transit networks (about a minute; downloads ~40 MB of OpenStreetMap data) ..."
    poetry run isochrone build
    if ($LASTEXITCODE -ne 0) { throw "Network build failed." }
}

$serveArgs = @("run", "isochrone", "serve", "--port", $Port)
if (-not $NoBrowser) { $serveArgs += "--open" }
poetry @serveArgs
