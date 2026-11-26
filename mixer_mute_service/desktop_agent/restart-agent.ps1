# Started by the display agent after a self-update (POST /update), outside the
# agent's own process: wait until the old agent has ended, then start it again
# through its scheduled task (same user and environment as at logon), or
# directly with the same Python if the task does not bring it back.
#
# This script itself runs as a WMI-created process: it has neither the user's
# PATH nor the user's own variables. A direct start therefore first takes the
# machine's and the user's variables from the registry, so the agent finds mpv
# and its token as after a logon (the agent also repairs its environment at
# start, see restore_user_environment in show_image_agent.py).
param(
    [string]$Python,
    [string]$Script
)

$TaskName = "MixerMuteDisplayAgent"

function Get-AgentProcesses {
    @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*show_image_agent.py*" })
}

for ($i = 0; $i -lt 40; $i++) {
    if (-not (Get-AgentProcesses).Count) { break }
    Start-Sleep -Milliseconds 250
}
Start-Sleep -Milliseconds 500

# The task may refuse silently (e.g. Windows still counts the old run), so
# only a running agent counts as restarted.
$Started = $false
try {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        if ((Get-AgentProcesses).Count) { $Started = $true; break }
    }
} catch {
    $Started = $false
}

if (-not $Started) {
    $Machine = [Environment]::GetEnvironmentVariables("Machine")
    $UserVariables = [Environment]::GetEnvironmentVariables("User")
    foreach ($Name in $Machine.Keys) {
        if ($Name -ne "Path") {
            [Environment]::SetEnvironmentVariable($Name, [Environment]::ExpandEnvironmentVariables($Machine[$Name]), "Process")
        }
    }
    foreach ($Name in $UserVariables.Keys) {
        if ($Name -ne "Path") {
            [Environment]::SetEnvironmentVariable($Name, [Environment]::ExpandEnvironmentVariables($UserVariables[$Name]), "Process")
        }
    }
    $PathParts = @($Machine["Path"], $UserVariables["Path"], $env:Path) | Where-Object { $_ }
    $env:Path = [Environment]::ExpandEnvironmentVariables(($PathParts -join ";"))
    $env:LOCALAPPDATA = [Environment]::GetFolderPath("LocalApplicationData")
    $env:APPDATA = [Environment]::GetFolderPath("ApplicationData")
    $env:USERPROFILE = [Environment]::GetFolderPath("UserProfile")
    Start-Process -FilePath $Python -ArgumentList "`"$Script`"" -WindowStyle Hidden
}
