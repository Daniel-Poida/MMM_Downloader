@echo off
setlocal
cd /d "%~dp0"
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\bootstrap_windows.ps1"
if errorlevel 1 (
  echo.
  echo MMM Downloader could not start. See the error above.
  pause
)
