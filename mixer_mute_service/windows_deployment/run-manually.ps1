[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$InstallDirectory = Join-Path $env:ProgramFiles "MixerMuteService"
$Executable = Join-Path $InstallDirectory "MixerMuteService.exe"
$env:MIXER_CONFIG = Join-Path $env:ProgramData "MixerMuteService\config.yaml"
$env:MIXER_LOG_LEVEL = "DEBUG"

if (-not (Test-Path $Executable)) {
    throw "Installed executable not found: $Executable"
}

$Service = Get-Service -Name "MixerMuteService" -ErrorAction SilentlyContinue
if ($null -ne $Service -and $Service.Status -ne "Stopped") {
    throw "Stop the MixerMuteService Windows service before running a manual debug instance."
}

Write-Host "Starting in the foreground with DEBUG logging. Press Ctrl+C to stop."
& $Executable

