@echo off
rem Double-click launcher: runs run.ps1 (setup on first run, then serves the app).
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0run.ps1" %*
