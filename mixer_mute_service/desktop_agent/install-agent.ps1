# Run once as the Windows user who is logged in at the target display.
[CmdletBinding()]
param()

$ErrorActionPreference = "Stop"
$AgentDirectory = Split-Path -Parent $MyInvocation.MyCommand.Path
$TaskName = "MixerMuteDisplayAgent"
$PythonLauncher = (Get-Command py.exe -ErrorAction Stop).Source

& $PythonLauncher -3 -m pip install -r (Join-Path $AgentDirectory "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "Could not install Pillow for the display agent." }

$Action = New-ScheduledTaskAction -Execute $PythonLauncher -Argument "-3 `"$(Join-Path $AgentDirectory 'show_image_agent.py')`""
$Trigger = New-ScheduledTaskTrigger -AtLogOn -User "$env:USERDOMAIN\$env:USERNAME"
$Principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited
$Settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName $TaskName -Action $Action -Trigger $Trigger -Principal $Principal -Settings $Settings -Force | Out-Null

$RuleName = "Mixer display agent (TCP 8766)"
Get-NetFirewallRule -DisplayName $RuleName -ErrorAction SilentlyContinue | Remove-NetFirewallRule
New-NetFirewallRule -DisplayName $RuleName -Direction Inbound -Action Allow -Protocol TCP -LocalPort 8766 -RemoteAddress 192.168.188.20 -Profile Private | Out-Null
Write-Host "Display agent is scheduled at user logon. Set the DESKTOP_AGENT_TOKEN system variable, then sign out and back in."
