#Requires -RunAsAdministrator
<#
.SYNOPSIS
    Unregisters the Mixer Mute Service.

.DESCRIPTION
    Without switches: stops and unregisters the Windows service and nothing
    else. update-static.ps1 relies on exactly this (an update is uninstall +
    install, run by the update task itself), so the plain call must stay as is.

    -Purge additionally removes what install-service.ps1 set up around the
    service: the firewall rules, the SYSTEM update task, the desktop shortcut,
    the updater copies in ProgramData and the program folder. The data in
    C:\ProgramData\MixerMuteService (config.yaml, groups, program, keys,
    cameras, songs, backups, logs, GitHub token) is always kept; delete that
    folder by hand if it should go too.

.EXAMPLE
    .\uninstall-service.ps1            # unregister only (used by updates)
    .\uninstall-service.ps1 -Purge     # remove the installation completely
#>
[CmdletBinding()]
param(
    [switch]$Purge
)

$ErrorActionPreference = "Stop"
$ServiceName = "MixerMuteService"
$InstallDirectory = Join-Path $env:ProgramFiles "MixerMuteService"
$DataDirectory = Join-Path $env:ProgramData "MixerMuteService"
$Wrapper = Join-Path $InstallDirectory "MixerMuteServiceService.exe"
# Names as created by install-service.ps1.
$FirewallRuleNames = @(
    "MixerMuteService HTTP (TCP 8765)",
    "MixerMuteService WebRTC (TCP 8889)",
    "MixerMuteService WebRTC media (UDP 8189)"
)
$UpdateTaskName = "MixerMuteServiceUpdate"
$ShortcutName = "Mixer Service aktualisieren.lnk"
$UpdaterCopies = @("update-from-github.ps1", "update-from-github.bat", "github-repo.txt")

$Service = Get-Service -Name $ServiceName -ErrorAction SilentlyContinue

if ($null -eq $Service) {
    Write-Host "Mixer Mute Service is not installed."
}
else {
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
}

if (-not $Purge) {
    Write-Host "Program files and $DataDirectory were preserved deliberately."
    Write-Host "Use -Purge to also remove firewall rules, the update task, the shortcut and the program folder."
    exit 0
}

Write-Host "Purging the installation (data in $DataDirectory is kept)..." -ForegroundColor Cyan

foreach ($RuleName in $FirewallRuleNames) {
    $Rule = Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue
    if ($null -ne $Rule) {
        $Rule | Remove-NetFirewallRule
        Write-Host "Removed firewall rule: $RuleName"
    }
}

$Task = Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction SilentlyContinue
if ($null -ne $Task) {
    if ($Task.State -eq "Running") {
        Write-Warning "The update task is running; it was left in place. Run -Purge again when the update has finished."
    }
    else {
        Unregister-ScheduledTask -TaskName $UpdateTaskName -Confirm:$false
        Write-Host "Removed scheduled task: $UpdateTaskName"
    }
}

$ShortcutPath = Join-Path ([Environment]::GetFolderPath("CommonDesktopDirectory")) $ShortcutName
if (Test-Path $ShortcutPath) {
    Remove-Item -Force $ShortcutPath
    Write-Host "Removed shortcut: $ShortcutPath"
}

foreach ($File in $UpdaterCopies) {
    $Path = Join-Path $DataDirectory $File
    if (Test-Path $Path) {
        Remove-Item -Force $Path
        Write-Host "Removed updater copy: $Path"
    }
}

if (Test-Path $InstallDirectory) {
    try {
        Remove-Item -Path $InstallDirectory -Recurse -Force
        Write-Host "Removed program folder: $InstallDirectory"
    }
    catch {
        Write-Warning "Could not remove $InstallDirectory (a file is in use). Close it and delete the folder by hand."
    }
}

Write-Host "Purge complete. Data kept in $DataDirectory." -ForegroundColor Green
