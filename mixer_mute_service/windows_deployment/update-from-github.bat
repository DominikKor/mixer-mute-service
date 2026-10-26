@echo off
setlocal

REM Double-click to update the Mixer Mute Service to the newest GitHub release.
REM The script asks for Administrator rights itself.
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0update-from-github.ps1" %*
echo.
pause
