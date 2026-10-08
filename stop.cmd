@echo off
setlocal
cd /d "%~dp0"
where pwsh.exe >nul 2>nul
if errorlevel 1 (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-web.ps1" %*
) else (
  pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\stop-web.ps1" %*
)
if errorlevel 1 (
  echo.
  echo Stop incomplete. See the messages above.
) else (
  echo.
  echo Leftover services stopped. Any open start window will exit by itself.
)
pause
