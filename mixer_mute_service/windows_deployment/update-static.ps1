#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Updates the installed Mixer Mute Service.

.DESCRIPTION
    Executes exactly the same flow as manually running:
      1) uninstall-service.ps1
      2) install-service.ps1
    This ensures update behavior stays identical to uninstall+install.

.EXAMPLE
    .\update-static.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ServiceName = "MixerMuteService"
$InstallDirectory = Join-Path $env:ProgramFiles "MixerMuteService"
$ProgramDataDirectory = Join-Path $env:ProgramData "MixerMuteService"
$SourceDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$UninstallScript = Join-Path $SourceDir "uninstall-service.ps1"
$InstallScript = Join-Path $SourceDir "install-service.ps1"
$ExpectedBuildId = ""
$BuildIdFile = Join-Path $SourceDir "build-id.txt"

if (Test-Path $BuildIdFile) {
    $ExpectedBuildId = (Get-Content -Path $BuildIdFile -Raw).Trim()
}

if (-not (Test-Path $UninstallScript)) {
    throw "Missing script: $UninstallScript"
}
if (-not (Test-Path $InstallScript)) {
    throw "Missing script: $InstallScript"
}

Write-Host "Updating Mixer Mute Service (uninstall + install)..." -ForegroundColor Cyan

Write-Host "Step 1/2: Running uninstall-service.ps1"
& $UninstallScript
if ($LASTEXITCODE -ne 0) {
    throw "uninstall-service.ps1 failed with exit code $LASTEXITCODE"
}

Write-Host "Cleanup: removing stale installation directory"
if (Test-Path $InstallDirectory) {
    try {
        Remove-Item -Path $InstallDirectory -Recurse -Force
    }
    catch {
        throw "Could not remove $InstallDirectory. Close any process using files in this folder and retry."
    }
}

# Ensure no stale service process is left behind after uninstall.
$Service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($null -ne $Service -and $Service.Status -ne "Stopped") {
    Stop-Service -Name $ServiceName -Force
}

Write-Host "Step 2/2: Running install-service.ps1"
& $InstallScript
if ($LASTEXITCODE -ne 0) {
    throw "install-service.ps1 failed with exit code $LASTEXITCODE"
}

Write-Host "Verification: checking /health and /groups"
$Health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 5
$Groups = Invoke-RestMethod -Uri "http://127.0.0.1:8765/groups" -TimeoutSec 5

Write-Host "  build_id:   $($Health.build_id)"
Write-Host "  group_count:$($Health.group_count)"
Write-Host "  enabled:    $($Health.enabled)"
Write-Host "  connected:  $($Health.mixer_connected)"

if (-not [string]::IsNullOrWhiteSpace($ExpectedBuildId)) {
    if ($Health.build_id -ne $ExpectedBuildId) {
        throw "Running build_id '$($Health.build_id)' does not match package build-id '$ExpectedBuildId'."
    }
}

$GroupNames = @($Groups.PSObject.Properties.Name)
Write-Host "  groups:     $($GroupNames -join ', ')"

if ($Health.group_count -eq 0 -and $GroupNames.Count -eq 0) {
    $ConfigPath = Join-Path $ProgramDataDirectory "config.yaml"
    $GroupsPath = Join-Path $ProgramDataDirectory "groups.json"
    Write-Warning "No groups are currently available. Check $ConfigPath and $GroupsPath."
}

Write-Host ""
Write-Host "Update complete (same behavior as uninstall + install)." -ForegroundColor Green
