# Started by the display agent after a self-update (POST /update) or for a
# plain restart (POST /restart), outside the agent's own process: wait until
# the old agent has ended, then start it again through its scheduled task
# (same user and environment as at logon), or directly with the same Python
# if the task does not bring it back.
#
# This script itself runs as a WMI-created process: it has neither the user's
# PATH nor the user's own variables. A direct start therefore first fills in
# what is missing from the registry, like the agent does itself at start
# (restore_user_environment in show_image_agent.py): only variables the
# process lacks, never the per-user ones from the machine (HKLM holds
# USERNAME=SYSTEM), PATH merged from all three.
param(
    [string]$Python,
    [string]$Script
)

$TaskName = "MixerMuteDisplayAgent"
$PerUserNames = @("USERNAME", "USERPROFILE", "USERDOMAIN", "USERDOMAIN_ROAMINGPROFILE",
    "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA")

# Process ids of running agents, or $null when WMI cannot tell (unknown is
# not the same as none: a second agent must not be started on a guess).
function Get-AgentIds {
    try {
        # The comma keeps an empty list a list (PowerShell would return $null).
        return ,@(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" -ErrorAction Stop |
            Where-Object { $_.CommandLine -like "*show_image_agent.py*" } |
            ForEach-Object { [int]$_.ProcessId })
    } catch {
        return $null
    }
}

for ($i = 0; $i -lt 40; $i++) {
    $Ids = Get-AgentIds
    if ($null -eq $Ids) { Start-Sleep -Seconds 3; break }
    if (-not $Ids.Count) { break }
    Start-Sleep -Milliseconds 250
}
Start-Sleep -Milliseconds 500

# Agents still alive now (an old one that did not end) do not count as
# restarted: only a process id that was not there before.
$Before = Get-AgentIds
if ($null -eq $Before) { $Before = @() }

$Started = $false
$Unknown = $false
try {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
    # The task may refuse silently (e.g. Windows still counts the old run).
    for ($i = 0; $i -lt 40; $i++) {
        Start-Sleep -Milliseconds 250
        $Now = Get-AgentIds
        if ($null -eq $Now) { $Unknown = $true; continue }
        $Unknown = $false
        if (@($Now | Where-Object { $Before -notcontains $_ }).Count) { $Started = $true; break }
    }
} catch {
    $Started = $false
    $Unknown = $false
}

# Could not check whether the task started it: do not risk a second agent.
if (-not $Started -and -not $Unknown) {
    $Machine = [Environment]::GetEnvironmentVariables("Machine")
    $UserVariables = [Environment]::GetEnvironmentVariables("User")
    foreach ($Name in @($UserVariables.Keys) + @($Machine.Keys)) {
        if (-not $Name -or $Name -eq "Path" -or $Name.Contains("=")) { continue }
        # Names are case-insensitive: ask Windows, not the hashtable.
        if ($null -ne [Environment]::GetEnvironmentVariable($Name, "Process")) { continue }
        if ($UserVariables.Contains($Name)) {
            $Value = $UserVariables[$Name]
        } elseif ($PerUserNames -contains $Name.ToUpper()) {
            continue
        } else {
            $Value = $Machine[$Name]
        }
        [Environment]::SetEnvironmentVariable($Name, [Environment]::ExpandEnvironmentVariables($Value), "Process")
    }
    $PathParts = @($env:Path, $Machine["Path"], $UserVariables["Path"]) | Where-Object { $_ }
    $env:Path = [Environment]::ExpandEnvironmentVariables(($PathParts -join ";"))
    foreach ($Folder in @(@("LOCALAPPDATA", "LocalApplicationData"), @("APPDATA", "ApplicationData"), @("USERPROFILE", "UserProfile"))) {
        $Value = [Environment]::GetEnvironmentVariable($Folder[0], "Process")
        if (-not $Value -or $Value -like "*systemprofile*") {
            [Environment]::SetEnvironmentVariable($Folder[0], [Environment]::GetFolderPath($Folder[1]), "Process")
        }
    }
    Start-Process -FilePath $Python -ArgumentList "`"$Script`"" -WindowStyle Hidden
}
