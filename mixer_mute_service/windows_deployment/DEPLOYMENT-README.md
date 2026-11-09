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

It keeps all data and logs under `C:\ProgramData\MixerMuteService` (see
[Files in ProgramData](#files-in-programdata)). That folder is readable and
writable only by Administrators and SYSTEM, because it holds camera passwords
and the GitHub token; edit `config.yaml` with an editor started as
Administrator.

The Windows service starts automatically during boot and restarts five seconds
after an unexpected failure. No user needs to be logged in.

Open <http://127.0.0.1:8765/> on the streaming PC to use the web interface.
From other devices in the same private LAN, open <http://PC-IP:8765/>.

The installer creates these Windows Firewall allow-rules:

| Rule | Port | Profile | For |
|---|---|---|---|
| MixerMuteService HTTP (TCP 8765) | TCP 8765 | Private | web UI, HTTP API, camera signalling (WHEP) and HLS fallback |
| MixerMuteService WebRTC media (UDP 8189) | UDP 8189 | all, private source addresses only | camera pictures (MediaMTX WebRTC media) |
| MixerMuteService WebRTC media (TCP 8189) | TCP 8189 | all, private source addresses only | camera pictures where UDP is dropped |

It removes the old `MixerMuteService WebRTC (TCP 8889)` rule (MediaMTX keeps
its signalling port 8889 on localhost now) and inbound block rules for
`mediamtx.exe`, and it reports each network's profile: if a network shows as
Public, set it to Private (see the main README, "Kamerabild mit geringer
Verzoegerung").

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

## Files in ProgramData

Updates and uninstalling never delete anything in
`C:\ProgramData\MixerMuteService`; the installer only creates `config.yaml`
and `groups.json` when they are missing.

| File | Content |
|---|---|
| `config.yaml` | configuration (mixer, safety, desktop profiles, ...) |
| `groups.json` | mute groups |
| `buses.json` | buses discovered from the mixer and their channel numbers |
| `channel_preferences.json` | "Immer an" channels |
| `channel_colors.json` | strip colours |
| `channel_settings.json` | saved channel settings (gain, EQ, ...) per mute group |
| `program_items.json` | the program / cue list |
| `companion_buttons.json` | Steuerung keys and categories |
| `native_control_state.json` | light levels and beamer state |
| `power_off_config.json` | power-off selection |
| `camera_streams.json`, `mediamtx-cameras.yml` | cameras, **including RTSP passwords** |
| `song_display.json`, `song_library.json` | song display settings and library index |
| `github-token.dat` | GitHub token for updates (encrypted, see below) |
| `update-status.json`, `logs\` | update progress, service and update logs |
| `backups\` | data files saved before each update (newest 10) |
| `update-from-github.*`, `github-repo.txt` | copies of the updater (see below) |

A data file the service cannot read is kept as `<name>.corrupt` (or
`<name>.corrupt-<time>`) next to it instead of being overwritten.

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
`main`** (after the checks pass; pushes to other branches and pull requests
are only checked, never released). A commit with a version tag is released as
that version (`v1.3.0`); later commits get names from `git describe`, e.g.
`v1.3.0-2-gabc1234` (2 commits after v1.3.0). So everything pushed to `main`
becomes installable on the streaming PC -- push only finished work. From the
first installation of such a release on, the streaming PC has a desktop
shortcut **"Mixer Service aktualisieren"**:

1. Double-click it and confirm the Administrator prompt.
2. The first time only: paste a GitHub token with read access to the
   repository (fine-grained token, *Only select repositories* -> this repo,
   *Contents: Read-only*). It is stored in `github-token.dat`, encrypted with
   DPAPI for this computer (LocalMachine) and readable only by Administrators
   and SYSTEM, so the update task (SYSTEM) can use it too.
3. The updater compares the installed version with the newest release,
   downloads it, checks its SHA-256 checksum and installs it. Before installing
   it saves the data files to `backups\<old release>-<time>.zip`; everything
   in ProgramData is kept.

**Or from the web UI:** the **Update** key in the header opens a dialog that
starts the same update, shows its progress and log, waits through the service
restart and closes itself when done (reloading the page after an update). It
needs the token to be stored once via the desktop shortcut. Behind it, the
service starts the scheduled task `MixerMuteServiceUpdate` (runs the updater
as SYSTEM with `-NonInteractive`), because the service cannot replace itself
while it runs. Progress: `update-status.json`, log: `logs\update.log`. The
control is unavailable for about a minute during an update.

If an update went wrong, the previous data files are in the newest ZIP under
`backups\`; stop the service, copy them back, start it again.

Publishing (developer): push to `main` -- that is all. To give a build a
version number, tag it and push both together, so only one release is built:

```bash
git tag -a v1.3.0 -m "v1.3.0: ..."
git push origin main --follow-tags
```

## Updating manually

Build or obtain the new release ZIP, extract it to a new temporary folder, and
run `update-static.bat` (or `update-static.ps1`) as Administrator. It saves the
data files to `backups\`, unregisters the old service, replaces
`C:\Program Files\MixerMuteService`, installs and starts the new one, and
checks that it answers.

The `config-template.yaml` and `groups-template.json` files in Program Files
are only defaults for a first installation.

## Uninstalling

Run as Administrator:

```powershell
.\uninstall-service.ps1          # unregister the service only
.\uninstall-service.ps1 -Purge   # remove the installation completely
```

Without `-Purge` the script only unregisters the service (updates use exactly
this). `-Purge` also removes the firewall rules, the update task, the desktop
shortcut, the updater copies in ProgramData and the program folder. The data
in `C:\ProgramData\MixerMuteService` is kept in both cases; delete that folder
by hand only after saving anything you still need.

## Building a release (developer PC only)

GitHub Actions builds every release (see `.github/workflows/ci.yml`); a local
build is only needed for testing. Build on a 64-bit Windows 10/11 PC with
64-bit Python 3.14 installed:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\windows_deployment\build-release.ps1
```

The release builder:

1. creates an isolated build environment under `windows_deployment\.build-venv`
   and installs the exact versions from `constraints.txt`;
2. bundles the service and static web UI with PyInstaller in one-folder mode;
3. downloads the pinned WinSW 2.12.0 x64 service wrapper and verifies its
   SHA-256 checksum;
4. adds configuration templates, the desktop agent and the service-management
   scripts;
5. produces:

```text
windows_deployment\release\MixerMuteService-Windows-x64\
windows_deployment\release\MixerMuteService-Windows-x64.zip
```

PyInstaller must build the Windows executable on Windows; the macOS development
machine cannot directly produce the Windows binary. Development and local
operation stay unchanged: `python -m app.main` from `mixer_mute_service`.
