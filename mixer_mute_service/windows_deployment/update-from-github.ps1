<#
.SYNOPSIS
    Updates the Mixer Mute Service to the newest GitHub release.

.DESCRIPTION
    Downloads the latest release of the private GitHub repository named in
    github-repo.txt, verifies its SHA-256 checksum and installs it with the
    release's own update-static.ps1 (uninstall + install; config.yaml,
    groups.json, buses.json and logs in ProgramData are preserved).

    The first run asks for a GitHub token with read-only access to the
    repository's contents. It is stored encrypted for the current Windows user
    (DPAPI) in ProgramData and reused afterwards. Run with -ResetToken to enter
    a new one.

.EXAMPLE
    .\update-from-github.ps1
    .\update-from-github.ps1 -Force       # reinstall even if already current
    .\update-from-github.ps1 -ResetToken  # forget the stored token first
#>
[CmdletBinding()]
param(
    [switch]$Force,
    [switch]$ResetToken,
    # Set by the self-elevation below: keep the new window open at the end.
    [switch]$PauseAtEnd
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Re-launch elevated: installing the Windows service needs Administrator rights.
$Principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
if (-not $Principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
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

$RepoFile = Join-Path $Here "github-repo.txt"
if (-not (Test-Path $RepoFile)) {
    throw "Missing $RepoFile (it is written by the GitHub release build)."
}
$Repo = (Get-Content -Path $RepoFile -Raw).Trim()

Add-Type -AssemblyName System.Security

function Save-Token([string]$Token) {
    $Bytes = [System.Text.Encoding]::UTF8.GetBytes($Token)
    $Protected = [System.Security.Cryptography.ProtectedData]::Protect(
        $Bytes, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
    New-Item -ItemType Directory -Force -Path $DataDirectory | Out-Null
    [System.IO.File]::WriteAllBytes($TokenFile, $Protected)
}

function Read-StoredToken {
    if (-not (Test-Path $TokenFile)) { return $null }
    try {
        $Protected = [System.IO.File]::ReadAllBytes($TokenFile)
        $Bytes = [System.Security.Cryptography.ProtectedData]::Unprotect(
            $Protected, $null, [System.Security.Cryptography.DataProtectionScope]::CurrentUser)
        return [System.Text.Encoding]::UTF8.GetString($Bytes)
    }
    catch {
        # Stored by another Windows user or damaged: ask again.
        return $null
    }
}

function Request-Token {
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

function Invoke-Update {
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

    if ($ResetToken -and (Test-Path $TokenFile)) { Remove-Item -Force $TokenFile }
    $Token = Read-StoredToken
    $NewToken = $false
    if ($null -eq $Token) { $Token = Request-Token; $NewToken = $true }

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
        if (Test-Path $TokenFile) { Remove-Item -Force $TokenFile }
        throw "GitHub refused the request ($($_.Exception.Message)). The token is wrong, expired or lacks read access to $Repo; it was forgotten, run the updater again to enter a new one."
    }
    if ($NewToken) { Save-Token $Token }

    $Tag = $Release.tag_name
    $InstalledTagFile = Join-Path $InstallDirectory "release-tag.txt"
    $InstalledTag = if (Test-Path $InstalledTagFile) { (Get-Content -Path $InstalledTagFile -Raw).Trim() } else { "" }
    Write-Host "  installed: $(if ($InstalledTag) { $InstalledTag } else { 'unknown' })"
    Write-Host "  latest:    $Tag"
    if ($InstalledTag -eq $Tag -and -not $Force) {
        Write-Host "Already up to date." -ForegroundColor Green
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
        & $Updater
    }
    finally {
        Remove-Item -Recurse -Force $Work -ErrorAction SilentlyContinue
    }

    Write-Host ""
    Write-Host "Updated to $Tag." -ForegroundColor Green
}

try {
    Invoke-Update
}
catch {
    Write-Host ""
    Write-Host "Update failed: $($_.Exception.Message)" -ForegroundColor Red
    $global:LASTEXITCODE = 1
}
finally {
    if ($PauseAtEnd) { Read-Host "Press Enter to close" | Out-Null }
}
