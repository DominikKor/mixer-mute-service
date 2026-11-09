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

The service serves a single-page control panel at `/`, built for a 13-inch
iPad in Safari (both orientations) and desktop browsers alike. Every section
head (Desktop, Channels, Mute Gruppen, Programm, Steuerung) folds its section
in and out; the choice is remembered per device.

- **Status bar**: mixer connection, send-enabled state, last-update age, the
  **Panic** button (disable/re-enable all sends) and **UPDATE** (Windows
  release only).
- **Stage** switch (next to the title, remembered per device): like Stage
  Manager, one section fills the screen and the others sit as live miniatures
  on the left; a tap on one flies it onto the stage. The page itself never
  scrolls. Programm and the full-size channel strips (300 px faders, as many
  per row as fit) keep their size and scroll on the stage, with a free strip
  on the right to swipe with the finger; Programm's "Weiter" bar stays at the
  top. Everything else is laid out to fit the screen (compact channels as
  tiles, Steuerung categories packed as tiles, Kameras as large as the stage
  allows) and only scaled down a little if that is not enough. Camera videos
  are not restarted when other sections switch; in Chrome/Edge they keep
  playing even when the Kameras section itself moves, in Safari they
  reconnect once when it moves onto or off the stage.
- **Desktop**: one tile per profile from `desktop_display.profiles` (see
  below), plus tiles to add or edit profiles.
- **Channels**: one strip per channel with the observed fader value, mute
  state and a `safe`/`unsafe` badge, mute key and a **vertical fader** with a
  dB scale. Only the fader cap can be dragged (finger or mouse; Shift = fine,
  arrow keys also work); clicking the track never jumps the fader. While
  dragging, the position is sent at most every 100 ms. The fader is locked while
  the channel's state is unknown. **Kompakt** hides the faders. Strips can get a
  colour, and **Immer an** protects a channel from group mutes and solo.
  Blocked actions show the reason as a toast (e.g. "fader value stale").
- **Mute Gruppen**: per-group mute / unmute, **create / edit / delete**, and
  per-channel fader presets (**Use** / **Use All**). **Mutegruppe aus Status**
  creates a group from the channels currently unmuted, with their observed
  fader values as presets.
- **Kanal-Einstellungen** (slider key on each mute group): saves gain, high-pass,
  EQ, gate and compressor of the group's input channels as they are set on the
  console right now, and shows them per channel. Read-only: the values come
  from the mixer's state dump and nothing is sent back (buses are not
  included). Stored in `channel_settings.json`, raw as the console sent them.
- **Programm**: a cue list with planned time, title, person, mute group
  (optional: a row without one changes no mutes) and up to three actions for
  playback, translation and lyrics. Play activates the group first, then runs
  the linked actions in order; the delay records the click offset from the
  planned time. The cue bar shows the clock, the next row and how long until
  it is due; **Weiter** starts the next row (also from a Stream Deck via
  `POST /program/next`). The row that ran last today is marked "Läuft", the
  next one "Nächster", earlier ones are dimmed. **Duplizieren** puts the copy
  right below its original, **Ablauf zurücksetzen** forgets all start times.
  The eye key top right of a row hides it: the row turns grey and stays in
  the list, but **Weiter** skips it (its own Play still works); tap it again
  to show the row. Times can be typed as `930`, `9:30` or `9.30`.
- **Steuerung**: the keys imported from the former Companion setup (lights,
  OBS, beamer, "Overall" scene macros), grouped in categories; the camera
  keys live in the Kameras section. With
  **Bearbeiten** switched on, keys can be renamed, moved, created and deleted.

The page polls `/state`, `/health` and `/groups` about once per second.

## Beamer Hide / Unhide

The Beamer category has separate **Beamer hide** and **Beamer unhide** actions.
Because the projector command is a toggle, first set the actual current mode in
the **Beamerzustand** selector. This only records the current state; it does not
send a projector command. A Hide/Unhide action sends one `avmute=AV Mute`
HTTP form POST only when its target differs from the recorded state. The state is persisted in
`native_control_state.json`; it starts as **unknown**, so both actions stay
blocked until the operator declares the current mode. If the projector is
toggled by another device, update the selector before using the web controls.
Freeze, Beamer on and Beamer off also use direct HTTP form POSTs to
`http://192.168.6.40/form/control_cgi`; no local Beamer scripts are needed.

## Agent PCs und Alles ausschalten

In **Steuerung** kannst du die Agent-PCs aus `desktop_display.profiles` und den
Beamer fuer **Alles ausschalten** auswaehlen. Die Auswahl wird neben
`config.yaml` in `power_off_config.json` gespeichert. Der Button fragt vor dem
Ausfuehren nach einer Bestaetigung, schaltet den Beamer aus und sendet an jeden
gewaehlten Agent-PC den authentifizierten Shutdown-Auftrag. Der Desktop-Agent
plant `shutdown /s /t 10` und beendet danach laufende Prozesse des angemeldeten
Windows-Benutzers mit `taskkill`. Die Agenten muessen dafuer aktualisiert sein;
der Shutdown-Endpunkt ist nur fuer IP-Adressen konfigurierter Desktop-Profile
erreichbar.

Jede Agent-PC-Zeile hat zusaetzlich eine eigene, bestaetigungspflichtige
**Herunterfahren**-Taste.

## Endpoints

### Beamer hide/unhide

The **Beamerzustand** selector declares the projector's current physical mode;
it does not send a command. The state starts unknown, so **Beamer hide** and
**Beamer unhide** stay blocked until the operator sets it. Each action sends an
AV Mute HTTP form POST only when the requested target differs from the stored
state. On success, the new state is persisted in
`native_control_state.json`. If the projector is toggled by another device,
update the selector before using the web buttons.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET | `/beamer/state` | read the stored state (`hidden: true`, `false` or `null`) |
| POST | `/beamer/state` | declare the current state `{ "hidden": true/false }` without toggling |
| GET | `/power/off-config` | read the selected agent PCs and Beamer for **Alles ausschalten** |
| PUT | `/power/off-config` | save `{ "target_ips": [...], "beamer": true/false }` |
| POST | `/power/off` | execute the saved shutdown selection |
| POST | `/power/agents/{target_ip}/shutdown` | shut down one configured agent PC |

### Steuerung keys (`/companion/...`)

The keys started as a Companion export and now run directly from the service
(Shelly lights, Art-Net stage light, VISCA cameras, OBS WebSocket and direct
Beamer HTTP controls); Companion itself is no longer needed. The two
mixer keys of the export are mapped to the safe channel endpoints instead of
replaying its raw HEX. Categories and key changes are stored in
`companion_buttons.json` next to `config.yaml`. New keys can call one of the
built-in actions, a mixer channel or a mute group; arbitrary shell commands and
raw HEX are not accepted. "Overall" macros activate a mute group, then run their
linked light/camera/OBS keys in order.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET  | `/companion/buttons` | list imported controls |
| POST | `/companion/buttons/{button_id}/press` | press one allowlisted control |
| POST | `/companion/categories` | create a category `{name}` |
| POST | `/companion/categories/{category_id}/visibility` | set `{visible}` |
| POST | `/companion/buttons` | create a safe custom button |
| POST | `/companion/buttons/{button_id}/update` | rename or reassign a button |
| POST | `/companion/buttons/{button_id}/delete` | delete a button |
| GET  | `/health` | service + connection status |
| GET  | `/state` | per-channel fader/mute/safe view |
| GET  | `/channels/{channel}` | single channel state |
| POST | `/channels/{channel}/mute` | mute if safe |
| POST | `/channels/{channel}/unmute` | unmute if safe |
| POST | `/channels/{channel}/toggle` | toggle (needs fresh mute state) |
| POST | `/channels/{channel}/fader` | move fader `{value}` 0..1000, keeps mute state |
| POST | `/channels/{channel}/recall-fader` | same with a stored `{fader_hex}` |
| POST | `/channels/{channel}/always-on` | `{enabled}`: protect from group mutes and solo |
| GET  | `/channel-colors` | strip colours and palette |
| POST | `/channels/{channel}/color` | `{color}` from the palette, or `null` |
| POST | `/groups/{group}/mute` | mute group (all-or-nothing) |
| POST | `/groups/{group}/unmute` | unmute group (all-or-nothing) |
| POST | `/groups/{group}/toggle` | toggle group |
| POST | `/groups/from-current-status` | create a group from currently unmuted channels and their observed faders `{name}` |
| GET  | `/program/items` | list saved program points |
| POST | `/program/items` | create a program point |
| PUT  | `/program/items/{item_id}` | update a program point |
| DELETE | `/program/items/{item_id}` | delete a program point |
| POST | `/program/items/{item_id}/play` | activate group and linked actions |
| POST | `/program/items/{item_id}/duplicate` | copy a row, placed right after it |
| POST | `/program/items/{item_id}/hidden` | `{"hidden": true}` greys a row out, `/program/next` skips it |
| GET  | `/program/status` | `current_id` (started last today) and `next_id` (hidden rows skipped) |
| POST | `/program/next` | start the next row (409 `program finished` at the end) |
| POST | `/program/reset` | forget all start times and delays |
| GET  | `/groups` | list all groups and their channels |
| POST | `/groups` | create group `{name, channels}` |
| GET  | `/groups/{group}` | one group's channels |
| POST | `/groups/{group}/update` | replace a group's channels `{channels}` |
| POST | `/groups/{group}/delete` | delete a group |
| GET  | `/groups/{group}/settings-snapshot` | saved gain/HPF/EQ/gate/compressor of the group's channels |
| POST | `/groups/{group}/settings-snapshot` | save them now, read from the mixer's state dump (nothing is sent) |
| POST | `/groups/{group}/rename` | rename `{name}`; Programm rows, Steuerung keys and Overall macros follow |
| PUT  | `/groups/{group}` | legacy alias for group update |
| DELETE | `/groups/{group}` | legacy alias for group deletion |
| GET  | `/desktop/profiles` | list desktop profiles |
| POST | `/desktop/profiles` | add a profile (with `id`) |
| PUT  | `/desktop/profiles/{id}` | change a profile (also `POST .../update`) |
| DELETE | `/desktop/profiles/{id}` | delete a profile |
| POST | `/desktop/{id}/open` | show/open the profile on its target PC |
| GET  | `/desktop/agent/update.cmd` | one-click agent update for the target PCs |
| GET  | `/update/status` | installed release, update progress and log tail |
| POST | `/update/start` | start the GitHub update (Windows release only) |
| POST | `/panic/disable` | block all sends (tracking continues) |
| POST | `/panic/enable` | re-enable sends |

Groups created/edited in the UI are persisted to `groups.json` next to
`config.yaml`; the `groups:` block in `config.yaml` is only the initial seed, so
your hand-written config (comments, mixer/safety settings) is never overwritten.
Group capture requires a connected mixer and known mute states for every
channel. It stores only unmuted channels, blocks if any included channel lacks
a safe observed fader value, and saves those values as the group's **Use All**
presets. Muted channels are omitted.
All group changes can be issued with POST requests. For example:

```bash
curl -X POST http://localhost:8765/groups/drums/update \\
  -H 'Content-Type: application/json' \\
  -d '{"channels":[1,2,3,4]}'
curl -X POST http://localhost:8765/groups/drums/delete
```

Blocked operations return HTTP **409** (unsafe/stale/disabled/no connection),
**404** (unknown group), or **400** (invalid channel), each with a JSON `reason`.

**No login:** the API has no authentication. Anyone who can reach port 8765
can mute, move faders, edit groups, start the update and add desktop profiles,
and a `program` profile starts any `.exe` on its target PC. Run the service
only in a trusted network (the mixer network) and never forward the port to
the internet.

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
curl -X POST http://localhost:8765/program/next     # Programm: "Weiter"
curl -X POST http://localhost:8765/program/next -H 'Content-Type: application/json' \
     -d '{"expected_next_id": "<id>"}'                # only if that row is still next
```

PowerShell (Windows):

```powershell
Invoke-WebRequest -Method POST http://localhost:8765/groups/drums/toggle
```

## RTSP-Kameras im Web

Das Kamerapanel nimmt RTSP-Adressen ueber **Kamera** auf. Adressen muessen auf
eine private IPv4-Kamera zeigen und duerfen keine Zugangsdaten enthalten;
Benutzername und Passwort kommen in die separaten Felder. Die Weboberflaeche
spielt RTSP nicht direkt ab: Der Dienst startet MediaMTX und zeigt das Bild
per WebRTC in einer responsiven Kachel (siehe unten). Quellen werden nur abgerufen,
waehrend ein Viewer verbunden ist.

MediaMTX muss zusaetzlich auf dem Service-PC installiert sein. Lade die
passende Windows-Version von den [offiziellen Releases](https://github.com/bluenviron/mediamtx/releases),
entpacke `mediamtx.exe` und setze `MEDIAMTX_PATH` als Systemvariable, zum Beispiel:

```powershell
[Environment]::SetEnvironmentVariable("MEDIAMTX_PATH", "C:\Tools\mediamtx.exe", "Machine")
```

Danach den Mixer-Service neu starten. Auf iPad und anderen PCs muss nichts
installiert werden.

### Kamerabild mit geringer Verzoegerung (WebRTC fuer alle Geraete)

Jede Kachel laedt `camera-player.html?cam=<kamera>` vom Dienst. Der Player
verbindet sich per WebRTC, auf jedem Geraet (iPad, andere PCs, Streaming-PC):

- **Signalisierung** (WHEP: ein HTTP-POST mit dem Angebot des Browsers) geht
  ueber den Service-Port 8765 (`/camera-webrtc/<kamera>/whep`,
  `app/camera_webrtc.py`). MediaMTX lauscht dafuer nur noch auf
  `127.0.0.1:8889`; Port 8889 muss nicht mehr freigegeben sein.
- **Bild** kommt direkt von MediaMTX ueber Port **8189**, per UDP oder, wo UDP
  verworfen wird, per TCP. Gemessen auf einem Mac (Python-Testsender anstelle
  von MediaMTX, Player im Browser, Signalisierung ueber den Dienst): ca. 40 ms
  vom Senden bis zum fertigen Bild. Kamera-Encoder und WLAN kommen dazu
  (typisch 100-300 ms), HLS liegt dagegen mehrere Sekunden zurueck.
- **Rueckfall:** Kommt die WebRTC-Verbindung binnen 6 s nicht zustande (Port
  8189 gesperrt), zeigt die Kachel dasselbe Bild als HLS ueber Port 8765
  (`/camera-hls/<kamera>/`, MediaMTX nur auf `127.0.0.1:8888`) und oben links
  ein kleines **HLS**. Das Bild ist dann mehrere Sekunden verzoegert. Siehst du
  „HLS“, ist die Firewall am Streaming-PC noch nicht richtig eingerichtet.
  Bricht ein laufendes WebRTC-Bild ab (Kamera weg, Dienst-Neustart), verbindet
  der Player neu und bleibt bei WebRTC.

`install-service.ps1` (laeuft auch bei jedem Update) richtet die Firewall dafuer ein:

- Regeln fuer **UDP und TCP 8189**, fuer **alle Netzwerkprofile**
  (Domaene, Privat, Oeffentlich), aber nur fuer private Absender
  (`10.0.0.0/8`, `172.16.0.0/12`, `192.168.0.0/16`). Bisher galt die Regel nur
  im Profil „Privat“ und nur fuer `LocalSubnet`; ist das Netz des PCs als
  „Oeffentlich“ eingestuft, griff sie nicht, und das iPad bekam kein
  WebRTC-Bild. Der Port selbst liefert nichts ohne eine ueber den Dienst
  ausgehandelte Sitzung.
- Die alte Regel fuer TCP 8889 wird entfernt.
- **Blockier-Regeln fuer `mediamtx.exe`** werden entfernt. Windows legt sie an,
  wenn die Rueckfrage „Zugriff zulassen“ einmal abgebrochen wurde (z. B. nach
  einem manuellen Start von `mediamtx.exe`); eine Blockier-Regel schlaegt jede
  Zulassen-Regel.
- Das Skript zeigt, wie jedes Netzwerk eingestuft ist, und warnt bei
  „Oeffentlich“ (die Regel fuer die Weboberflaeche, TCP 8765, gilt nur fuer
  „Privat“) und bei „Alle eingehenden Verbindungen blocken“.

**Einmalig am Streaming-PC nach dem Update:**

1. Update wie gewohnt einspielen (UPDATE-Taste bzw. `update-from-github.ps1`);
   das fuehrt `install-service.ps1` als Administrator aus und setzt die Regeln.
   Alternativ `install-service.ps1` direkt in einer Administrator-PowerShell
   starten.
2. Die Ausgabe des Skripts lesen: Steht dort bei einem Netzwerk „Public“, das
   Netz auf „Privat“ stellen (Einstellungen > Netzwerk und Internet >
   Ethernet > Netzwerkprofiltyp: Privat, oder
   `Set-NetConnectionProfile -InterfaceAlias "<Name>" -NetworkCategory Private`).
3. Auf dem iPad die Seite neu laden. Erscheint in der Kachel kein „HLS“, laeuft
   WebRTC. Den TCP-Weg pruefst du von einem anderen Windows-PC aus mit
   `Test-NetConnection 192.168.188.20 -Port 8189`.

Laeuft zusaetzlich eine andere Firewall (Virenscanner-Suite), braucht sie
dieselbe Freigabe fuer UDP und TCP 8189. Bei mehreren Netzwerkkarten (z. B.
eigene Karte fuer das Kameranetz 192.168.5.x) meldet MediaMTX alle Adressen;
der Browser waehlt die erreichbare. Klappt das nicht, `CAMERA_WEBRTC_HOST` als
Systemvariable auf die LAN-Adresse setzen, unter der iPad und PCs den Dienst
erreichen (z. B. `192.168.188.20`).

**Kamera-Einstellungen fuer wenig Verzoegerung** (an der Kamera selbst, nicht
im Dienst einstellbar):

- Codec **H.264** ohne B-Frames (Profil Baseline oder Main). H.265 koennen
  nicht alle Browser per WebRTC abspielen.
- **Keyframe-Abstand (GOP/I-Frame-Intervall) ca. 1 s**, also gleich der
  Bildrate (z. B. 25 bei 25 fps, 30 bei 30 fps). Ein neuer Betrachter sieht
  erst ab dem naechsten Keyframe ein Bild, und der HLS-Rueckfall kann ein
  Segment nur an einem Keyframe beginnen: Bei 4 s Abstand liegt HLS deutlich
  weiter zurueck.
- Konstante Bitrate passend zum WLAN (fuer 1080p etwa 4-6 Mbit/s); das iPad
  im 5-GHz-WLAN betreiben.

Kameraliste und Relay-Konfiguration liegen in `camera_streams.json` und
`mediamtx-cameras.yml` neben `config.yaml`; beide koennen Zugangsdaten
enthalten. `mediamtx.log` kann ebenfalls Quelladressen protokollieren. Diese
Dateien sind vom Git ausgeschlossen und sollten wie Zugangsdaten behandelt
werden. Der Dienst hat keine Web-Anmeldung: nur in einem vertrauenswuerdigen
LAN verwenden und Kamera-Zugangsdaten nicht ueber ein untrusted WLAN eingeben.
MediaMTX leitet Codecs unveraendert weiter; fuer breite Browser-Kompatibilitaet
sollte die Kamera H.264 ohne B-Frames liefern.

Heisst eine Kamera **Cam1** oder **Cam2** (Gross-/Kleinschreibung und
Leerzeichen egal), zeigt ihre Kachel unter dem Bild die VISCA-Steuerung dieser
Kamera: ihre Preset-Tasten (das zuletzt aufgerufene Preset leuchtet), vier
Pfeiltasten zum Schwenken/Neigen und zwei Zoom-Tasten **Weit**/**Nah**.
Pfeile und Zoom bewegen die Kamera nur, solange die Taste gedrueckt ist;
Loslassen (auch neben der Taste), Fensterwechsel oder spaetestens 30 s
(Schwenken) bzw. 15 s (Zoom) stoppen sie
(`POST /cameras/{cam1|cam2}/pan-tilt` mit
`{"direction": "left"|"right"|"up"|"down"|"stop"}`,
`POST /cameras/{cam1|cam2}/zoom` mit `{"direction": "in"|"out"|"stop"}`).
Eine gehaltene Pfeiltaste wiederholt ihre Richtung jede Sekunde; kommt 3 s
lang weder Wiederholung noch Stop an (z. B. WLAN weg), stoppt der Dienst die
Kamera selbst.

Die Kamera-Tasten (Presets, Zoom) stehen seit Katalog-Version 7 nicht mehr in
der Steuerung; eine bestehende `companion_buttons.json` wird beim Start
umgestellt (umbenannte Presets behalten ihre Beschriftung im Kamera-Bereich).
Die Overall-Makros und Programmpunkte rufen die Presets weiter per ID auf.

## Dateien und Programme auf Netzwerk-PCs oeffnen

Die Webseite zeigt je Profil aus `desktop_display.profiles` einen Button.
Jedes Profil hat seine eigene `target_ip`; Bild und Programmeinstellungen
werden auf dem jeweiligen Ziel-PC ausgefuehrt. Der Windows-Agent muss dort als
angemeldeter Benutzer laufen, weil der Mixer-Webdienst als Windows-Dienst keine
Fenster auf dem Desktop anzeigen kann.

### Ziel-PCs

In der Weboberfläche wird keine IP-Adresse eingetippt: Profile, Liedanzeige und
Datei-Aktionen im Programm wählen **Streaming PC** oder **Anzeige PC** (mit der
IP in Klammern). Namen und Adressen der beiden stehen im Fenster **Ziel-PCs**
(Taste im Kopf der Desktop-Sektion oder „PCs…“ neben jeder Auswahl), gespeichert
in `target_pcs.json` neben `config.yaml` (Start: Streaming PC `192.168.188.20`,
Anzeige PC `192.168.188.11`). Wird dort eine Adresse geändert, stellt der Dienst
alles um, was die alte Adresse benutzt: Profile in `config.yaml` (nur die
`target_ip`-Zeile), Liedanzeige, Auswahl für „Alles ausschalten“ sowie
Datei-Aktionen in Programmzeilen und Vorlagen; ein Tausch der beiden Adressen
klappt ebenso. Eine Adresse, die zu keinem der beiden PCs passt, bleibt als
„Andere IP“ erhalten. API: `GET /target-pcs`, `PUT /target-pcs`
`{"pcs": [{"id": "streaming"|"display", "name", "ip"}, …]}`.

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
`program`, `file` oder `audio`. Bei `program` muss `path` auf eine `.exe` zeigen;
`args` ist eine Liste separater Argumente, die ohne Shell gestartet werden.
Bei `file` oeffnet Windows die Datei mit der Standardanwendung. `monitor` ist
1-basiert; `fullscreen` aktiviert fuer Bilder bzw. Programmfenster den
Vollbildmodus. Bei `fullscreen: false` legt `position` die Ausrichtung des
Fensters fest: `top-left` (Standard), `top-right`, `bottom-left`,
`bottom-right` oder `center`. Nach Aenderung der Konfiguration von Hand den
Service neu starten.

Ein `audio`-Profil enthaelt den Pfad zu einer Musikdatei auf dem Ziel-PC. Dort
muss mpv installiert sein und entweder ueber `PATH` oder die Windows-Variable
`MPV_PATH` auf `mpv.exe` auffindbar sein. Nach einer Aenderung der
Umgebungsvariable den Ziel-PC ab- und wieder anmelden und den Desktop-Agenten
neu starten. Die Audio-Kachel spielt den Titel ohne Player-Fenster ab; ein
erneuter Play-Klick ersetzt die laufende, vom Agenten gestartete Wiedergabe.
Stop beendet nur diesen Agenten-Player, keine anderen mpv-Instanzen. Der Agent
laeuft im angemeldeten Benutzerkonto, sodass die normale Windows-Audioausgabe
des Ziel-PCs verwendet wird. Bei Netzwerkdateien muss der Pfad fuer dieses
Benutzerkonto erreichbar sein; ein UNC-Pfad ist robuster als ein verbundenes
Laufwerk.

Beispiel fuer eine Systemvariable auf dem Ziel-PC:

```powershell
[Environment]::SetEnvironmentVariable("MPV_PATH", "C:\Tools\mpv\mpv.exe", "Machine")
```

Die Wiedergabesteuerung verwendet `POST /desktop/{profile_id}/audio/play` und
`POST /desktop/{profile_id}/audio/stop`.

**Ein-/Ausfaden:** Im Profil-Editor eines Audio-Profils schaltet
**Ein-/Ausfaden** sanftes Ein- und Ausblenden ein. Play startet dann stumm und
blendet ueber die eingestellte Dauer (Standard 5 s) **von 0 auf 100 %** ein;
Stop blendet von der aktuellen Lautstaerke auf 0 aus und beendet erst dann den
Player. Eine Lautstaerke-Einstellung gibt es nicht: Geregelt wird nur die
mpv-Lautstaerke auf dem Ziel-PC (ueber mpvs IPC-Pipe), immer bis 100 %, das
Mischpult wird nicht angefasst. Die Lautstaerke im Saal stellst du am
Mischpult-Kanal des Ziel-PCs ein. In `config.yaml`:

```yaml
    hintergrundmusik:
      kind: "audio"
      path: 'I:\Musik\Hintergrund.mp3'
      fade: true
      fade_seconds: 5
```

Fruehere Versionen hatten eine Zielstufe `fade_db` (Standard -30 dB). Eine
solche Zeile in einer alten `config.yaml` wird ignoriert; gefadete Musik kommt
deshalb jetzt bis zu 30 dB lauter aus dem Ziel-PC als vorher. Den Pegel einmal
am Mischpult (Gain/Fader des PC-Kanals) neu einstellen.

Dafuer muss der Desktop-Agent auf dem Ziel-PC aktuell sein; ein aelterer Agent
lehnt Play/Stop mit Fade ab (die Meldung sagt das), ohne Fade laeuft alles wie
bisher. Dateien, die ein Programmpunkt direkt abspielt, werden nicht gefadet.

Profile lassen sich auch in der Weboberflaeche anlegen (graue **+**-Kachel
hinter den Profilen) und ueber das Drei-Punkte-Menue bearbeiten oder loeschen;
die Buttons erscheinen sofort, ohne Neustart. Fuer ein Audio-Profil **Audio**
waehlen, Titel und den vollstaendigen Musikdatei-Pfad auf dem Ziel-PC eintragen
und die IP-Adresse dieses PCs angeben. Play startet die Wiedergabe ohne
Player-Fenster, Stop beendet die laufende Agenten-Wiedergabe auf diesem PC.
Dabei wird `config.yaml` nie neu geschrieben: ein neues Profil wird
hinter dem letzten eingefuegt, beim Bearbeiten werden nur die geaenderten Werte
ersetzt. Kommentare, andere Profile und alle anderen Einstellungen bleiben
unveraendert. Vor dem Speichern wird geprueft, dass sich wirklich nur dieses
Profil geaendert hat, sonst wird nichts geschrieben. Die vorige Fassung liegt
als `config.yaml.bak` daneben. API: `POST /desktop/profiles` (mit `id`) und
`PUT /desktop/profiles/{id}` bzw. `POST /desktop/profiles/{id}/update`. Updates
ueberschreiben die `config.yaml` in ProgramData ebenfalls nie.

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

## Dateien direkt im Programm

Statt einer Desktop- oder Companion-Taste kann ein Programmpunkt in *Playback*,
*Übersetzung* oder *Liedtext* auch direkt eine Datei starten: **Datei oder
Pfad…** wählen, den vollständigen Pfad auf dem Ziel-PC eintragen und den PC
wählen (*Streaming-PC (dieser Service)* oder ein PC aus den Desktop-Profilen).
Musikdateien (mp3, wav, m4a, aac, flac, ogg, opus, wma) spielt der Display-Agent
dort ohne Fenster mit mpv, `.exe` startet er, alles andere öffnet Windows mit
der Standard-App. Gespeichert wird `file:<ip>|<pfad>` im jeweiligen Feld.

**Durchsuchen…** gibt es nur, wenn die Weboberfläche auf dem Streaming-PC selbst
geöffnet ist (`http://127.0.0.1:8765`): Der Service zeigt dann Ordner und
Dateien dieses PCs (`GET /files/browse`). Anfragen von anderen Geräten lehnt er
ab (403); dort wird der Pfad eingetippt.

## Lieder (FoilPresenter) im Programm

Die FoilPresenter-Bibliothek bleibt auf dem Anzeige-PC (z. B. `.11`). Der
Display-Agent dort liest die `.foil`-Dateien aus dem eingestellten Ordner (dessen
Unterordner `Songs`, oder den Ordner selbst); der Service liest sie ein
(`app/foil.py`), merkt sie sich in `song_library.json` und schickt zum Anzeigen
immer nur eine Folie an den Agent, der sie im Vollbild zeichnet. Eine Folie ist
eine Strophe in der Reihenfolge des Lieds (der Refrain wiederholt sich).

In der Weboberfläche unter **Programm → Liedanzeige** (gilt für alle Lieder,
gespeichert in `song_display.json`): Ordner auf dem Anzeige-PC, Anzeige-PC (IP),
Monitor, Vollbild, Schrift, größte Schriftgröße (px bei 1080p, lange Folien werden
kleiner), Text- und Hintergrundfarbe, Titel und Copyright auf der ersten Folie,
Ausrichtung (links, mittig, rechts) und **Vorschau nächste Folie**: wo unter dem
Text Platz bleibt, stehen dort in Grau (Textfarbe in den Hintergrund gemischt)
die ersten Zeilen der nächsten Folie, höchstens halb so groß. Der Text selbst
bleibt dabei unverändert groß und an seiner Stelle; nach der letzten Folie gibt
es keine Vorschau. Ein Anzeige-PC mit älterem Display-Agent bekommt die Folie
ohne Vorschau und Ausrichtung, bis der Agent aktualisiert ist („Display Service
herunterladen“).
**Titelfolie** (Standard: an): Jedes Lied beginnt mit einer eigenen Folie, auf
der der Titel in der Mitte steht und das Copyright darunter (mit dem Schalter
*Copyright*). Sie ist Folie 0, die Liedfolien zählen ab 1; ein Programmpunkt
startet mit ihr. Der Schalter *Titel* (Titel oben auf der ersten Liedfolie) gilt
nur ohne Titelfolie. Die Titelfolie wird als normale, zentrierte Folie geschickt,
jeder Display-Agent zeigt sie also ohne Update.
**Speichern und Bibliothek laden** liest die Bibliothek (neu) ein.

Ein Programmpunkt bekommt sein Lied in der Spalte *Liedtext* („♪ Lied wählen“,
Suche nach Titel oder Liederbuch-Nummer); beim Start zeigt er die erste Folie.
Unter der Weiter-Leiste erscheinen dann eine 16:9-Vorschau, alle Folien als
Miniaturen (antippen = anzeigen) und **Zurück / Weiter / Schwarz / Aus**. Nach
der letzten Folie schaltet *Weiter* auf schwarz.

Tastatur und Presenter (immer, solange die Seite offen ist; nicht beim Tippen in
einem Feld oder bei offenem Dialog):

| Taste | Wirkung |
|---|---|
| Bild ab | nächste Folie des Lieds; ist das Lied durch (schwarz nach der letzten Folie) oder läuft keins: nächster Programmpunkt |
| →, Leertaste | nur nächste Liedfolie, nie ein Programmpunkt (eine versehentlich gedrückte Taste schaltet so keine Mutegruppe) |
| Bild auf, ← | vorige Folie (im Programm geht es nie zurück) |
| B oder . | Bildschirm schwarz / wieder an (ohne Lied: Anzeige-PC schwarz / aus) |
| H | Beamer hide / unhide (wie die Tasten „Beamer hide/unhide“; der Ist-Zustand muss einmal gesetzt sein) |

**Neues Lied…** (im Liedauswahl-Fenster) legt ein Lied an: Titel, Copyright
und Liedtext einfügen; jede Leerzeile beginnt eine neue Folie, die Folien werden
durchnummeriert. Eine Folie kann als Refrain markiert werden; er kommt dann
automatisch nach jeder Strophe (Schalter: am Anfang, nach jeder Strophe, am
Ende). Die Reihenfolge lässt sich auch von Hand eintippen, z. B. `R 1 2 R 3 R`
(R = Refrain); die Vorschau zeigt alle Folien in dieser Reihenfolge. Gespeichert
wird als FoilPresenter-`.foil`-Datei in der Bibliothek auf dem Anzeige-PC
(Dateiname wie in FoilPresenter, z. B. `Gott_ du bist da.foil`); eine vorhandene
Datei wird nie überschrieben.

Der Agent braucht dafür die neue Version (Weboberfläche: *Display Service
herunterladen*, auf dem Anzeige-PC ausführen). Endpunkte, z. B. für Companion:

| Methode | Pfad | |
|---|---|---|
| GET/PUT | `/songs/settings` | Liedanzeige-Einstellungen |
| POST | `/songs/library/reload` | Bibliothek vom Anzeige-PC lesen |
| GET | `/songs`, `/songs/{datei}` | Liste, ein Lied mit allen Folien |
| GET | `/songs/live` | was gerade angezeigt wird |
| POST | `/songs/create` `{"title", "copyright", "slides", "refrain", "order"}` | neues Lied in der Bibliothek speichern |
| POST | `/songs/show` `{"song_id", "slide"}` | Folie anzeigen |
| POST | `/songs/next`, `/songs/prev`, `/songs/blank`, `/songs/hide` | weiter, zurück, schwarz, aus |

## Kamera-Presets im Programm

Jeder Programmpunkt hat eine Zeile **Kamera-Presets**: pro Kamera (Cam 1,
Cam 2) ein VISCA-Preset (Namen wie bei den Preset-Tasten der Kamera-Sektion)
und **Umschalten nach … s** (Standard 5 s, 1 bis 30 s). Ein Preset faehrt
die Kamera sichtbar an; damit diese Bewegung nie im Stream zu sehen ist,
bewegt der Dienst nur die Kamera, die in OBS gerade **nicht** live ist
(`app/camera_program.py`):

1. OBS wird gefragt, welche Kamera live ist. Heisst die Programmszene wie eine
   Kamera („Cam 1“, „Cam 2“), ist es diese. In jeder anderen Szene zaehlt jede
   sichtbare Kamera-Quelle („Cam1_klein“, auch in verschachtelten Szenen).
   Ist OBS nicht erreichbar, bewegt sich keine Kamera.
2. Die Kamera, die nicht live ist, faehrt ihr Preset an.
3. Nach der Wartezeit schaltet OBS auf sie um (wie die Taste „Cam N Live“).
   Hat inzwischen jemand OBS umgeschaltet, passiert nichts mehr.
4. Nach der Ueberblendung faehrt die vorher aktive Kamera ihr Preset an; auf
   sie wird **nicht** umgeschaltet, das macht der Bediener von Hand.

Sonderfaelle: Ist keine Kamera live (Titel, PC, Beamer), fahren beide sofort
an und es wird nicht umgeschaltet. Ein Preset nur fuer die Kamera, die live ist,
wird uebersprungen; ebenso alles, wenn beide live sind (Bild im Bild). Der
naechste Programmpunkt bricht ein noch wartendes Umschalten ab. Die Meldung
nach dem Start nennt den ersten Schritt, das Ergebnis des Umschaltens folgt
als zweite Meldung (`GET /program/status` → `camera`).

Die Wartezeit ist fest: Ob eine Kamera ihr Ziel erreicht hat, meldet VISCA
nicht zuverlaessig. Langsame Preset-Fahrten brauchen einen groesseren Wert.

## Programm-Vorlagen

Ein Gottesdienst sieht meist gleich aus: **Programm → Vorlagen… → Speichern**
legt das aktuelle Programm unter einem Namen ab (`program_templates.json`). Pro
Zeile gibt es den Schalter **Liedplatz**: dort kommen beim Anwenden die Lieder
des Tages hinein (vorbelegt für Zeilen mit Lied oder mit „Lied“/„Lobpreis“ im
Titel). Ein Lied in einer Zeile ohne Liedplatz bleibt fest in der Vorlage.

**Anwenden**: Vorlage wählen, Lieder eintragen, entweder als Text (ein Lied pro
Zeile, Titel oder Liederbuch-Nummer wie `GHS 20`, Nummerierung wie `1.` wird
ignoriert) oder einzeln über **Lied aus der Bibliothek**. Jede Zeile wird in der
Bibliothek gesucht; ist sie nicht eindeutig oder fehlt sie, gibt es **Wählen**
(Vorschläge zuerst). Die Lieder füllen die Liedplätze der Reihe nach; mehr Lieder
als Liedplätze werden direkt nach dem letzten Liedplatz angehängt (als Kopie
dieser Zeile), übrige Liedplätze bleiben ohne Lied, ein Lied, das nicht in der
Bibliothek ist, steht als Text im Titel. Die Vorschau zeigt das Programm genau so,
wie es danach aussieht. **Programm ersetzen** (zweimal tippen) ersetzt das ganze
Programm und setzt den Ablauf zurück.

| Methode | Pfad | |
|---|---|---|
| GET | `/program/templates` | Vorlagen |
| POST | `/program/templates` `{"name", "slots", "replace"}` | aktuelles Programm speichern (`slots`: IDs der Liedplatz-Zeilen) |
| DELETE | `/program/templates/{id}` | Vorlage löschen |
| POST | `/program/templates/{id}/apply` `{"songs": [{"song_id", "text"}], "dry_run"}` | anwenden (`dry_run`: nur Vorschau) |
| POST | `/songs/match` `{"lines"}` | Liedzeilen in der Bibliothek suchen |

## Licht

Die Section **Licht** steuert das ganze Licht; in der Steuerung gibt es die
Kategorien Licht und Mixer nicht mehr (Katalog v8 räumt sie aus, eigene Tasten
darin bleiben). Programmzeilen, Overall-Makros und eigene Tasten, die diese
Tasten per ID nutzen, funktionieren weiter; Companion/Stream Deck kann sie auch
weiter über `/companion/buttons/{id}/press` drücken.

- **Bühne** (Art-Net, 4 Zonen): je Zone An/Aus und ein Regler 0–100 %, dazu
  Warm und Kalt für alle Zonen; *Alle an* (auf die Einschalthelligkeit, eine
  schon leuchtende Zone bleibt) und *Alle aus*. Beim Ziehen folgt das Licht
  direkt, Tasten und Szenen blenden mit dem eingestellten Übergang.
- **Raumlicht** (Shelly, 7 Kreise): An/Aus je Kreis mit dem Zustand, den der
  Shelly meldet (alle paar Sekunden gefragt); ein Kreis, der nicht antwortet,
  zeigt „?“.
- **Einstellungen**: Einschalthelligkeit, Übergang (0–10 s) und Dimmschritt
  (für die Heller/Dunkler-Tasten über Companion).
- **Szenen**: *+ Szene* speichert das Licht, wie es gerade ist (Bühne, Warm/Kalt,
  alle Raumlicht-Kreise mit bekanntem Zustand). Antippen blendet die Szene ein.

Im **Programm** hat jede Zeile die Spalte *Licht*: eine Szene, die beim Start
der Zeile eingeblendet wird (vor Tasten und Lied). Wird die Szene gelöscht,
startet die Zeile nicht mehr („unknown light scene“), bis eine andere gewählt ist.

| Methode | Pfad | |
|---|---|---|
| GET | `/lights` | Bühne, Einstellungen, Raumlicht-Kreise, Szenen |
| POST | `/lights/stage` `{"zones": {"left": 0..255}, "warm", "cold", "fade"}` | Bühne setzen (`fade: false` springt) |
| PUT | `/lights/settings` `{"start", "steps", "fade_ms"}` | Einstellungen |
| POST | `/lights/circuits` `{"states": {"light-front": true}}` | Raumlicht schalten |
| GET | `/lights/scenes` | nur die Szenen |
| POST | `/lights/scenes` `{"name", "replace"}` | aktuelles Licht als Szene speichern |
| POST | `/lights/scenes/{id}/apply` | Szene einblenden |
| DELETE | `/lights/scenes/{id}` | Szene löschen |

## Configuration (`config.yaml`)

- `mixer.ip` / `mixer.port` / `mixer.max_channels` (input channels, default 16)
- `mixer.mode`: `direct` (default and recommended; state dump every 10 s) or
  `proxy`. Any other value stops the service from starting.
- `mixer.buses`: optional, pins a bus to a channel number/name (the others are
  discovered from the mixer). Bus channels must be above `max_channels` and
  unique.
- `safety.freshness_mode`: `latest` (default), `session` or `age`, see
  [Freshness model](#freshness-model)
- `safety.max_fader_age_seconds`: optional age cap. Leave it `null` in direct
  mode: the dump only arrives every 10 s, so a short cap blocks almost every
  send between dumps.
- `safety.allow_partial_groups`: keep `false`
- `safety.group_send_delay_ms`: delay between per-channel sends in a group
- `groups`: named channel lists (only the seed for `groups.json`)
- `server.allowed_hosts`: host names with a dot under which the page may be
  opened (for example `mixer.gemeinde.de`). IP addresses, `localhost`, names
  without a dot and `.local`/`.lan`/`.fritz.box`/... always work; any other
  name gets 403, so a web page elsewhere cannot reach the service through
  its own domain (DNS rebinding). Changing requests from a browser must also
  come from this page itself (same Origin); curl, Stream Deck and Companion
  send no Origin and are not affected.

## Connection modes

**Direct** (default): the service opens its own TCP connection to the mixer,
next to the iPad app (the mixer accepts several clients). Its subscribe
handshake, repeated as a keepalive every 10 s, makes the mixer answer with a
full state dump, so every channel's fader and mute state is known about 0.1 s
after connecting and refreshed every 10 s. If nothing arrives for 2.5
keepalive periods the link counts as dead: sends are refused ("no data from
mixer (link stale)") and the service reconnects.

**Proxy**: the iPad app connects to this host (`listen_host:listen_port`) and
the service relays everything to the real mixer, parsing every frame both ways
and injecting its own commands on the same link (only between complete app
frames; otherwise "mixer link busy (app frame in progress)").

```
iPad app --TCP--> [ this service :6400 ] --TCP--> Mixer (192.168.188.30:6400)
                         |  parses every frame both ways
Stream Deck --HTTP-->----'  injects safe mute commands on the same link
```

Caveat: the mixer does not echo a client's own commands, so the iPad never
learns about commands the service injected on its connection. Its display
stays stale, and its next touch on that channel can resend the old mute state.
Prefer direct mode.

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
  as the object holding it; buses are discovered automatically (see below).
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
24 Bus 8, then 25 for the main L/R output). The main L/R output (id 255,
"Saal L/R MIX OUTPUT") is a channel with its own fader and mute like any bus,
but because a mistake there silences the whole room, **groups never switch it**:
not on mute, unmute or toggle (a member is reported as `protected`), not in the
solo-on-unmute (reported in `protected_others`), and "group from current status"
leaves it out. Only its own channel controls (UI strip, `/channels/<n>/…`, a
channel key) send to it. Its command frame (`72 … 08 ff01 [18 01] 3d <fader>`)
follows the bus format verified on the other buses; the main output itself was
not switched live before this was added, so test it once on the console before
a service. Built commands are
byte-identical to what the iPad app sends. Channel labels (`name`) flow
through `/state` to the UI.

## Freshness model

A stored value may only be sent if it was **observed on the current mixer
link**: after a reconnect, nothing from before counts until the mixer reports
it again (in direct mode: the dump right after connecting).
`safety.freshness_mode` adds:

- `latest` (default): any age on the current link.
- `session`: the same as `latest` (kept for older configs).
- `age`: additionally newer than `max_fader_age_seconds` (1 s if unset).

In all modes a value must have been **observed** (never a default/dummy), the
fader hex must be valid (finite, 0..1000), the link must be live and the
service not in panic. Commands are serialised, so two requests for the same
channel can never both act on the same old state.

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
- `app/mixer_client.py` — direct link (default): persistent TCP connection,
  subscribe/keepalive, liveness check, reconnect with backoff (1/2/5/10s), sender.
- `app/proxy.py` — transparent TCP proxy (`mode: proxy`): relays iPad<->mixer
  traffic, parses every frame into state, injects safe commands between frames.
- `app/safety.py` — per-channel and per-group safety checks.
- `app/controller.py` — the only sanctioned send path (state → safety → build →
  send → log), serialised by one command lock.
- `app/groups.py` / `app/buses.py` — mute groups (`groups.json`) and discovered
  buses (`buses.json`).
- `app/jsonstore.py` — atomic writes for all JSON files; an unreadable file is
  kept as `<name>.corrupt` instead of being overwritten.
- `app/native_controls.py` / `app/companion_buttons.py` — Steuerung keys (lights,
  cameras, OBS, beamer) and their catalog.
- `app/api/` — the HTTP routes, one module per area (`mixer`, `groups`,
  `program`, `controls`, `desktop`, `power`, `cameras`); handlers stay thin.
- `app/actions.py` — runs a key, desktop profile or file by its ID; Steuerung,
  the camera section and Programm rows all use it.
- `app/display_agent.py` — the one client for the display agents (port 8766);
  the agent token is only ever sent to LAN addresses.
- `app/responses.py` — answer shapes: every refusal is `{"ok": false, "reason": ...}`
  with 400/404/409/502/503.
- `app/request_guard.py` — refuses requests from other web sites (Origin) and
  foreign host names (DNS rebinding); extra names via `server.allowed_hosts`.
- `app/main.py` — app setup, start order of the features, static UI.
- `app/static/` — the web control panel, no build step: `index.html` (markup),
  `css/app.css`, and `js/*.js` as classic scripts that share one global scope
  and load in the order listed in `index.html` (`core.js` first with `api()`,
  `toast()` and the shared state, `main.js` last, which starts the polls).
  A function a file calls while it loads must be in that file or an earlier
  one. The release build writes its id into `<meta name="mixer-build">` and
  the `?v=` of every script and stylesheet.
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
