[CmdletBinding()]
param(
    [string]$PythonCommand = "py",
    [string]$OutputDirectory = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$DeploymentDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$ProjectDirectory = Split-Path -Parent $DeploymentDirectory

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $DeploymentDirectory "release"
}

$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
$BuildDirectory = Join-Path $DeploymentDirectory "build"
$DistDirectory = Join-Path $DeploymentDirectory "dist"
$VirtualEnvironment = Join-Path $DeploymentDirectory ".build-venv"
$ReleaseName = "MixerMuteService-Windows-x64"
$ReleaseDirectory = Join-Path $OutputDirectory $ReleaseName
$ArchivePath = Join-Path $OutputDirectory ($ReleaseName + ".zip")
$WinSwVersion = "v2.12.0"
$WinSwUrl = "https://github.com/winsw/winsw/releases/download/$WinSwVersion/WinSW-x64.exe"
$WinSwSha256 = "05b82d46ad331cc16bdc00de5c6332c1ef818df8ceefcd49c726553209b3a0da"
$BuildTimestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$GitShort = ""
try {
    $GitShort = (& git -C $ProjectDirectory rev-parse --short HEAD 2>$null).Trim()
}
catch {
    $GitShort = ""
}
if ([string]::IsNullOrWhiteSpace($GitShort)) {
    $GitShort = "nogit"
}
$BuildId = "$BuildTimestamp-$GitShort"

Write-Host "Building Mixer Mute Service Windows release..." -ForegroundColor Cyan
Write-Host "Build ID: $BuildId"

if (-not (Test-Path $VirtualEnvironment)) {
    Write-Host "Creating the isolated build environment..."
    if ($PythonCommand -eq "py") {
        & $PythonCommand -3.14 -m venv $VirtualEnvironment
    }
    else {
        & $PythonCommand -m venv $VirtualEnvironment
    }
    if ($LASTEXITCODE -ne 0) {
        throw "Could not create the build environment. Install 64-bit Python 3.14 and try again."
    }
}

$BuildPython = Join-Path $VirtualEnvironment "Scripts\python.exe"
if (-not (Test-Path $BuildPython)) {
    throw "Build Python was not found at $BuildPython"
}

Write-Host "Installing build dependencies into the isolated environment..."
& $BuildPython -m pip install --disable-pip-version-check -r (Join-Path $ProjectDirectory "requirements.txt") -r (Join-Path $DeploymentDirectory "requirements-build.txt")
if ($LASTEXITCODE -ne 0) {
    throw "Dependency installation failed."
}

New-Item -ItemType Directory -Force -Path $OutputDirectory | Out-Null

foreach ($Path in @($BuildDirectory, $DistDirectory, $ReleaseDirectory, $ArchivePath)) {
    if (Test-Path $Path) {
        Remove-Item -Recurse -Force $Path
    }
}

Push-Location $ProjectDirectory
try {
    & $BuildPython -m PyInstaller --noconfirm --clean --workpath $BuildDirectory --distpath $DistDirectory (Join-Path $DeploymentDirectory "MixerMuteService.spec")
    if ($LASTEXITCODE -ne 0) {
        throw "PyInstaller failed."
    }
}
finally {
    Pop-Location
}

$BuiltApplication = Join-Path $DistDirectory "MixerMuteService"
if (-not (Test-Path (Join-Path $BuiltApplication "MixerMuteService.exe"))) {
    throw "The expected executable was not produced."
}

New-Item -ItemType Directory -Force -Path $ReleaseDirectory | Out-Null
Copy-Item -Path (Join-Path $BuiltApplication "*") -Destination $ReleaseDirectory -Recurse -Force

Write-Host "Downloading the pinned WinSW $WinSwVersion service wrapper..."
$WinSwDestination = Join-Path $ReleaseDirectory "MixerMuteServiceService.exe"
Invoke-WebRequest -UseBasicParsing -Uri $WinSwUrl -OutFile $WinSwDestination
$DownloadedSha256 = (Get-FileHash -Algorithm SHA256 $WinSwDestination).Hash.ToLowerInvariant()
if ($DownloadedSha256 -ne $WinSwSha256) {
    Remove-Item -Force $WinSwDestination
    throw "WinSW checksum mismatch. Expected $WinSwSha256 but downloaded $DownloadedSha256."
}

$FilesToCopy = @(
    "MixerMuteServiceService.xml",
    "install-service.ps1",
    "uninstall-service.ps1",
    "service-status.ps1",
    "run-manually.ps1",
    "update-static.ps1",
    "update-static.bat",
    "update-from-github.ps1",
    "update-from-github.bat",
    "DEPLOYMENT-README.md"
)
foreach ($File in $FilesToCopy) {
    Copy-Item (Join-Path $DeploymentDirectory $File) $ReleaseDirectory -Force
}

Copy-Item (Join-Path $ProjectDirectory "config.yaml") (Join-Path $ReleaseDirectory "config-template.yaml") -Force
Copy-Item (Join-Path $ProjectDirectory "groups.json") (Join-Path $ReleaseDirectory "groups-template.json") -Force
[System.IO.File]::WriteAllText((Join-Path $ReleaseDirectory "build-id.txt"), $BuildId + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))

# Built by GitHub Actions: tell the updater where releases come from and which
# release this is (local builds leave these out; the updater then refuses).
if (-not [string]::IsNullOrWhiteSpace($env:GITHUB_REPOSITORY)) {
    [System.IO.File]::WriteAllText((Join-Path $ReleaseDirectory "github-repo.txt"), $env:GITHUB_REPOSITORY + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
}
if ($env:GITHUB_REF_TYPE -eq "tag" -and -not [string]::IsNullOrWhiteSpace($env:GITHUB_REF_NAME)) {
    [System.IO.File]::WriteAllText((Join-Path $ReleaseDirectory "release-tag.txt"), $env:GITHUB_REF_NAME + [Environment]::NewLine, [System.Text.UTF8Encoding]::new($false))
}

$UiIndexPath = Join-Path $ReleaseDirectory "_internal\app\static\index.html"
if (Test-Path $UiIndexPath) {
    $UiHtml = [System.IO.File]::ReadAllText($UiIndexPath, [System.Text.Encoding]::UTF8)
    if ($UiHtml.Contains("__BUILD_ID__")) {
        $UiHtml = $UiHtml.Replace("__BUILD_ID__", $BuildId)
        [System.IO.File]::WriteAllText($UiIndexPath, $UiHtml, [System.Text.UTF8Encoding]::new($false))
    }
    else {
        Write-Warning "Build ID placeholder not found in $UiIndexPath"
    }
}
else {
    Write-Warning "Bundled UI file not found: $UiIndexPath"
}

Compress-Archive -Path $ReleaseDirectory -DestinationPath $ArchivePath -CompressionLevel Optimal

Write-Host ""
Write-Host "Release created successfully:" -ForegroundColor Green
Write-Host "  Folder: $ReleaseDirectory"
Write-Host "  ZIP:    $ArchivePath"
Write-Host "  Build:  $BuildId"
Write-Host ""
Write-Host "Copy the ZIP to the streaming PC, extract it, and run install-service.ps1 as Administrator."
