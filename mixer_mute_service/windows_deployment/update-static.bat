@echo off
setlocal

REM Run the PowerShell updater from this folder. Start this .bat as Administrator.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-static.ps1" %*
set "EXITCODE=%ERRORLEVEL%"

if not "%EXITCODE%"=="0" (
  echo.
  echo Update failed with exit code %EXITCODE%.
  pause
)

exit /b %EXITCODE%
