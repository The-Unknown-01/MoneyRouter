@echo off
setlocal
cd /d "%~dp0"
where pwsh.exe >nul 2>nul
if errorlevel 1 (
  powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-web.ps1" %*
) else (
  pwsh.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-web.ps1" %*
)
if errorlevel 1 (
  echo.
  echo Startup failed. Check the message above and agent-service-error.log.
  pause
  exit /b 1
)
