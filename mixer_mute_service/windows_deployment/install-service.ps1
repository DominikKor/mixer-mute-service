#Requires -RunAsAdministrator
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$ServiceName = "MixerMuteService"
$FirewallRuleName = "MixerMuteService HTTP (TCP 8765)"
$HttpPort = 8765
$CameraWebRtcTcpRuleName = "MixerMuteService WebRTC (TCP 8889)"  # old, no longer needed: removed below
$CameraWebRtcUdpRuleName = "MixerMuteService WebRTC media (UDP 8189)"
$CameraMediaTcpRuleName = "MixerMuteService WebRTC media (TCP 8189)"
$CameraMediaPort = 8189
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

    # On-demand task for the web UI's update button: the service itself cannot
    # run the update (it is stopped and replaced during it), so it only starts
    # this task, which runs as SYSTEM independently of the service.
    $UpdateTaskName = "MixerMuteServiceUpdate"
    $UpdateScript = Join-Path $DataDirectory "update-from-github.ps1"
    $UpdateArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$UpdateScript`" -NonInteractive"
    $ExistingTask = Get-ScheduledTask -TaskName $UpdateTaskName -ErrorAction SilentlyContinue
    $TaskUpToDate = $null -ne $ExistingTask -and @($ExistingTask.Actions)[0].Arguments -eq $UpdateArgs
    if (-not $TaskUpToDate) {
        # Never re-register while it runs: that would be the update installing us.
        if ($null -ne $ExistingTask -and $ExistingTask.State -eq "Running") {
            Write-Warning "Update task is running; its definition is refreshed on the next install."
        }
        else {
            $Action = New-ScheduledTaskAction -Execute "powershell.exe" -Argument $UpdateArgs -WorkingDirectory $DataDirectory
            $TaskPrincipal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
            $TaskSettings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit (New-TimeSpan -Minutes 15) -MultipleInstances IgnoreNew
            Register-ScheduledTask -TaskName $UpdateTaskName -Action $Action -Principal $TaskPrincipal -Settings $TaskSettings -Description "Mixer Mute Service: update from GitHub (started by the web UI)" -Force | Out-Null
            Write-Host "Update task registered: $UpdateTaskName"
        }
    }
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

# Camera pictures (WebRTC). The signalling goes through the service port
# above; MediaMTX keeps its own signalling port 8889 on localhost, so that
# old rule is removed. Only the media port 8189 must be reachable, over UDP
# and, where UDP is dropped, TCP. The rules apply to every network profile:
# the streaming PC's LAN may be classified "Public", where Private-only rules
# do nothing. They accept only private (LAN) addresses instead, and the port
# carries nothing without a session negotiated through the service.
$CameraLanAddresses = @("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")
foreach ($RuleName in @($CameraWebRtcTcpRuleName, $CameraWebRtcUdpRuleName, $CameraMediaTcpRuleName)) {
    Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
}
New-NetFirewallRule `
    -DisplayName $CameraWebRtcUdpRuleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol UDP `
    -LocalPort $CameraMediaPort `
    -RemoteAddress $CameraLanAddresses `
    -Profile Any `
    -Description "Allow LAN WebRTC media (UDP) for the camera pictures" | Out-Null
New-NetFirewallRule `
    -DisplayName $CameraMediaTcpRuleName `
    -Direction Inbound `
    -Action Allow `
    -Protocol TCP `
    -LocalPort $CameraMediaPort `
    -RemoteAddress $CameraLanAddresses `
    -Profile Any `
    -Description "Allow LAN WebRTC media (TCP) for the camera pictures" | Out-Null
Write-Host ("WebRTC media rules active (UDP and TCP {0}, all profiles, private addresses only)." -f $CameraMediaPort) -ForegroundColor Green

# A block rule wins over every allow rule. Windows creates such rules for a
# program when its "allow access" prompt was cancelled, e.g. after starting
# mediamtx.exe by hand once. Remove those for mediamtx.exe only.
try {
    $MediaMtxBlockRules = @(
        Get-NetFirewallApplicationFilter -ErrorAction SilentlyContinue |
            Where-Object { $_.Program -like "*\mediamtx.exe" } |
            Get-NetFirewallRule -ErrorAction SilentlyContinue |
            Where-Object { $_.Direction -eq "Inbound" -and $_.Action -eq "Block" }
    )
    foreach ($Rule in $MediaMtxBlockRules) {
        Write-Host ("Removing firewall block rule for mediamtx.exe: {0}" -f $Rule.DisplayName) -ForegroundColor Yellow
        $Rule | Remove-NetFirewallRule -ErrorAction Stop
    }
}
catch {
    Write-Warning ("Could not check or remove firewall block rules for mediamtx.exe: {0}" -f $_.Exception.Message)
}

# Report what else can keep the camera pictures (and the web UI) from LAN devices.
try {
    foreach ($Connection in @(Get-NetConnectionProfile -ErrorAction Stop)) {
        Write-Host ("Network '{0}' ({1}) is classified {2}." -f $Connection.Name, $Connection.InterfaceAlias, $Connection.NetworkCategory)
        if ("$($Connection.NetworkCategory)" -eq "Public") {
            Write-Warning ("Network '{0}' is Public: the web UI rule (TCP {1}) only covers Private networks. Set it to Private: Set-NetConnectionProfile -InterfaceAlias '{2}' -NetworkCategory Private" -f $Connection.Name, $HttpPort, $Connection.InterfaceAlias)
        }
    }
    foreach ($FirewallProfile in @(Get-NetFirewallProfile -ErrorAction Stop)) {
        if ("$($FirewallProfile.Enabled)" -eq "True" -and "$($FirewallProfile.AllowInboundRules)" -eq "False") {
            Write-Warning ("Firewall profile {0} blocks all incoming connections, allow rules included; LAN devices get no camera pictures or web UI there." -f $FirewallProfile.Name)
        }
    }
}
catch {
    Write-Warning ("Could not read the network or firewall profiles: {0}" -f $_.Exception.Message)
}

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

