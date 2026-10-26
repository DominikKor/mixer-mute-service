# Update (or first install) of the display agent on this PC in one step.
#
# Normally runs inside the one-file "MixerDisplayAgent-Update.cmd" that the
# mixer service hands out (web UI key "Agent-Update"); that file defines
# $AgentVersion and $AgentFiles (file name -> base64) in front of this script.
# Started directly from an unpacked release, it takes the files next to itself.
#
# An installed agent is found via its scheduled task: its files are replaced in
# place, then the running agent is stopped and started again. Without a task,
# install-agent.ps1 does the first install, which needs administrator rights
# (firewall rule).

$ErrorActionPreference = "Stop"
$TaskName = "MixerMuteDisplayAgent"
$AgentPort = 8766

if (-not (Get-Variable AgentFiles -ErrorAction SilentlyContinue)) {
    $AgentFiles = @{}
    foreach ($File in Get-ChildItem -File $PSScriptRoot | Where-Object { $_.Name -match '\.(py|txt|ps1)$' -and $_.Name -notlike '._*' }) {
        $AgentFiles[$File.Name] = [Convert]::ToBase64String([IO.File]::ReadAllBytes($File.FullName))
    }
    $AgentVersion = "local"
}

function Test-AgentPort {
    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        $Client = New-Object System.Net.Sockets.TcpClient
        try {
            if ($Client.ConnectAsync("127.0.0.1", $AgentPort).Wait(300)) { return $true }
        } catch {
        } finally {
            $Client.Close()
        }
    }
    return $false
}

function Test-Administrator {
    $Identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    return ([Security.Principal.WindowsPrincipal]$Identity).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
}

try {
    Write-Host "Mixer Display-Agent Update ($AgentVersion)"
    $Task = Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    if ($Task) {
        $Action = $Task.Actions | Select-Object -First 1
        $AgentScript = $Action.Arguments.Trim().Trim('"')
        $AgentDirectory = Split-Path -Parent $AgentScript
    } else {
        $AgentDirectory = Join-Path $env:LOCALAPPDATA "MixerMuteDisplayAgent\agent"
        if (-not (Test-Administrator)) {
            throw "Kein installierter Agent gefunden. Die Erstinstallation braucht Administratorrechte: Datei per Rechtsklick 'Als Administrator ausfuehren'."
        }
    }
    Write-Host "Agent-Ordner: $AgentDirectory"

    # Write first: if that fails (no rights), the running agent stays untouched.
    New-Item -ItemType Directory -Force -Path $AgentDirectory | Out-Null
    foreach ($Name in $AgentFiles.Keys) {
        [IO.File]::WriteAllBytes((Join-Path $AgentDirectory $Name), [Convert]::FromBase64String($AgentFiles[$Name]))
    }
    Write-Host "$($AgentFiles.Count) Dateien geschrieben."

    if (-not $Task) {
        Write-Host "Erstinstallation ueber install-agent.ps1 ..."
        & (Join-Path $AgentDirectory "install-agent.ps1")
    } else {
        $Running = @(Get-CimInstance Win32_Process -Filter "Name = 'pythonw.exe' OR Name = 'python.exe'" |
            Where-Object { $_.CommandLine -like "*show_image_agent.py*" })
        foreach ($Process in $Running) {
            Write-Host "Stoppe laufenden Agent (PID $($Process.ProcessId))"
            Stop-Process -Id $Process.ProcessId -Force
        }
        if ($Running.Count) { Start-Sleep -Seconds 1 }

        $Py = Get-Command py.exe -ErrorAction SilentlyContinue
        if ($Py) {
            & $Py.Source -3 -m pip install --disable-pip-version-check -q -r (Join-Path $AgentDirectory "requirements.txt")
            if ($LASTEXITCODE -ne 0) { Write-Warning "pip konnte die Abhaengigkeiten nicht pruefen; der Agent wird trotzdem gestartet." }
        }

        try {
            Start-ScheduledTask -TaskName $TaskName
        } catch {
            Write-Warning "Aufgabe $TaskName liess sich nicht starten ($_); starte den Agent direkt."
            Start-Process -FilePath $Action.Execute -ArgumentList "`"$AgentScript`""
        }
        if (Test-AgentPort) {
            Write-Host "Fertig: Der Agent laeuft wieder (Port $AgentPort)." -ForegroundColor Green
        } else {
            Write-Warning "Der Agent antwortet nicht auf Port $AgentPort. Log: $env:LOCALAPPDATA\MixerMuteDisplayAgent\agent.log"
        }
    }
} catch {
    Write-Host "FEHLER: $_" -ForegroundColor Red
}
