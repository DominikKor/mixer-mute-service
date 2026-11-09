#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Updates the installed Mixer Mute Service.

.DESCRIPTION
    Executes exactly the same flow as manually running:
      1) uninstall-service.ps1
      2) install-service.ps1
    This ensures update behavior stays identical to uninstall+install.

    Before that, the data files in C:\ProgramData\MixerMuteService
    (*.json, *.yaml, *.yml) are saved to backups\<release>-<time>.zip there,
    because the new version may migrate them on its first start. The newest
    10 backups are kept.

.EXAMPLE
    .\update-static.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$InstallDirectory = Join-Path $env:ProgramFiles "MixerMuteService"
$ProgramDataDirectory = Join-Path $env:ProgramData "MixerMuteService"
$BackupDirectory = Join-Path $ProgramDataDirectory "backups"
$BackupsToKeep = 10
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

# ---- backup of the data files ---------------------------------------------------

$DataFiles = @()
if (Test-Path $ProgramDataDirectory) {
    $DataFiles = @(Get-ChildItem -Path $ProgramDataDirectory -File |
        Where-Object { $_.Extension -in @(".json", ".yaml", ".yml") })
}
if ($DataFiles.Count -gt 0) {
    # A failed backup must not block the update itself: warn and go on.
    try {
        # Name after the release being replaced (or its build id), then the time.
        $Previous = "unknown"
        foreach ($Candidate in @((Join-Path $InstallDirectory "release-tag.txt"), (Join-Path $InstallDirectory "build-id.txt"))) {
            if (Test-Path $Candidate) {
                $Previous = (Get-Content -Path $Candidate -Raw).Trim()
                break
            }
        }
        $Previous = $Previous -replace '[^A-Za-z0-9._-]', '_'
        $Stamp = Get-Date -Format "yyyyMMdd-HHmmss"
        New-Item -ItemType Directory -Force -Path $BackupDirectory | Out-Null
        $BackupPath = Join-Path $BackupDirectory ("{0}-{1}.zip" -f $Previous, $Stamp)
        Compress-Archive -Path ($DataFiles | ForEach-Object { $_.FullName }) -DestinationPath $BackupPath -CompressionLevel Optimal
        Write-Host "Backup of $($DataFiles.Count) data files: $BackupPath"

        $Old = @(Get-ChildItem -Path $BackupDirectory -Filter "*.zip" -File | Sort-Object LastWriteTime -Descending | Select-Object -Skip $BackupsToKeep)
        foreach ($File in $Old) {
            Remove-Item -Force $File.FullName
            Write-Host "Removed old backup: $($File.Name)"
        }
    }
    catch {
        Write-Warning "Backup of the data files failed ($($_.Exception.Message)); updating anyway."
    }
}
else {
    Write-Host "No data files to back up yet."
}

# ---- uninstall + install --------------------------------------------------------

Write-Host "Step 1/2: Running uninstall-service.ps1"
try {
    & $UninstallScript
}
catch {
    throw "uninstall-service.ps1 failed: $($_.Exception.Message)"
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

Write-Host "Step 2/2: Running install-service.ps1"
try {
    & $InstallScript
}
catch {
    throw "install-service.ps1 failed: $($_.Exception.Message)"
}

# ---- verification ------------------------------------------------------------------

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
