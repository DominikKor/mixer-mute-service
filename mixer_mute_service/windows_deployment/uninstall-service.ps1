#Requires -RunAsAdministrator
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$ServiceName = "MixerMuteService"
$InstallDirectory = Join-Path $env:ProgramFiles "MixerMuteService"
$Wrapper = Join-Path $InstallDirectory "MixerMuteServiceService.exe"
$Service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if ($null -eq $Service) {
    Write-Host "Mixer Mute Service is not installed."
    exit 0
}

if ($Service.Status -ne "Stopped") {
    Stop-Service -Name $ServiceName -Force
    (Get-Service -Name $ServiceName).WaitForStatus("Stopped", [TimeSpan]::FromSeconds(20))
}

if (-not (Test-Path $Wrapper)) {
    throw "Cannot unregister the service because its wrapper is missing: $Wrapper"
}

& $Wrapper uninstall
if ($LASTEXITCODE -ne 0) {
    throw "WinSW could not unregister the service."
}

Write-Host "Mixer Mute Service was unregistered." -ForegroundColor Green
Write-Host "Program files and C:\ProgramData\MixerMuteService were preserved deliberately."

