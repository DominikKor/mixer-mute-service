<#
.SYNOPSIS
    Updates the Mixer Mute Service to the newest GitHub release.

.DESCRIPTION
    Downloads the latest release of the private GitHub repository named in
    github-repo.txt, verifies its SHA-256 checksum and installs it with the
    release's own update-static.ps1 (uninstall + install; config.yaml,
    groups.json, buses.json and logs in ProgramData are preserved).

    The first interactive run asks for a GitHub token with read-only access to
    the repository's contents. It is stored encrypted for this machine (DPAPI,
    LocalMachine) in a file only Administrators and SYSTEM can read, so the
    web UI's update button (scheduled task "MixerMuteServiceUpdate", running as
    SYSTEM with -NonInteractive) can use it too. Run with -ResetToken to enter
    a new one.

    Progress is written to ProgramData\MixerMuteService\update-status.json and
    the full output to logs\update.log; the service shows both in the web UI.

.EXAMPLE
    .\update-from-github.ps1
    .\update-from-github.ps1 -Force       # reinstall even if already current
    .\update-from-github.ps1 -ResetToken  # forget the stored token first
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$ResetToken,
    # Run by the scheduled task for the web UI: never prompt, never elevate.
    [switch]$NonInteractive,
    # Set by the self-elevation below: keep the new window open at the end.
    [switch]$PauseAtEnd
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Re-launch elevated: installing the Windows service needs Administrator rights.
$Principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $NonInteractive -and -not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    $Arguments = @("-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "`"$PSCommandPath`"")
    if ($Force) { $Arguments += "-Force" }
    if ($ResetToken) { $Arguments += "-ResetToken" }
    $Arguments += "-PauseAtEnd"
    Start-Process -FilePath "powershell.exe" -ArgumentList $Arguments -Verb RunAs
    return
}

$AssetName = "MixerMuteService-Windows-x64.zip"
$Here = Split-Path -Parent $MyInvocation.MyCommand.Path
$DataDirectory = Join-Path $env:ProgramData "MixerMuteService"
$InstallDirectory = Join-Path $env:ProgramFiles "MixerMuteService"
$TokenFile = Join-Path $DataDirectory "github-token.dat"
$StatusFile = Join-Path $DataDirectory "update-status.json"
$LogDirectory = Join-Path $DataDirectory "logs"
$LogFile = Join-Path $LogDirectory "update.log"

Add-Type -AssemblyName System.Security

# ---- status for the web UI ---------------------------------------------------

$script:RunId = ""
if (Test-Path $StatusFile) {
    # The service writes {"state": "requested", "run_id": ...} before starting
    # the task; keep that run id so the web UI recognises this run's result.
    try { $script:RunId = [string](Get-Content -Path $StatusFile -Raw | ConvertFrom-Json).run_id } catch { $script:RunId = "" }
}
if (-not $NonInteractive) { $script:RunId = "manual-" + [guid]::NewGuid().ToString("N") }

function Write-Status([string]$State, [string]$Message, [string]$From = "", [string]$To = "") {
    $Status = [ordered]@{
        state      = $State
        run_id     = $script:RunId
        message    = $Message
        from       = $From
        to         = $To
        updated_at = (Get-Date).ToUniversalTime().ToString("o")
    }
    New-Item -ItemType Directory -Force -Path $DataDirectory | Out-Null
    [System.IO.File]::WriteAllText($StatusFile, ($Status | ConvertTo-Json), [System.Text.UTF8Encoding]::new($false))
}

# ---- token -------------------------------------------------------------------

function Save-Token([string]$Token) {
    $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Token)
    $Protected = [System.Security.Cryptography.ProtectedData]::Protect(
        $Bytes, $null, [System.Security.Cryptography.DataProtectionScope]::LocalMachine)
    New-Item -ItemType Directory -Force -Path $DataDirectory | Out-Null
    [System.IO.File]::WriteAllBytes($TokenFile, $Protected)
    # Only Administrators (S-1-5-32-544) and SYSTEM (S-1-5-18) may read it.
    & icacls.exe $TokenFile /inheritance:r /grant:r "*S-1-5-32-544:F" "*S-1-5-18:F" | Out-Null
}

function Read-StoredToken {
    if (-not (Test-Path $TokenFile)) { return $null }
    $Protected = [System.IO.File]::ReadAllBytes($TokenFile)
    try {
        $Bytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $Protected, $null, [System.Security.Cryptography.DataProtectionScope]::LocalMachine)
        return [System.Text.Encoding]::UTF8.GetString($Bytes)
    }
    catch { }
    try {
        # Stored per user by v1.0/v1.1: move it to machine scope.
        $Bytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $Protected, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        $Token = [System.Text.Encoding]::UTF8.GetString($Bytes)
        Save-Token $Token
        return $Token
    }
    catch {
        return $null  # damaged or from another user: ask again
    }
}

function Request-Token([string]$Repo) {
    if ($NonInteractive) {
        throw "No GitHub token stored yet. Double-click 'Mixer Service aktualisieren' on this PC once and enter it."
    }
    Write-Host ""
    Write-Host "A GitHub token with read access to $Repo is needed (asked only once)." -ForegroundColor Yellow
    Write-Host "Create it at https://github.com/settings/personal-access-tokens/new :"
    Write-Host "  Repository access: Only select repositories -> $Repo"
    Write-Host "  Permissions: Contents = Read-only"
    $Secure = Read-Host -Prompt "Paste the token" -AsSecureString
    $Bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($Secure)
    try { $Token = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($Bstr) }
    finally { [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($Bstr) }
    if ([string]::IsNullOrWhiteSpace($Token)) { throw "No token entered." }
    return $Token.Trim()
}

function Get-HttpStatus($ErrorRecord) {
    $Response = $ErrorRecord.Exception.PSObject.Properties["Response"]
    if ($null -eq $Response -or $null -eq $Response.Value) { return 0 }
    try { return [int]$Response.Value.StatusCode } catch { return 0 }
}

# ---- update ------------------------------------------------------------------

function Invoke-Update {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    $RepoFile = Join-Path $Here "github-repo.txt"
    if (-not (Test-Path $RepoFile)) {
        throw "Missing $RepoFile (it is written by the GitHub release build)."
    }
    $Repo = (Get-Content -Path $RepoFile -Raw).Trim()

    $InstalledTagFile = Join-Path $InstallDirectory "release-tag.txt"
    $InstalledTag = if (Test-Path $InstalledTagFile) { (Get-Content -Path $InstalledTagFile -Raw).Trim() } else { "" }
    Write-Status "running" "Checking for a new version..." $InstalledTag

    if ($ResetToken -and (Test-Path $TokenFile)) { Remove-Item -Force $TokenFile }
    $Token = Read-StoredToken
    $NewToken = $false
    if ($null -eq $Token) { $Token = Request-Token $Repo; $NewToken = $true }

    $Headers = @{
        "Authorization"        = "Bearer $Token"
        "Accept"               = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
        "User-Agent"           = "MixerMuteService-updater"
    }

    Write-Host "Checking the latest release of $Repo..." -ForegroundColor Cyan
    try {
        $Release = Invoke-RestMethod -Uri "https://api.github.com/repos/$Repo/releases/latest" -Headers $Headers
    }
    catch {
        $Code = Get-HttpStatus $_
        if ($Code -in 401, 403, 404) {
            if (Test-Path $TokenFile) { Remove-Item -Force $TokenFile }
            throw "GitHub refused the token (HTTP $Code): wrong, expired or without read access to $Repo. It was forgotten; double-click 'Mixer Service aktualisieren' to enter a new one."
        }
        throw "GitHub is not reachable ($($_.Exception.Message)). Check the internet connection and try again."
    }
    if ($NewToken) { Save-Token $Token }

    $Tag = $Release.tag_name
    Write-Host "  installed: $(if ($InstalledTag) { $InstalledTag } else { 'unknown' })"
    Write-Host "  latest:    $Tag"
    if ($InstalledTag -eq $Tag -and -not $Force) {
        Write-Host "Already up to date." -ForegroundColor Green
        Write-Status "up_to_date" "Already up to date ($Tag)." $InstalledTag $Tag
        return
    }

    $Zip = $Release.assets | Where-Object { $_.name -eq $AssetName } | Select-Object -First 1
    $Sum = $Release.assets | Where-Object { $_.name -eq "$AssetName.sha256" } | Select-Object -First 1
    if ($null -eq $Zip -or $null -eq $Sum) {
        throw "Release $Tag has no $AssetName with checksum; was the build successful?"
    }

    $Work = Join-Path $env:TEMP ("MixerMuteService-update-" + [guid]::NewGuid().ToString("N"))
    New-Item -ItemType Directory -Force -Path $Work | Out-Null
    try {
        $DownloadHeaders = $Headers.Clone()
        $DownloadHeaders["Accept"] = "application/octet-stream"
        $ZipPath = Join-Path $Work $AssetName
        $SumPath = "$ZipPath.sha256"
        Write-Status "running" "Downloading $Tag..." $InstalledTag $Tag
        Write-Host "Downloading $AssetName ($Tag)..."
        Invoke-WebRequest -UseBasicParsing -Uri $Zip.url -Headers $DownloadHeaders -OutFile $ZipPath
        Invoke-WebRequest -UseBasicParsing -Uri $Sum.url -Headers $DownloadHeaders -OutFile $SumPath

        $Expected = (Get-Content -Path $SumPath -Raw).Trim().ToLowerInvariant()
        $Actual = (Get-FileHash -Algorithm SHA256 $ZipPath).Hash.ToLowerInvariant()
        if ($Expected -ne $Actual) {
            throw "Checksum mismatch: expected $Expected, got $Actual. Nothing was installed."
        }
        Write-Host "Checksum OK."

        Expand-Archive -Path $ZipPath -DestinationPath $Work -Force
        $Updater = Join-Path $Work "MixerMuteService-Windows-x64\update-static.ps1"
        if (-not (Test-Path $Updater)) { throw "update-static.ps1 not found in the release." }
        Write-Status "running" "Installing $Tag (the service restarts)..." $InstalledTag $Tag
        & $Updater
    }
    finally {
        Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
    }

    Write-Host ""
    Write-Host "Updated to $Tag." -ForegroundColor Green
    Write-Status "updated" "Updated from $(if ($InstalledTag) { $InstalledTag } else { 'unknown' }) to $Tag." $InstalledTag $Tag
}

New-Item -ItemType Directory -Force -Path $LogDirectory | Out-Null
$Transcript = $false
try { Start-Transcript -Path $LogFile -Force | Out-Null; $Transcript = $true } catch { }
try {
    Invoke-Update
}
catch {
    Write-Host ""
    Write-Host "Update failed: $($_.Exception.Message)" -ForegroundColor Red
    try { Write-Status "failed" $_.Exception.Message } catch { }
    $global:LASTEXITCODE = 1
}
finally {
    if ($Transcript) { try { Stop-Transcript | Out-Null } catch { } }
    if ($PauseAtEnd) { Read-Host "Press Enter to close" | Out-Null }
}
