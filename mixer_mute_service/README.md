# Mixer Mute Service

A local HTTP service that provides **safe mute groups** for a digital mixing
console whose iPad app has no native mute groups. A Stream Deck (or any button)
calls HTTP endpoints; the service talks TCP to the mixer.

## The one rule that matters

Mute/unmute commands on this mixer **also rewrite the channel's fader value**.
Sending a wrong or stale fader value can move a live fader to a catastrophic
position. Therefore the service:

1. continuously tracks the mixer state (last observed fader value per channel),
2. **only sends** a mute/unmute when it knows a *fresh, valid, mixer-observed*
   fader value for that channel,
3. blocks otherwise, with a clear reason — never substitutes a default/dummy.

Safety first: an ignored button press is always preferable to a moved fader.

## Install & run

```bash
cd mixer_mute_service
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

# uses ./config.yaml (or set MIXER_CONFIG=/path/to/config.yaml)
python -m app.main
# or: uvicorn app.main:app --host 0.0.0.0 --port 8765
```

Then open **http://localhost:8765/** for the web UI.

## Web UI

The service serves a single-page control panel at `/`:

- **Status bar** — mixer connection, send-enabled state, last-update age, and a
  **Panic** button to disable/re-enable all sends.
- **Channels** — one card per channel showing the observed fader value, its age,
  mute state, and a `safe`/`unsafe` badge, with mute buttons and a **vertical
  fader** with a dB scale. Only the fader cap can be dragged (Shift = fine,
  arrow keys also work); clicking the track never jumps the fader. While
  dragging, the position is sent at most every 100 ms. The fader is locked
  while the channel's mute state is unknown. Blocked actions surface the reason
  as a toast (e.g. "fader value stale").
- **Mute groups** — per-group **Mute / Unmute / Toggle**, plus **create / edit /
  delete**. Each group shows its member channels and whether they are all safe.

The page polls `/state`, `/health` and `/groups` once per second.

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET  | `/health` | service + connection status |
| GET  | `/state` | per-channel fader/mute/safe view |
| GET  | `/channels/{channel}` | single channel state |
| POST | `/channels/{channel}/mute` | mute if safe |
| POST | `/channels/{channel}/unmute` | unmute if safe |
| POST | `/channels/{channel}/toggle` | toggle (needs fresh mute state) |
| POST | `/channels/{channel}/fader` | move fader `{value}` 0..1000, keeps mute state |
| POST | `/channels/{channel}/recall-fader` | same with a stored `{fader_hex}` |
| POST | `/groups/{group}/mute` | mute group (all-or-nothing) |
| POST | `/groups/{group}/unmute` | unmute group (all-or-nothing) |
| POST | `/groups/{group}/toggle` | toggle group |
| GET  | `/groups` | list all groups and their channels |
| POST | `/groups` | create group `{name, channels}` |
| GET  | `/groups/{group}` | one group's channels |
| POST | `/groups/{group}/update` | replace a group's channels `{channels}` |
| POST | `/groups/{group}/delete` | delete a group |
| PUT  | `/groups/{group}` | legacy alias for group update |
| DELETE | `/groups/{group}` | legacy alias for group deletion |
| GET  | `/update/status` | installed release, update progress and log tail |
| POST | `/update/start` | start the GitHub update (Windows release only) |
| POST | `/panic/disable` | block all sends (tracking continues) |
| POST | `/panic/enable` | re-enable sends |

Groups created/edited in the UI are persisted to `groups.json` next to
`config.yaml`; the `groups:` block in `config.yaml` is only the initial seed, so
your hand-written config (comments, mixer/safety settings) is never overwritten.
All group changes can be issued with POST requests. For example:

```bash
curl -X POST http://localhost:8765/groups/drums/update \\
  -H 'Content-Type: application/json' \\
  -d '{"channels":[1,2,3,4]}'
curl -X POST http://localhost:8765/groups/drums/delete
```

Blocked operations return HTTP **409** (unsafe/stale/disabled/no connection),
**404** (unknown group), or **400** (invalid channel), each with a JSON `reason`.

### Group semantics

Group actions are **all-or-nothing**: if any channel is unsafe, the whole action
is blocked and nothing is sent (unless `allow_partial_groups: true`). Group
*toggle* unmutes only when **all** channels are currently muted, otherwise mutes.

**Solo on unmute** (`safety.exclusive_group_unmute`, default on): unmuting a
group also **mutes every other channel** (buses included, unless a bus is in the
group) — so the group becomes the only thing audible. Muting a group does the
opposite of nothing to the others — it leaves them exactly as they were. Other
channels that have no safe (observed) fader value are **skipped**, not forced,
and reported under `others_skipped` in the response.

## Stream Deck

Configure buttons to run HTTP requests — never raw bytes:

```bash
curl -X POST http://localhost:8765/groups/drums/toggle
curl -X POST http://localhost:8765/channels/2/mute
curl -X POST http://localhost:8765/channels/2/unmute
```

PowerShell (Windows):

```powershell
Invoke-WebRequest -Method POST http://localhost:8765/groups/drums/toggle
```

## Dateien und Programme auf Netzwerk-PCs oeffnen

Die Webseite zeigt je Profil aus `desktop_display.profiles` einen Button.
Jedes Profil hat seine eigene `target_ip`; Bild und Programmeinstellungen
werden auf dem jeweiligen Ziel-PC ausgefuehrt. Der Windows-Agent muss dort als
angemeldeter Benutzer laufen, weil der Mixer-Webdienst als Windows-Dienst keine
Fenster auf dem Desktop anzeigen kann.

Beispielprofil fuer das QR-Bild auf PC `.11`:

```yaml
desktop_display:
  enabled: true
  token_env: "DESKTOP_AGENT_TOKEN"
  profiles:
    gemeindegaben:
      title: "Gemeindegaben-QR"
      kind: "image"
      path: 'I:\Googledrive\Streamingbilder\GemeindegabenQR.png'
      target_ip: "192.168.188.11"
      fullscreen: true
      monitor: 2
```

Weitere Profile erhalten eigene IDs und Ziel-IPs. `kind` ist `image`,
`program` oder `file`. Bei `program` muss `path` auf eine `.exe` zeigen;
`args` ist eine Liste separater Argumente, die ohne Shell gestartet werden.
Bei `file` oeffnet Windows die Datei mit der Standardanwendung. `monitor` ist
1-basiert; `fullscreen` aktiviert fuer Bilder bzw. Programmfenster den
Vollbildmodus. Bei `fullscreen: false` legt `position` die Ausrichtung des
Fensters fest: `top-left` (Standard), `top-right`, `bottom-left`,
`bottom-right` oder `center`. Nach Aenderung der Konfiguration den Service
neu starten.

Der Desktop-Agent und sein Installer liegen in `desktop_agent/`; der
Windows-Release-Build legt diesen Ordner neben die Installationsskripte. Auf
jedem Ziel-PC Python 3 mit Tkinter bereitstellen, den Agentenordner kopieren und
als angemeldeter Benutzer `install-agent.ps1` ausfuehren. Das Skript installiert
Pillow, registriert den unsichtbaren Hintergrundstart bei Benutzeranmeldung
(ohne Konsolenfenster) und erlaubt TCP 8766 in der Firewall nur von
`192.168.188.20`. Agentenmeldungen werden unter
`%LOCALAPPDATA%\MixerMuteDisplayAgent\agent.log` gespeichert.

Erzeuge einen gemeinsamen Token auf dem Webserver-PC `.20` in einer
administrativen PowerShell:

```powershell
$bytes = New-Object byte[] 32
$rng = [Security.Cryptography.RandomNumberGenerator]::Create()
$rng.GetBytes($bytes)
$token = [Convert]::ToBase64String($bytes)
$rng.Dispose()
[Environment]::SetEnvironmentVariable("DESKTOP_AGENT_TOKEN", $token, "Machine")
$token
```

Setze denselben Wert als Systemvariable `DESKTOP_AGENT_TOKEN` auf jedem
Ziel-PC. Nach dem Setzen ab- und wieder anmelden, damit der Agent den Wert
einliest; den Mixer-Service auf `.20` neu starten. In der Produktivkonfiguration
unter `C:\\ProgramData\\MixerMuteService\\config.yaml` muss
`desktop_display.enabled: true` stehen. Die API listet Profile ueber
`GET /desktop/profiles`; ein Button sendet `POST /desktop/{profile_id}/open`.

## Configuration (`config.yaml`)

- `mixer.ip` / `mixer.port` / `mixer.max_channels`
- `mixer.mode` — `direct` (recommended; state dump every 10 s) or `proxy`
- `mixer.buses` — optional: pin a bus to a channel number/name (others are
  discovered from the mixer)
- `safety.max_fader_age_seconds` — tighten to `0.5` for live use
- `safety.allow_partial_groups` — keep `false`
- `safety.group_send_delay_ms` — delay between per-channel sends in a group
- `groups` — named channel lists

## Connection modes

The mixer does **not** push state to a passive extra TCP connection (verified:
0 bytes), and it only talks to its primary client (the iPad). So the service
runs as a **transparent proxy** by default:

```
iPad app --TCP--> [ this service :6400 ] --TCP--> Mixer (192.168.188.30:6400)
                         |  parses every frame both ways
Stream Deck --HTTP-->----'  injects safe mute commands on the same link
```

**Setup:** point the mixer's iPad app at this host's IP (e.g. `192.168.188.73`),
port `6400`, instead of the mixer. The service relays everything to the real
mixer, so the app works exactly as before — but now every fader/mute change
(from the app *and* from the physical surface) flows through the service and is
tracked. Set `mixer.mode: direct` only if you have a mixer that pushes state to
extra clients.

### What the service observes

- iPad app changes (faders + mutes): `6e` command frames — fully parsed.
- Physical mute/unmute and physical fader moves: `6d` status frames — fully parsed.
- **Full state dump** (frame type `01`): the mixer answers every subscribe
  handshake about 0.1 s later with the complete state. In direct mode the
  service re-sends the handshake as a keepalive every 10 s, so every input
  channel and configured bus gets a start value right after connecting and is
  refreshed every 10 s (stable over ~60 dumps in a live test). Inputs come from
  top-level fields 1/2 (fader section in field 7), buses from top-level field 3
  (fader section in field 6, byte-identical to their `71` status frames —
  verified live for Echo and Monitor). A section must name the same channel/id
  as the object holding it; buses are only read if listed in `mixer.buses`.
  In proxy mode the app sends the handshake only once, so there is one dump per
  app connection and no periodic refresh.
- Changes on the console are not always visible as live frames (a dump showed
  channel 1 moving without any status frame in between), so the periodic dump
  is the reliable source in direct mode.

### Channel addressing (important quirk)

Input channels 2–16 are addressed **positionally** in the payload: `08 XX` with
`XX = channel − 1` (channel 2 = `08 01` … channel 16 = `08 0f`). **Channel 1 is
the exception** — it is addressed by a fixed object id (`15 00abc93d`) rather
than `08 00`, which the mixer never uses. The service handles this for both
reading and sending (see `CHANNEL_ID_FIELDS` in `app/protocol.py`).

**Buses/auxes** (e.g. Echo, Monitor) are a separate object class with their own
frame format:

| | input channels | buses |
| --- | --- | --- |
| command type | `6e` | `72` |
| status type | `6d` | `71` |
| mute flag | `20 01` | `18 01` |
| fader marker | `2d` | `3d` |
| id | `08 XX` (or `15…` for ch 1) | `08 <varint>` (Echo=`8001`, Monitor=`8301`) |

Buses are **discovered automatically** from the state dump (`app/buses.py`):
every bus the console reports gets the next free API/UI channel number and its
console name, and is then controllable like any other channel (including in
groups). The assignment is saved in `buses.json` next to `groups.json`, so a
bus keeps its number across restarts. Buses listed under `mixer.buses` in
`config.yaml` keep the number and name given there (Echo = 17, Monitor = 18,
because existing groups use them); the rest follow in id order (19 Hall 2 …
24 Bus 8). The main L/R output (id 255) is never added automatically — list it
explicitly only after checking it on the console. Built commands are
byte-identical to what the iPad app sends. Channel labels (`name`) flow
through `/state` to the UI.

## Freshness model

`safety.freshness_mode` controls when a stored fader value may be sent:

- `latest` (default) — use the most recent observed value while the proxy link
  is live. Relies on the proxy relaying every change (true for all app changes
  and all unmuted physical moves).
- `session` — only trust values observed during the current link session.
- `age` — only trust values newer than `max_fader_age_seconds` (defaults 1s).

In all modes a value must have been **observed** (never a default/dummy), the
fader hex must be valid, and the proxy link must be live.

## Architecture

```
Stream Deck --HTTP--> Local Service --TCP command--> Mixer
                          ^---------TCP state updates-----'
```

- `app/protocol.py` — `COMMAND_PREFIX`, `build_mute_command()`, fader encoding
  (`fader_value_to_hex()` / `fader_hex_to_value()`), and the byte-based frame
  parser (`StreamBuffer`, `parse_hex_stream()`), which also reads the input
  channels from the mixer's start dump.
- `app/state.py` — async-safe per-channel state store (monotonic timestamps).
- `app/mixer_client.py` — persistent TCP connection, buffered stream parser,
  reconnect with backoff (1/2/5/10s), sender.
- `app/safety.py` — per-channel and per-group safety checks.
- `app/controller.py` — the only sanctioned send path (state → safety → build → send → log).
- `app/groups.py` — writable mute-group store, persisted to `groups.json`.
- `app/proxy.py` — transparent TCP proxy (default mode): relays iPad<->mixer
  traffic, parses every frame into state, injects safe commands.
- `app/mixer_client.py` — direct-connection link (alternative `mode: direct`).
- `app/routes.py` / `app/main.py` — FastAPI endpoints, group CRUD, static UI, background link.
- `app/static/index.html` — self-contained web control panel (no build step).
- `tools/probe.py`, `tools/analyze.py` — read-only capture/decode helpers used to
  reverse-engineer the protocol.
- `tools/capture_proxy.py` — passive relay between the mixer app and the mixer
  that logs and decodes every frame (never sends anything of its own). Point
  the app's IP at `127.0.0.1`; logs go to `captures/`.

## Protocol (reverse-engineered)

```
COMMAND_PREFIX = 5a5a5a5a5a5a0000006e000000     (App -> Mixer, accepted)
STATUS frames  = 5a5a5a5a5a5a0000006d00000100   (Mixer -> iPad, status only)

Channel N: XX = N - 1 (one byte hex)
Mute:   PREFIX + 090908 XX 20012d FFFFFFFF
Unmute: PREFIX + 070708 XX     2d FFFFFFFF
FFFFFFFF = current fader value (exactly 4 bytes / 8 hex chars)
```

Status (`6d`) frames carry the same channel/fader structure and may contain a
lot of trailing null padding; they are parsed for state but never sent.

Frames are a length-delimited protobuf message behind the header:
`5a×6 | type u32 | length u32 | varint msg_len | message`. In a channel's
fader section field 1 = index, 4 = muted, 5 = fader; fields that are 0 are
omitted (so channel 1 has no index, and a fader at −∞ has no fader field).

**Fader value:** `FFFFFFFF` is a little-endian float32 position from `0.0`
(−∞, bottom) to `1000.0` (top). Captures match an X32-style law: about 750 =
0 dB, 500 = −10 dB, 375 = −20 dB, 1000 = +10 dB (used for display only).
NaN or out-of-range values are never parsed as state and never sent. There is
no fader-only command: a fader move always re-sends the observed mute flag.

## Notes / open questions

- It is not yet confirmed whether the mixer pushes status updates to an extra
  TCP connection or requires an initial sync request. If no fresh updates
  arrive, **every send is blocked by design** and `/state` shows the channels
  as unsafe — this is the intended fail-safe behaviour, not a bug.
- No safe mute-only command (without a fader value) has been found, hence the
  observed-fader-value requirement.
- The protocol is reverse-engineered and incomplete; `07 07` / `09 09` appear to
  be length/type markers but are not officially confirmed.

## systemd (Linux autostart)

See `systemd/mixer-mute-service.service`; adjust paths, then:

```bash
sudo cp systemd/mixer-mute-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now mixer-mute-service
```
