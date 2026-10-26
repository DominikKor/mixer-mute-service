#Requires -RunAsAdministrator
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ServiceName = "MixerMuteService"
$FirewallRuleName = "MixerMuteService HTTP (TCP 8765)"
$HttpPort = 8765
$SourceDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$InstallDirectory = Join-Path $env:ProgramFiles "MixerMuteService"
$DataDirectory = Join-Path $env:ProgramData "MixerMuteService"
$LogDirectory = Join-Path $DataDirectory "logs"

Write-Host "Installing Mixer Mute Service..." -ForegroundColor Cyan

$ExistingService = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue
if ($null -ne $ExistingService -and $ExistingService.Status -ne "Stopped") {
    Write-Host "Stopping the existing service for the update..."
    Stop-Service -Name $ServiceName -Force
    (Get-Service -Name $ServiceName).WaitForStatus("Stopped", [TimeSpan]::FromSeconds(20))
}

New-Item -ItemType Directory -Force -Path $InstallDirectory, $DataDirectory, $LogDirectory | Out-Null

$SourceFullPath = [System.IO.Path]::GetFullPath($SourceDirectory).TrimEnd('\')
$InstallFullPath = [System.IO.Path]::GetFullPath($InstallDirectory).TrimEnd('\')
if ($SourceFullPath -ne $InstallFullPath) {
    Write-Host "Copying program files to $InstallDirectory..."
    Copy-Item -Path (Join-Path $SourceDirectory "*") -Destination $InstallDirectory -Recurse -Force
}

$OverrideStaticDirectory = Join-Path $InstallDirectory "app\static"
if (Test-Path $OverrideStaticDirectory) {
    Write-Host "Removing stale override UI at $OverrideStaticDirectory..."
    Remove-Item -Path $OverrideStaticDirectory -Recurse -Force
}

$ConfigPath = Join-Path $DataDirectory "config.yaml"
$GroupsPath = Join-Path $DataDirectory "groups.json"
if (-not (Test-Path $ConfigPath)) {
    Copy-Item (Join-Path $InstallDirectory "config-template.yaml") $ConfigPath
    Write-Host "Created $ConfigPath"
}
else {
    Write-Host "Preserved existing $ConfigPath"
}
if (-not (Test-Path $GroupsPath)) {
    Copy-Item (Join-Path $InstallDirectory "groups-template.json") $GroupsPath
    Write-Host "Created $GroupsPath"
}
else {
    Write-Host "Preserved existing $GroupsPath"
}

# The GitHub updater runs from ProgramData: update-static.ps1 deletes the
# Program Files folder during an update, so it must not run from there.
$RepoFile = Join-Path $InstallDirectory "github-repo.txt"
if (Test-Path $RepoFile) {
    foreach ($File in @("update-from-github.ps1", "update-from-github.bat", "github-repo.txt")) {
        Copy-Item (Join-Path $InstallDirectory $File) (Join-Path $DataDirectory $File) -Force
    }
    $ShortcutPath = Join-Path ([Environment]::GetFolderPath("CommonDesktopDirectory")) "Mixer Service aktualisieren.lnk"
    $Shell = New-Object -ComObject WScript.Shell
    $Shortcut = $Shell.CreateShortcut($ShortcutPath)
    $Shortcut.TargetPath = Join-Path $DataDirectory "update-from-github.bat"
    $Shortcut.WorkingDirectory = $DataDirectory
    $Shortcut.Description = "Mixer Mute Service auf die neueste GitHub-Version aktualisieren"
    $Shortcut.Save()
    Write-Host "Updater: $ShortcutPath"
}

$Wrapper = Join-Path $InstallDirectory "MixerMuteServiceService.exe"
if (-not (Test-Path $Wrapper)) {
    throw "Service wrapper not found at $Wrapper"
}

if ($null -eq $ExistingService) {
    Write-Host "Registering the Windows service..."
    & $Wrapper install
    if ($LASTEXITCODE -ne 0) {
        throw "WinSW could not register the service."
    }
}

Set-Service -Name $ServiceName -StartupType Automatic
Start-Service -Name $ServiceName
(Get-Service -Name $ServiceName).WaitForStatus("Running", [TimeSpan]::FromSeconds(20))

Write-Host "Ensuring Windows Firewall rule for TCP $HttpPort..."
$ExistingFirewallRule = Get-NetFirewallRule -DisplayName $FirewallRuleName -ErrorAction SilentlyContinue
if ($null -ne $ExistingFirewallRule) {
    Remove-NetFirewallRule -DisplayName $FirewallRuleName | Out-Null
}
New-NetFirewallRule `
    -DisplayName $FirewallRuleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $HttpPort `
    -Profile Private `
    -Description "Allow LAN access to Mixer Mute Service web UI" | Out-Null
Write-Host "Firewall rule active (Private profile)." -ForegroundColor Green

Write-Host "Waiting for the HTTP endpoint..."
$Health = $null
for ($Attempt = 1; $Attempt -le 15; $Attempt++) {
    try {
        $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 2
        break
    }
    catch {
        Start-Sleep -Seconds 1
    }
}

if ($null -eq $Health) {
    Write-Warning "The service is running, but its HTTP health endpoint did not answer yet. Check $LogDirectory."
}
else {
    Write-Host "Service health: enabled=$($Health.enabled), mixer_connected=$($Health.mixer_connected)" -ForegroundColor Green
}

$ServerHost = $null
if (Test-Path $ConfigPath) {
    $HostLine = Select-String -Path $ConfigPath -Pattern '^\s*host\s*:\s*"?([^"\r\n#]+)"?' -CaseSensitive:$false | Select-Object -First 1
    if ($null -ne $HostLine -and $HostLine.Matches.Count -gt 0) {
        $ServerHost = $HostLine.Matches[0].Groups[1].Value.Trim()
    }
}
if ($ServerHost -eq "127.0.0.1" -or $ServerHost -eq "localhost") {
    Write-Warning "Config host is '$ServerHost'. LAN devices cannot reach the UI until you set server.host to 0.0.0.0 in $ConfigPath and restart the service."
}

Write-Host ""
Write-Host "Installation complete." -ForegroundColor Green
Write-Host "Web UI:  http://127.0.0.1:8765/"
Write-Host "LAN UI:  http://<THIS-PC-IP>:$HttpPort/"
Write-Host "Config:  $ConfigPath"
Write-Host "Groups:  $GroupsPath"
Write-Host "Logs:    $LogDirectory"

