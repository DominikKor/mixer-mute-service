[CmdletBinding()]
param()

$Service = Get-Service -Name "MixerMuteService" -ErrorAction SilentlyContinue
if ($null -eq $Service) {
    Write-Host "Mixer Mute Service is not installed."
    exit 1
}

$Service | Format-List Name, DisplayName, Status, StartType

try {
    $Health = Invoke-RestMethod -Uri "http://127.0.0.1:8765/health" -TimeoutSec 3
    Write-Host "HTTP health:"
    $Health | ConvertTo-Json
}
catch {
    Write-Warning "The HTTP health endpoint is not responding: $($_.Exception.Message)"
}

Write-Host "Logs: $env:ProgramData\MixerMuteService\logs"

