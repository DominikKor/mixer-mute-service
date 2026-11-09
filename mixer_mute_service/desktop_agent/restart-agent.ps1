# Started by the display agent after a self-update (POST /update), outside the
# agent's own process: wait until the old agent has ended, then start it again
# through its scheduled task (same user and environment as at logon), or
# directly with the same Python if the task cannot be started.
param(
    [string]$Python,
    [string]$Script
)

$TaskName = "MixerMuteDisplayAgent"

for ($i = 0; $i -lt 40; $i++) {
    $Running = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
        Where-Object { $_.CommandLine -like "*show_image_agent.py*" })
    if (-not $Running.Count) { break }
    Start-Sleep -Milliseconds 250
}
Start-Sleep -Milliseconds 500

try {
    Start-ScheduledTask -TaskName $TaskName -ErrorAction Stop
} catch {
    Start-Process -FilePath $Python -ArgumentList "`"$Script`"" -WindowStyle Hidden
}
