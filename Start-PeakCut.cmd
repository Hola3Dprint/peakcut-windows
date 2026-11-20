@echo off
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0Start-PeakCut.ps1"
if errorlevel 1 (
    pause
    exit /b 1
)
if /i "%~1"=="dashboard" start "" "http://127.0.0.1:8765/app"
