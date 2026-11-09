#Requires -RunAsAdministrator
# First install of the display agent on this PC (needs Administrator rights for
# the firewall rule). Run it as Administrator while the Windows user who uses
# the display is signed in: the agent is registered for that user (the user at
# the console, not the administrator account that runs this script).
#
#   .\install-agent.ps1                                  # console user, service at 192.168.188.20
#   .\install-agent.ps1 -User PC11\Beamer -ServiceAddress 192.168.188.20
[CmdletBinding()]
param(
    # Windows account the agent runs for ("DOMAIN\user"); default: the user
    # signed in at this PC's console, or the current user if nobody is.
    [string]$User = "",
    # Only this address (the streaming PC running the Mixer Mute Service) may
    # reach the agent's port.
    [string]$ServiceAddress = "192.168.188.20"
)

$ErrorActionPreference = "Stop"
$AgentDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "MixerMuteDisplayAgent"
$AgentPort = 8766
$PythonLauncher = (Get-Command py.exe -ErrorAction Stop).Source

if ([string]::IsNullOrWhiteSpace($User)) {
    $User = (Get-CimInstance Win32_ComputerSystem).UserName
    if ([string]::IsNullOrWhiteSpace($User)) { $User = "$env:USERDOMAIN\$env:USERNAME" }
}
Write-Host "Display agent for Windows user: $User"

& $PythonLauncher -3 -m pip install -r (Join-Path $AgentDirectory "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install Pillow for the display agent." }

$PythonExecutable = & $PythonLauncher -3 -c "import sys; print(sys.executable)"
if ($LASTEXITCODE -ne 0 -or -not $PythonExecutable) { throw "Could not locate Python 3." }
$Pythonw = Join-Path (Split-Path -Parent $PythonExecutable.Trim()) "pythonw.exe"
if (-not (Test-Path $Pythonw)) { throw "pythonw.exe was not found next to $PythonExecutable." }

$AgentScript = Join-Path $AgentDirectory "show_image_agent.py"
$Action = New-ScheduledTaskAction -Execute $Pythonw -Argument "`"$AgentScript`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User $User
$Principal = New-ScheduledTaskPrincipal -UserId $User -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null

$RuleName = "Mixer display agent (TCP $AgentPort)"
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort $AgentPort -RemoteAddress $ServiceAddress -Profile Private | Out-Null
Write-Host "Display agent is scheduled at logon of $User (port $AgentPort open for $ServiceAddress only)."
Write-Host "Set the DESKTOP_AGENT_TOKEN system variable, then sign out and back in."
