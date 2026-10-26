# Mixer Mute Service — Windows deployment

This release is self-contained. The streaming PC does **not** need Python,
pip, Docker, or a virtual environment.

## First installation on the streaming PC

1. Extract the complete ZIP to a temporary folder. Do not run it from inside
   the ZIP preview.
2. Right-click Windows PowerShell and choose **Run as administrator**.
3. If Windows blocks local scripts for this one PowerShell session, run:

   ```powershell
   Set-ExecutionPolicy -Scope Process Bypass
   ```

4. Change to the extracted folder and run:

   ```powershell
   .\install-service.ps1
   ```

The installer copies the application to:

```text
C:\Program Files\MixerMuteService
```

It creates the editable data files and logs under:

```text
C:\ProgramData\MixerMuteService\config.yaml
C:\ProgramData\MixerMuteService\groups.json
C:\ProgramData\MixerMuteService\logs\
```

The Windows service starts automatically during boot and restarts five seconds
after an unexpected failure. No user needs to be logged in.

Open <http://127.0.0.1:8765/> on the streaming PC to use the web interface.
From other devices in the same private LAN, open <http://PC-IP:8765/>.
The install/update scripts create a Windows Firewall allow-rule for TCP 8765 on
the Private profile.

## Configuration

Edit `C:\ProgramData\MixerMuteService\config.yaml`, not the template under
Program Files. After changing it, restart the service in an Administrator
PowerShell:

```powershell
Restart-Service MixerMuteService
```

If the Stream Deck software runs on this same PC, consider changing
`server.host` to `127.0.0.1`. Keep `0.0.0.0` only when another computer or
device must call the HTTP API.

## Status and troubleshooting

From the extracted release folder or the installed program directory:

```powershell
.\service-status.ps1
```

The two main log files are normally:

```text
C:\ProgramData\MixerMuteService\logs\MixerMuteServiceService.out.log
C:\ProgramData\MixerMuteService\logs\MixerMuteServiceService.err.log
```

For foreground debugging, stop the service and run:

```powershell
Stop-Service MixerMuteService
.\run-manually.ps1
```

Start the background service again afterward:

```powershell
Start-Service MixerMuteService
```

## Updating from GitHub (recommended)

Releases are built automatically by GitHub Actions for **every push to
`main`** (after the tests pass). A commit with a version tag is released as
that version (`v1.3.0`); later commits get names from `git describe`, e.g.
`v1.3.0-2-gabc1234` (2 commits after v1.3.0). So everything pushed to `main`
becomes installable on the streaming PC -- push only finished work. From the
first installation of such a release on, the streaming PC has a desktop
shortcut **"Mixer Service aktualisieren"**:

1. Double-click it and confirm the Administrator prompt.
2. The first time only: paste a GitHub token with read access to the
   repository (fine-grained token, *Only select repositories* -> this repo,
   *Contents: Read-only*). It is stored encrypted for this Windows user.
3. The updater compares the installed version with the newest release,
   downloads it, checks its SHA-256 checksum and installs it. Config, groups,
   discovered buses and logs in ProgramData are kept.

**Or from the web UI:** the **Update** key in the header (left of the status
display) opens a dialog that starts the same update, shows its progress and
log, waits through the service restart and closes itself when done (reloading
the page after an update). It needs the token to be stored once via the desktop
shortcut. Behind it, the service starts the scheduled task
`MixerMuteServiceUpdate` (runs the updater as SYSTEM with `-NonInteractive`),
because the service cannot replace itself while it runs. Progress:
`C:\ProgramData\MixerMuteService\update-status.json`, log: `logs\update.log`.
The control is unavailable for about a minute during an update.

Buses no longer need to be configured by hand: the service discovers them from
the mixer's state dump and remembers their channel numbers in
`C:\ProgramData\MixerMuteService\buses.json`.

Publishing (developer): push to `main` -- that is all. To give a build a
version number, tag it and push both together, so only one release is built:

```bash
git tag -a v1.3.0 -m "v1.3.0: ..."
git push origin main --follow-tags
```

## Updating manually

Build or obtain the new release ZIP, extract it to a new temporary folder, and
run either `update-static.bat` or `install-service.ps1` as Administrator. Both
paths stop the old service, update `C:\Program Files\MixerMuteService`, and
start it again.

Your live `config.yaml`, `groups.json`, and logs in ProgramData are preserved.
The `config-template.yaml` and `groups-template.json` files in Program Files are
only defaults for a first installation.

## Uninstalling

Run as Administrator:

```powershell
.\uninstall-service.ps1
```

The script unregisters the service but deliberately preserves both program
files and ProgramData. Delete those folders manually only after saving anything
you still need.

## Building a release (developer PC only)

Build on a 64-bit Windows 10/11 PC with 64-bit Python 3.14 installed:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\windows_deployment\build-release.ps1
```

The build uses an isolated environment under `windows_deployment\.build-venv`
and produces:

```text
windows_deployment\release\MixerMuteService-Windows-x64\
windows_deployment\release\MixerMuteService-Windows-x64.zip
```

PyInstaller must build the Windows executable on Windows; the macOS development
machine cannot directly produce the Windows binary.

