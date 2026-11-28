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

For trying the UI on a development machine, never start the service plainly
(its device code has the real LAN hosts built in). Use the local test runner:

```bash
python -m tools.run_local_test     # http://127.0.0.1:8770
```

It starts `tools/fake_mixer.py` (answers like the console: state dump on the
handshake, commands change its state, no echo), replaces every device
(Shellys, Art-Net, beamer, VISCA, OBS, display agents) with a fake that logs
"FAKE ..." lines, installs the tests' network guard (any non-loopback
connection raises) and keeps its data in `local-test/` (ignored by git;
delete it to start fresh). `--dead-shelly light-front` plays a room light circuit that
is off the network (each call hangs for the 1 s connect timeout, then fails), `--shelly-delay 0.3` slows
the other fake Shellys' answers, `--flaky-shelly light-left --flaky-rate 0.3`
makes one miss a share of the calls (a Shelly on poor Wi-Fi).

## Web UI

The service serves a single-page control panel at `/`, built for a 13-inch
iPad in Safari (both orientations) and desktop browsers alike. Every section
head (Desktop, Channels, Mute Gruppen, Programm, Liedanzeige, Steuerung) folds its section
in and out; the choice is remembered per device. **Anordnen** (switch in the
header) turns the page into an editable gallery: you set only the order of the
sections and a width for each (⅓, ½, ⅔ or whole); every section then drops
into the place of its width where it can start highest (masonry), so short
sections stack under each other next to a tall one. Every section head shows
⠿ (drag it to another place, finger or mouse), ◀ ▶ (earlier / later in the
order, as many steps as it takes for the page to change) and the width key
(⅓ → ½ → ⅔ → whole). Below 1100 px page width (iPad upright) ⅓ and ½ show as
half, ⅔ and whole as whole; below 900 px everything is one column. Outside
"Anordnen" a change of contents (program rows, song text, the Channels view,
opening or closing a section) never moves a section to other columns, so the
page stays calm during a service; only an arrange action, "Anordnen" itself
and the first full load of a view (places are saved once it has settled)
place sections anew. What is inside a section follows the section's own
width (CSS container queries), so ⅓ and ½ sections lay out their contents
like a narrow screen. The
arrangement is remembered per device, also sets the order of the Stage
miniatures, and **Standard** restores the original one.

- **Status bar**: mixer connection, send-enabled state, last-update age, the
  **Panic** button (disable/re-enable all sends) and **UPDATE**. Update does
  both: first the service from GitHub (Windows release only), then the Display
  Service (desktop agent) of the PC whose browser pressed it, which the service
  replaces with the agent files of its own release and restarts. So: Update on
  the streaming PC updates the service and its agent; Update on the display PC
  then finds the service current and only brings that PC's agent up to date.
  Agents from before this self-update need the one-time download the dialog
  offers (`update.cmd`); the key also shows when only the agent is behind.
  Quick keys left of it (the same actions as in Steuerung, which keeps its
  own): **Alles aus** (switches off what is selected under "Alles
  ausschalten", after the same question; usable exactly when that key is, and
  never next to Panic) and **Beamer Hide | Unhide** (the idempotent keys; the
  lit one is the remembered state, both locked while it is unknown, an amber
  snowflake while the beamer is frozen). Below 1300 px they show icons only and
  the status display stacks the data age above the version, so the header
  stays one row down to iPad portrait (1024 px) with UPDATE showing.
- **Stage** switch (next to the title, remembered per device): like Stage
  Manager, one section fills the screen and the others sit as live miniatures
  on the left; a tap on one flies it onto the stage. The page itself never
  scrolls. The miniatures share the left rail's full height; at 40 px they
  stop shrinking and the rail scrolls instead (finger or mouse), so a low
  window never cuts one off. Programm and the full-size channel strips (300 px faders, as many
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
  per-channel fader presets (**Use** / **Use All**). **Aus Pult-Status…**
  creates a group from the channels currently unmuted, with their observed
  fader values as presets. Each group is one line (display, Mute, Solo, "⋮");
  tapping the display shows its channels (Set / Use and the fader). The "⋮"
  menu has Kanäle bearbeiten (channel descriptions, remove and add channels; "Fertig"
  ends it), Umbenennen, Use All, Kanal-Einstellungen, Duplizieren, Löschen.
- **Kanal-Einstellungen** (in the "⋮" menu of each mute group): saves gain, high-pass,
  EQ, gate and compressor of the group's input channels as they are set on the
  console right now, and shows them per channel. Read-only: the values come
  from the mixer's state dump and nothing is sent back (buses are not
  included). Stored in `channel_settings.json`, raw as the console sent them.
- **Programm**: a cue list with planned time, title, person, mute group
  (optional: a row without one changes no mutes), light scene, a file each
  for playback and translation, for lyrics a song *or* a file (small keys
  "Lied" / "Datei", ✕ removes it; a row saved with both keeps the song),
  camera presets with an OBS scene per camera, desktop profiles (only the
  chosen ones show, numbered in the order they run, ✕ removes one, ‹ moves
  one earlier, **+** adds another at the end), and the beamer (hide before
  the row shows anything, unhide after its files, desktop profiles and
  song; "Beamer bleibt" leaves it alone). A row always runs its own fields.
  **Shortcut laden** (row's "⋮"; a new row has the key next to ✓) copies a
  shortcut's fields into them: a copy, they stay visible and can be
  changed, and later edits of the shortcut change no row. Time, title,
  person and song stay the row's; with a song the shortcut's Liedtext file
  is left out. Rows and templates saved while rows could refer to a
  shortcut (v1.2.1-210 and before) get that shortcut's fields copied in
  once when the service starts (logged as warnings, a deleted shortcut is
  just dropped); an imported program file of that time too. A
  choice with only one entry (e.g. one light scene) is a key that
  switches it on and off instead of a dropdown (not the OBS scenes).
  Each row is one compact line (time, delay, title, person, chips of what it
  does, Play / hide / "⋮"); tapping the line opens its fields for editing
  (one row at a time, Esc or another tap closes it).
  Play activates the group first, then runs
  the rest **best effort per area**: the light, the cameras and the content
  (beamer, the file / key fields, the desktop profiles, the song) side by
  side, each on its own, so a light that does not answer or a mixer that is
  not connected costs only that part (steps on the same PC keep their
  order; other PCs do not wait for one that is off). The mixer part stays
  all or nothing: a blocked group sends nothing to the mixer. A key, group,
  profile or light scene deleted since only skips its own part. The row
  counts as started in any case; the answer lists every area
  (`areas: {mixer: {ok, reason}, …}`, `ok`, `partial`) and the page shows
  one short line for what did not work ("„Predigt“ gestartet – Mischpult:
  nicht verbunden; Licht: keine Antwort"). The delay records the click
  offset from the planned time. As soon as a row starts (Play, **Weiter**), the desktop
  profiles the rows before started stop, unless the new row starts them
  itself (app/profile_stops.py): music fades out with its profile's fade,
  a file's window (e.g. the Mission Spotlight video) closes and an image is
  hidden (agent `POST /close`; only windows of the process the agent
  started itself: a file handed to an app that was already running, e.g.
  a single-instance player, cannot be closed and is logged). A profile the
  new row starts again is sent again, and the agent decides: music that
  still plays and a video window an earlier row opened that is still open
  stay as they are (no new fade-in, no reopened video), music that ended
  starts again, an image is shown again; a window left open by hand is
  opened again by the row. A start the agent did not answer in time is
  stopped later by its profile alone. Music is never stopped on a PC where the new row starts
  audio itself (a profile or a file). Every start carries a stamp and a
  stop names the start it means, so the agent never lets a late stop end
  a newer start; no start waits for a stop. What the operator started by hand (a Desktop key,
  a shortcut key) keeps running, also a profile the program started whose
  Desktop key was used by hand since. Programs (e.g. OBS),
  song windows, files of the row's file fields, the beamer and the light
  are left alone. The stops run in the background with short timeouts; an
  agent that does not answer is skipped for 30 s and tried again on the
  next row. After a restart nothing counts as started by the program. The cue bar shows the clock, the next row and how long until
  it is due; **Weiter** starts the next row (also from a Stream Deck via
  `POST /program/next`). The row that ran last today is marked "Läuft", the
  next one "Nächster", earlier ones are dimmed. **Duplizieren** puts the copy
  right below its original, **Ablauf zurücksetzen** (the section head's "⋮",
  with JSON export and import) forgets all start times.
  **Als Shortcut speichern** (row's "⋮") copies what the row does into a
  new shortcut (`shortcuts.json`). Shortcuts are keys in the Desktop
  section: a tap runs them without touching the program's order or start
  times; their "⋮" edits all their assignments (same fields as a row),
  duplicates or deletes them; **+** makes a new one.
  The eye key top right of a row hides it: the row turns grey and stays in
  the list, but **Weiter** skips it (its own Play still works); tap it again
  to show the row. Times can be typed as `930`, `9:30` or `9.30`.
- **Steuerung**: the keys imported from the former Companion setup (lights,
  OBS, beamer, "Overall" scene macros), grouped in categories; the camera
  keys live in the Kameras section. With
  **Bearbeiten** switched on, every key, category and *Alles ausschalten*
  target shows a "⋮" (Umbenennen; keys also Nach „…“ verschieben and Löschen),
  and a tap on a key opens its menu instead of pressing it.

**One pattern for editing:** everything that can be renamed, moved or deleted
has one "⋮" key (desktop keys, shortcuts, program rows, mute groups, light
presets, cameras, templates, Steuerung in Bearbeiten); the menu lists what can
be done, deleting asks once at the "⋮". A tap outside an open menu only closes
it. Section heads show their tools with an icon (the main "+" lit blue), rare
ones sit in the head's "⋮".

The page polls `/state`, `/health` and `/groups` about once per second.

## Beamer Hide / Unhide

The Beamer category has separate **Beamer hide** and **Beamer unhide** actions.
Because the projector command is a toggle, first set the actual current mode in
the **Beamerzustand** selector. This only records the current state; it does not
send a projector command. A Hide/Unhide action sends one `avmute=AV Mute`
HTTP form POST only when its target differs from the recorded state. The state is persisted in
`native_control_state.json`. The projector starts un-hidden, so on every service
start and after **Beamer on** or **Beamer off** (also through **Alles
ausschalten**) the recorded state is set to **Unhide**, without sending anything
to the projector. It becomes **unknown** only when an AV Mute toggle got no
answer; Hide and Unhide then stay blocked until the operator declares the
current mode. If the projector is toggled by another device or its remote,
update the selector before using the web controls.
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
it does not send a command. Service start, **Beamer on** and **Beamer off** set
it to Unhide (nothing is sent). While it is unknown (a toggle without an
answer), **Beamer hide** and **Beamer unhide** stay blocked until the operator
sets it. Each action sends an
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
raw HEX are not accepted.

"Overall" keys run as they did in Companion (catalog version 9, table in
`app/companion_buttons.py`):

1. the mute group (unmuted exclusively); a blocked group stops the key before
   anything else changes;
2. the four stage zones go to the key's pattern (e.g. Predigt: Links and
   Rechts on, Klavier and Logo off); a zone that is already on keeps its
   level, one that comes on gets the start brightness; the light fades while
   the next steps run;
3. only the camera that is off air drives to its preset; 4 s later it goes
   live, then the other camera follows to its preset (the same sequence as
   program rows, `app/camera_program.py`). With no camera on air both move and
   Cam 1 goes live after the wait; if OBS was switched meanwhile (in OBS or
   with an OBS key here), nothing is cut. The key pressed last wins: a second
   Overall or an OBS key pressed right after an Overall stops its cameras,
   even while its mute group is still being set;
4. their OBS key, if any (Mission Video: PC2, Ende: the end scene).

Every part is tried even if another fails; the answer (and the toast) names
what failed. "Licht AN/AUS" leave the Technik circuit alone, as in Companion,
and switch all other circuits at once. "Cam N Live" makes the camera's source
visible in its own scene and puts that scene on air; a scene without the
source no longer stops the switch; the page then shows a warning (renamed
source?).

Catalog version 10 keeps each OBS key once: the duplicates of Companion's
first page (Cam 2 Live twice, PC2 Live three times, Start/Ende twice) left
Steuerung; program rows, shortcuts and own keys that name an old ID
(`BUTTON_ALIASES`) run the native key. "Übergang Langsamer/Schneller" set
OBS's transition time in 50 ms steps (50 ms to 20 s); the keys show the
current value. As on the Companion buttons, a key whose scene is on air in
OBS is red, one in the preview (studio mode) green. The page asks
`GET /obs/status` every 1.5 s; one OBS query serves every page for a
second, and while OBS does not answer no key is coloured and OBS is asked
only every 5 s.

"Bibel Cam1 Beamer" / "Bibel Cam2 Beamer" (Companion page 7) show or hide
the Bible text (OBS source "BibleShow") in the camera's scene and put that
camera on air, then the other camera in the preview (studio mode). While the
text shows, the projector is hidden; hiding the text brings the projector
back (Companion ran its "Beamer hide" script on every press). The key is red
while the text shows in the scene on air, green while it shows in a scene
not on air (`overlays` in `GET /obs/status`).

The service keeps ONE WebSocket connection to OBS open (`app/obs_client.py`)
for every key, program row, camera sequence and status query; OBS no longer
reports a new client and a disconnect on every click. Requests from several
places at once share it (answers matched by requestId), each waits at most
3 s in all, connecting included (a connect runs in its own thread; callers
only wait for it, and stopping the service does not), and a request without
an answer drops the connection so the next one connects anew. When OBS is gone, connecting is retried after 1, 2, 4 ... up
to 15 s; meanwhile requests fail at once with "OBS WebSocket unavailable".
The connection closes when the service stops.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| GET  | `/companion/buttons` | list imported controls |
| GET  | `/obs/status` | OBS program and preview scene, studio mode, transition (cached 1 s) |
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
| POST | `/program/items/{item_id}/shortcut` | save what the row does as a new shortcut |
| GET/POST | `/shortcuts` | list shortcuts / create one (`title` and the row's action fields) |
| PUT/DELETE | `/shortcuts/{id}` | edit / delete (program rows keep what they loaded from it) |
| POST | `/shortcuts/{id}/duplicate`, `/shortcuts/{id}/play` | copy ("… (Kopie)") / run now |
| GET  | `/program/obs-scenes` | OBS's scenes for a row's OBS scene (503 if OBS is not reachable) |
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
| POST | `/desktop/profiles/{id}/duplicate` | copy a profile ("… (Kopie)", new id), placed right after it |
| DELETE | `/desktop/profiles/{id}` | delete a profile |
| POST | `/desktop/{id}/open` | show/open the profile on its target PC |
| GET  | `/desktop/agent/update.cmd` | one-click agent update (first install, agents without self-update) |
| GET  | `/update/status` | installed release, update progress and log tail |
| POST | `/update/start` | start the GitHub update (Windows release only) |
| GET  | `/update/agent` | the display agent of the requesting PC: present, version, current |
| POST | `/update/agent` | push this release's agent files to it; waits until it is back |
| GET  | `/update/agents` | the same for every PC the service sends to (Ziel-PCs, desktop profiles, song display, this PC) |
| POST | `/update/agents` | start every agent again, all at once (the Update key uses this): one that is behind through its self-update, a current one through a plain restart; per PC `updated`, `restarted`, `self_update: false` (too old) or `present: false` (not reachable) |
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

Jede Preset-Taste zeigt ihre Nummer vor dem Namen („3 · Kanzel“), ebenso die
Preset-Auswahl der Programmzeilen. Rechts neben „Presets“ fährt
**Nr. [ ] Anfahren** jede Preset-Nummer 1 bis 16 an, auch eine ohne Namen in
der App (`POST /cameras/{cam1|cam2}/preset/recall` mit `{"preset": 11}`;
derselbe Weg wie eine Preset-Taste). „⋮“ der Kamera → **Aktuelle Position
speichern** fragt Nummer und Name; vorbelegt ist die kleinste Nummer, die in
der App keinen Namen hat und die nichts benutzt. Die Kamera speichert dort
ihre aktuelle Position (VISCA `81 01 04 3F 01 pp FF`), der Dienst den Namen
(`POST /cameras/{cam1|cam2}/presets` mit `{"preset", "name", "overwrite"}`).
Hat die Nummer schon einen Namen oder benutzt sie eine Programmzeile, ein
Shortcut, eine Vorlage oder eine Taste der Steuerung (z. B. eine
Overall-Taste), antwortet der Dienst ohne `"overwrite": true` mit 409, dem
Namen und der Liste `users`, und die Seite fragt, ob überschrieben werden
soll (`GET /cameras/{cam1|cam2}/presets/usage` liefert diese Liste für alle
Nummern). „In der App ohne Namen“ heißt nicht, dass die Kamera dort nichts
gespeichert hat (Companion, Fernbedienung). Zwei gleichzeitige Speichervorgänge
derselben Kamera laufen nacheinander. Nummern ohne Katalog-Taste stehen als
`saved_camera_presets` in `companion_buttons.json`.

Die Kamera-Tasten (Presets, Zoom) stehen seit Katalog-Version 7 nicht mehr in
der Steuerung; eine bestehende `companion_buttons.json` wird beim Start
umgestellt (umbenannte Presets behalten ihre Beschriftung im Kamera-Bereich).
Programmpunkte rufen die Presets weiter per ID auf; die Overall-Tasten nennen
ihre Presets selbst (seit Katalog-Version 9).

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
`program`, `file` oder `audio`. Im Profilfenster wird die Art nicht mehr
gewählt: der Dienst leitet sie beim Speichern aus dem Pfad ab
(`app/profile_kind.py`), das Fenster zeigt sie vorher als „Erkannt: …“.
Bildendungen, die der Agent zeigen kann (PNG, JPG, GIF, BMP, WebP, TIFF, ICO;
nicht SVG und HEIC), ergeben `image`, Audioendungen und YouTube-Links `audio`
(Groß-/Kleinschreibung von Schema und Host egal, gespeichert wird der Link
klein), `.exe` `program`, alles andere `file` (Video, PDF, Folien, `.url` …;
Windows öffnet die Datei mit der Standard-App, `.lnk`, `.bat` und `.cmd` führt
es aus). Ein Pfad ohne Endung (z. B.
`…\obs64*`) und ein Mission-Spotlight-Ordner sagen nichts: ein bestehendes
Profil behält dann seine Art (Spotlight-Audio bleibt Audio, ein Programm wird
mit Spotlight zu `file`), ein neues wird `file`. Ein
bestehendes Profil behält seine gespeicherte Art, solange Pfad und Mission
Spotlight gleich bleiben; ein `kind` von einer älteren Seite zählt nur bei
einem Pfad ohne Endung. Bei `program` muss `path` auf eine `.exe` zeigen;
`args` ist eine Liste separater Argumente, die ohne Shell gestartet werden.
Bei `file` oeffnet Windows die Datei mit der Standardanwendung. `monitor` ist
1-basiert; `fullscreen` aktiviert fuer Bilder bzw. Programmfenster den
Vollbildmodus. Bei `fullscreen: false` legt `position` die Ausrichtung des
Fensters fest: `top-left` (Standard), `top-right`, `bottom-left`,
`bottom-right` oder `center`. Ein Bild in einer Ecke fuellt wie beim Einrasten
in Windows ein Viertel des Arbeitsbereichs (ohne Taskleiste); bei `center`
richtet sich das Fenster nach dem Bild. Nach Aenderung der Konfiguration von Hand den
Service neu starten.

**Mission Spotlight** (Schalter im Profil, `mission_spotlight: true`; fuer Bild,
Datei und Audio): `path` ist dann ein Ordner. Beim Ausloesen nimmt der Service
Jahr und Quartal des heutigen Datums und die Woche darin (Tag 1–7 des Quartals =
Woche 01, Tag 8–14 = Woche 02 …) und schickt `<Ordner>\<JJJJ>_<Q>.Quartal\<WW>*`; der
Agent nimmt die Datei, deren Name mit der zweistelligen Woche beginnt, z. B.
`I:\Mission Spotlight\2026_3.Quartal\01_Testvideo.mp4` (bei mehreren die neueste;
Ordner ohne Jahr, z. B. `3.Quartal`, werden nicht mehr beachtet).
Das Profilfenster zeigt das aktuelle Jahr, Quartal und die Woche.
Videos (`.mp4`, `.mov`, `.mkv`, …; ein Datei-Profil wie Mission Spotlight
oder eine Datei im Programmpunkt) spielt der Agent selbst in **mpv**, wenn er
dort eine echte `mpv.exe` findet (nicht MPV.NET, keine Store-App): Vollbild per
mpv (`--fs`) auf dem eingestellten Monitor (Monitor N wie bei Bildern; mpv
bekommt dessen Nummer in Windows' eigener Reihenfolge, `--screen` /
`--fs-screen`, denn `--screen-name` kennt mpv unter Windows nicht). Am Ende bleibt
das letzte Bild stehen, bis der nächste Programmpunkt es schließt. So kennt der
Agent das Fenster, schließt es, wenn das Programm weitergeht, und startet nie
zwei Player für dasselbe Profil. Befehlszeile und Monitor stehen im Agent-Log.
Ohne mpv (oder mit einem älteren Agent) öffnet die Standard-App das Video wie bisher.

**Platzhalter `*` in Pfaden:** In allen Pfaden (Profile, Datei-Aktionen im
Programm, Liederbibliothek) steht `*` fuer einen unbekannten Teil eines Namens,
auch eines Ordners, z. B. `I:\Musik\Ansage_*.mp3`,
`C:\Program Files\obs-studio*\bin\64bit\obs64.exe` oder
`C:\Users\*\foilpresenter Bibliothek`. Der Display-Agent setzt ihn auf dem
Ziel-PC ein; passen mehrere Eintraege, gilt der zuletzt geaenderte. Ein
Programmpfad schreibt den Platzhalter vor der Endung (`obs64*.exe`), sonst wird
er nicht als Programm erkannt; ein älteres Profil, das auf `*` endet, bleibt
Programm, dann kommen nur `.exe`-Dateien in Frage. Programme auf Netzlaufwerken bleiben auch mit `*`
gesperrt. Der Platzhalter braucht den aktuellen Display-Agent auf dem Ziel-PC;
die Update-Taste aktualisiert die Display-Agents aller PCs mit und startet sie
immer neu, auch wenn sie schon aktuell sind (dann über `POST /restart` am
Agenten, auf demselben Weg wie nach einem Update). Findet der
Agent keine passende Datei, nennt die Meldung Muster und Ordner.

Ein `audio`-Profil enthaelt den Pfad zu einer Musikdatei auf dem Ziel-PC. Dort
muss mpv installiert sein. Der Agent sucht es in dieser Reihenfolge: die
Variable `MPV_PATH`, `PATH` (auch der Benutzer- und System-PATH aus der
Registry), der Agent-Ordner, WinGet (Links und Pakete, Benutzer und System),
scoop, Chocolatey, `Program Files`, `Program Files (x86)` und
`%LOCALAPPDATA%\Programs` (dort auch Ordner, deren Name „mpv“ enthaelt: winget
`shinchiro.mpv` installiert nach `C:\Program Files\MPV Player` und setzt kein
PATH) und der Installationsordner, den ein Installer in der Registry eintraegt
(Programme und Features, Name mit „mpv“, ohne MPV.NET). Ein echtes `mpv.exe` geht immer vor MPV.NET (`mpvnet.exe`, auch wenn
`MPV_PATH` darauf zeigt): MPV.NET startet langsam, und sein Steuerkanal
antwortet nicht; es wird nur genommen, wenn sonst nichts da ist. Dasselbe gilt
fuer jedes Programm unter einem `WindowsApps`-Ordner (Store-Apps und ihre
Aufruf-Aliasse). Die Registry wird in der 64- und der 32-Bit-Ansicht gelesen,
`%ProgramFiles%` in den Eintraegen aufgeloest; `mpv_search` nennt sie immer,
auch wenn kein Eintrag passt. Statt
`mpv.com` (Konsolen-Huelle) wird das `mpv.exe` daneben gestartet. yt-dlp (fuer
YouTube-Links) genauso, zuerst neben mpv. Ein Play nimmt den Pfad der letzten
Suche im Hintergrund (hoechstens 5 Minuten alt, Datei noch vorhanden,
`MPV_PATH`/`YTDLP_PATH` unveraendert, kein MPV.NET und keine Store-App) und
sucht nur sonst neu, damit mpv ohne Ordnersuche startet. Startet der Agent ohne die Umgebung des Benutzers (nach dem Selbst-Update
startet ihn Windows ueber WMI neu), uebernimmt er fehlende Benutzer- und
System-Variablen und PATH-Eintraege beim Start aus der Registry. Findet er mpv
nicht, nennt die Meldung die durchsuchten Orte; der Update-Dialog zeigt pro PC
„mpv nicht gefunden“, und `GET /update/agents` liefert pro Agent `mpv`, `ytdlp`
(Pfad oder `null`), `restored_env` (beim Start ergaenzte Variablen),
`mpv_search` (jeder durchsuchte Ort mit dem Fund) und `agent_user` /
`agent_localappdata` (das Windows-Konto, unter dem der Agent laeuft: ein
Programm, das ein anderes Konto nur fuer sich installiert hat, findet er nicht). Die Audio-Kachel spielt den Titel ohne Player-Fenster ab; ein
erneuter Play-Klick ersetzt die laufende, vom Agenten gestartete Wiedergabe.
Stop beendet nur diesen Agenten-Player, keine anderen mpv-Instanzen. Der Agent
laeuft im angemeldeten Benutzerkonto, sodass die normale Windows-Audioausgabe
des Ziel-PCs verwendet wird. Bei Netzwerkdateien muss der Pfad fuer dieses
Benutzerkonto erreichbar sein; ein UNC-Pfad ist robuster als ein verbundenes
Laufwerk.

Damit die Musik sofort startet, spielt der Agent eine lokale Kopie der Quelle
(`%LOCALAPPDATA%\MixerMuteDisplayAgent\audio-cache`, hoechstens 4 GB, je Datei
hoechstens 1 GB, nur Audiodateien: mp3, wav, flac, m4a, aac, ogg, opus, wma;
es bleiben immer mindestens 2 GB frei). Auf einem Google-Drive-Laufwerk
(`I:\GoogleDrive`) laedt Drive eine Datei erst beim Oeffnen, mpv kann darauf
warten. Bei YouTube-Links laedt yt-dlp den Ton einmal herunter. Die Kopien
entstehen im Hintergrund: fuer die Audioquellen, die der Dienst bei jeder
Abfrage von `/update/agents` mitschickt (jede offene Seite alle 30 s und bei
jedem Update; der Agent schaut sich jede Quelle hoechstens alle 5 Minuten an
und versucht eine fehlgeschlagene erst nach einer wachsenden Pause wieder,
5 Minuten bis 1 Stunde), beim Start des Agenten fuer die zuletzt geschickte
Liste, und nach einem Play ohne Kopie (das spielt dann noch direkt von der
Quelle). Eine geaenderte Datei (Groesse oder Aenderungszeit) spielt nie die
alte Kopie. Wo mpvs letzter Start
Zeit gebraucht hat, liefert `GET /update/agents` pro Agent unter `audio_start`
(aus mpvs eigenem Log: Sekunden bis Datei offen, Audioausgabe bereit, Ton
laeuft, und die laengsten Pausen bis zum ersten Ton mit den Logzeilen davor
und danach).

Beispiel fuer eine Systemvariable auf dem Ziel-PC:

```powershell
[Environment]::SetEnvironmentVariable("MPV_PATH", "C:\Tools\mpv\mpv.exe", "Machine")
```

Die Wiedergabesteuerung verwendet `POST /desktop/{profile_id}/audio/play` und
`POST /desktop/{profile_id}/audio/stop`.

**YouTube statt Datei**: Im Audio-Profil darf statt eines Pfades ein YouTube-Link
stehen (`youtube.com/watch?v=…`, `youtu.be/…`, `music.youtube.com`, Shorts,
Live). Der Link wird beim Eintippen erkannt, das Profil wird dabei automatisch
zu *Audio*. Der Agent spielt nur den Ton ohne Fenster (mpv holt ihn mit
yt-dlp), nur das eine Video (keine Playlist); Ein-/Ausblenden und Stop wie bei
einer Datei, das Einblenden beginnt erst, wenn der Ton wirklich läuft (Laden
dauert ein paar Sekunden). Andere Web-Adressen nimmt der Agent nicht an.
Dafür braucht der Ziel-PC zusätzlich **yt-dlp**: einmalig
`winget install yt-dlp.yt-dlp` (oder `yt-dlp.exe` neben `mpv.exe` legen,
oder die Variable `YTDLP_PATH`). Fehlt es, meldet „Play“
„yt-dlp was not found on this PC“. YouTube ändert sein Format öfter; spielt
ein Link nicht mehr, hilft meist `yt-dlp -U` (bzw. `winget upgrade yt-dlp.yt-dlp`).
Der Agent auf dem Ziel-PC braucht die neue Version („Display Service
herunterladen“ bzw. Update-Knopf).

**Ein-/Ausfaden:** Im Profil-Editor eines Audio-Profils schaltet
**Ein-/Ausfaden** sanftes Ein- und Ausblenden ein. Play blendet ueber die
eingestellte Dauer (Standard 5 s) **von 0 auf 100 %** ein; das macht mpv
selbst (Audiofilter `afade`) ab dem ersten Ton, die Musik startet also so
schnell wie ohne Fade. Stop blendet ueber mpvs Steuerkanal (IPC-Pipe) auf 0
aus und beendet erst dann den Player; antwortet der Steuerkanal nicht, endet
die Musik bei Stop sofort (im Agent-Log steht dann der Windows-Fehler).
Eine Lautstaerke-Einstellung gibt es nicht, das Mischpult wird nicht
angefasst. Die Lautstaerke im Saal stellst du am
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

**Wenn die Musik nicht startet:** Die Meldung nennt den Grund, z. B. „mpv ended
at once (exit code 2): Could not open audio device“ (kein Audiogerät, etwa
ein ausgeschalteter HDMI-Monitor). Früher scheiterte jedes Play mit Einblenden
auf dem Streaming-PC nach 10 s mit „mpv did not open its control channel“:
mpv spielte, aber seine Steuer-Pipe antwortete nicht. Seit das Einblenden
ohne Steuerkanal läuft, startet die Musik trotzdem. mpvs eigenes Protokoll des letzten Starts liegt auf dem Ziel-PC
unter `%LOCALAPPDATA%\MixerMuteDisplayAgent\mpv-last.log`.

Profile lassen sich auch in der Weboberflaeche anlegen (graue **+**-Kachel
hinter den Profilen) und ueber das Drei-Punkte-Menue bearbeiten, duplizieren
(die Kopie "… (Kopie)" steht direkt hinter dem Original) oder loeschen;
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

## Desktop-Schritte eines Programmpunkts

Was ein Programmpunkt zeigt und abspielt, steht in seiner Zeile *Desktop* als
Folge von Schritten, die in dieser Reihenfolge laufen (`app/row_files.py`,
`app/api/program.py`). Mit **+** (gestrichelt) kommt ein Schritt hinzu, mit **‹**
rückt er vor, **✕** entfernt ihn:

- **eine Desktop-Taste** – wie sie unter Desktop eingerichtet ist. Mit **Pfad…**
  zeigt sie stattdessen eine andere Datei: PC, Monitor(e), Vollbild, Position
  und Ein-/Ausblenden kommen von der Taste, die Datei vom Schritt; die Art
  (Bild, Musik, Programm, Datei) kommt aus dem Pfad. ✕ daneben nimmt wieder die
  Datei der Taste. Gespeichert als `via:<profil-id>|<pfad>`.
- **Beamer einfrieren** / **Beamer auftauen** (`@freeze` / `@unfreeze`) – Freeze
  ist am Beamer ein Umschalter; der Dienst merkt sich den Zustand und sendet nur,
  wenn er anders ist. Nach dem Start des Dienstes gilt er als *live* (nicht
  eingefroren, wie „Unhide“ beim Beamer-Zustand), ebenso nach Beamer an/aus;
  die Taste „Beamer freeze“ schaltet um. Unbekannt wird er nur, wenn ein Freeze
  vielleicht nicht ankam (keine Antwort vom Beamer): dann wird nichts gesendet
  (im Log, der Schritt meldet es, der Programmkopf zeigt „Freeze: unbekannt“) – im Steuerung-Bereich unter *Freeze-Zustand festlegen* setzen.
  Ist der Beamer am Gerät selbst eingefroren, dort ebenso. Aufgetaut wird nur durch
  einen Schritt *Beamer auftauen* oder die Taste. Solange er eingefroren ist,
  zeigen der Programmkopf („Beamer eingefroren“) und der Steuerung-Bereich das an.
- **Liedtext** (`@song`) – das oben gewählte Lied aus der Liedbibliothek, höchstens
  einmal. Wird ein Lied gewählt, kommt der Schritt ans Ende; wird es entfernt,
  geht er mit.

Ablauf: Mischpult, Licht und Kameras wie bisher; dann *Beamer* (hide / unhide,
vor allen Schritten), dann die Schritte. Schritte auf demselben PC laufen
nacheinander, ein Freeze/Auftauen nach allem davor und alles danach nach ihm;
Schritte auf anderen PCs warten nicht auf einen PC, der nicht antwortet. Ein
Schritt, der etwas zeigt, bekommt 2 s (`SETTLE_SECONDS`), bevor ein Freeze oder
das nächste Bild auf demselben PC kommt – außer der Agent meldet `"shown": true`
(er hat es schon gezeichnet). Ein Schritt, der nicht klappt, steht im Log und in
der Meldung; die folgenden laufen trotzdem. Nur ein Freeze direkt nach einem
gescheiterten Schritt entfällt (er würde das falsche Bild einfrieren).
Beispiel: Übersetzung (Taste Beamer + PDF) → Beamer einfrieren → Liedtext.

**Stopp** im Programmkopf beendet, was der Programmablauf gestartet hat (Musik
blendet aus, Fenster und Bilder gehen zu, `POST /program/stop`); von Hand
Gestartetes läuft weiter. Der Agent kennt einen Schritt mit anderer Datei als
`<profil-id>--<kurzer Hash des Pfads>`, so dass „offen lassen“ und das Beenden
beim nächsten Programmpunkt (`app/profile_stops.py`) Taste *und* Datei unterscheiden.
Im **laufenden** Programmpunkt trägt jeder Schritt, den der Ablauf gestartet
hat und der noch spielen oder zeigen kann (Musik, Video, Dokument, Bild), ein
kleines rotes Stopp-Quadrat auf seinem Chip: es beendet nur diesen Schritt
(Musik blendet mit der Ausblendzeit ihrer Taste aus, Fenster gehen zu), die
anderen laufen weiter (`POST /program/stop {"step": <Start-Id>}`; welche
Schritte das sind, steht in `GET /program/status` unter `running`).

Früher gab es eigene Felder *Playback*, *Übersetzung* und *Liedtext* (als Datei).
Beim Start des Dienstes (und bei Zeilen von älteren Seiten oder aus
Programmdateien) werden sie zu Schritten, in der Reihenfolge, in der sie liefen:
Playback, Übersetzung, Liedtext, dann die bisherigen Desktop-Profile, das Lied
als letzter Schritt. Eine Datei `file:<ip>|<pfad>` bekommt eine Desktop-Taste auf
demselben PC (am liebsten gleiche Art, Musik eine Taste ohne Einblenden, bei
Übersetzung Monitor 2; `127.0.0.1` ist der Streaming-PC; gibt es für Musik nur
eine Taste mit Einblenden, wird sie genommen und das steht im Log); gibt es dort keine, bleibt sie ein Datei-Schritt, läuft wie
bisher und zeigt „Desktop-Taste wählen“ (Auswahl direkt im Schritt).
`desktop:<id>` wird die Taste (außer sie ist schon ein Schritt), eine
Steuerung-Taste ein Schritt `key:<id>`. Das gilt auch für Vorlagen (eine
Lied-Lücke bekommt den Liedtext-Schritt) und Shortcuts und steht im Log. In
der Programmdatei steht eine selbst benannte Steuerung-Taste (Benutzer-Taste,
gespeichertes Kamera-Preset) als `key:<Name>` (wie Licht und Tasten sonst
auch; ist der Name nicht eindeutig oder sieht er wie eine ID aus, bleibt die
ID und der Export warnt), Desktop-Tasten und `via:<profil-id>|<pfad>` bleiben
wie sie sind; `desktop_names` nennt jeden Schritt nur zum Lesen. Der Import
sucht eine unbekannte Taste über ihren Namen (benannte Tasten zuerst), dann
über den Namen daneben.

**Dokumente (PDF, PowerPoint, Word) als Vollbild:** Zeigt ein Schritt eine
`.pdf`, `.ppt/.pptx/.pps/.ppsx/.odp` oder `.doc/.docx/.odt/.rtf` (die Datei
einer Desktop-Taste oder eine andere Datei über **Pfad…**), öffnet der
Display-Agent sie nicht mit der Standard-App, sondern zeigt die Seiten selbst,
in einem eigenen Fenster über Bild und Liedanzeige. Monitor(e), Vollbild und
Position kommen von der Taste, wie bei einem Bild (dieselbe Nummerierung):
ohne Vollbild ein Fenster im Arbeitsbereich (Mitte: der ganze, Ecke: ein
Viertel), mit mehreren Monitoren dieselbe Seite auf jedem. Der Dienst wartet
bis zu 20 s, bis die erste Seite steht (`"shown": true`); erst dann kommt ein
folgendes *Beamer einfrieren* (steht sie nicht rechtzeitig, entfällt es).
Ältere Datei-Schritte `file:<ip>|<pfad>` gehen auf den Monitor der Liedanzeige
dieses PCs (Anzeige-PC: der Beamer), sonst Monitor 1, immer im Vollbild. PDFs rendert der Agent mit `pypdfium2`; PowerPoint- und
Word-Dateien wandelt einmal das installierte Office (unsichtbar, per COM) oder
sonst LibreOffice in ein PDF um. Die Kopie liegt unter
`%LOCALAPPDATA%\MixerMuteDisplayAgent\documents`, ein zweites Öffnen geht sofort.
Animationen und Übergänge aus PowerPoint gibt es dabei nicht.
Blättern: `POST /documents/next` bzw. `prev`, `first`, `last` (optional
`{"pc": ip}`, sonst der PC, der zuletzt ein Dokument geöffnet hat),
`GET /documents/current` liefert Seite und Seitenzahl; am Anzeige-PC selbst
blättern Pfeiltasten, Bild auf/ab, Pos1/Ende und ein Presenter, Esc blendet
aus (Leertaste, Enter und Rücktaste blättern nie: Tippen an dem PC bleibt
harmlos). Den Tastaturfokus nimmt der Agent nur, wenn gerade kein anderes
Fenster ihn hat; ein Klick auf die Seite gibt ihn dem Dokument, ohne zu blättern.
Im laufenden Programmpunkt zeigt ein Streifen „‹ Seite“ / „Seite ›“ mit der
Seitenzahl. Eine Umwandlung endet nach spätestens gut drei Minuten; hängt
Office dabei, beendet der Agent genau die Office-Instanz, die er selbst
gestartet hat (nie ein vom Benutzer geöffnetes Office), und ein mit Kennwort
geschütztes Word-Dokument schlägt sofort fehl. Geht das Programm weiter, schließt der nächste Programmpunkt das Dokument;
zeigt er dieselbe Datei über dieselbe Taste, bleibt sie auf ihrer Seite stehen. Fehlt `pypdfium2`,
installiert der Agent genau diese Version (`pypdfium2==5.15.2`) beim Start selbst
(`pip`, im Hintergrund, einmal je Agent-Version, nach einem Fehlschlag erst nach
einer Stunde wieder, dann seltener; das Update über den UPDATE-Knopf führt kein
pip aus, `update-agent.ps1` schon). Kann der Agent ein Dokument nicht
anzeigen (kein pypdfium2, weder Office noch LibreOffice, älterer Agent), öffnet
es wie bisher die Standard-App; der Grund steht im Agent-Log, `/info` meldet unter
`documents`, was vorhanden ist.

**Durchsuchen…** gibt es nur, wenn die Weboberfläche auf dem Streaming-PC selbst
geöffnet ist (`http://127.0.0.1:8765`). Es öffnet dort den normalen
Windows-Dialog „Datei öffnen“ (`POST /files/pick`): Der Display-Agent des
Streaming-PCs zeigt ihn auf dessen Bildschirm, der Knopf zeigt solange
„Dialog am PC offen…“, und der gewählte Pfad landet im Programmpunkt.
Abbrechen lässt alles, wie es war. Der Dienst selbst kann keinen Dialog
zeigen (er läuft als Windows-Dienst ohne Bildschirm), und der Datei-Dialog
eines Browsers verrät der Seite nie den vollständigen Pfad; deshalb der Weg
über den Agenten. Kann der Agent den Dialog nicht zeigen (älterer Agent, Agent
nicht gestartet), erscheint stattdessen die eingebaute Ordnerliste
(`GET /files/browse`). Anfragen von anderen Geräten lehnt der Service bei
beidem ab (403); dort wird der Pfad eingetippt. Für den Windows-Dialog muss
der Desktop-Agent auf dem Streaming-PC aktuell sein.

## Lieder (FoilPresenter) im Programm

Die FoilPresenter-Bibliothek bleibt auf dem Anzeige-PC (z. B. `.11`). Der
Display-Agent dort liest die `.foil`-Dateien aus dem eingestellten Ordner (dessen
Unterordner `Songs`, oder den Ordner selbst); der Service liest sie ein
(`app/foil.py`), merkt sie sich in `song_library.json` und schickt zum Anzeigen
immer nur eine Folie an den Agent, der sie im Vollbild zeichnet. Eine Folie ist
eine Strophe in der Reihenfolge des Lieds (der Refrain wiederholt sich).

PowerPoint-Dateien (`.pptx`) im selben Ordner werden ebenfalls eingelesen
(`app/pptx_song.py`): Jede Folie der Präsentation ist in ihrer Reihenfolge eine
Folie des Lieds, mit dem Text der Folie (ausgeblendete und leere Folien fallen
weg); der Titel ist der Dateiname. Gezeigt wird wie bei allen Liedern mit den
Einstellungen der Liedanzeige (Bilder und Gestaltung der Präsentation nicht).
Der Agent schickt dafür nur die XML-Teile der Folien (höchstens 200 Folien,
4 MB entpackt); dafür muss er aktualisiert sein.

In der Weboberfläche unter **Liedanzeige → Einstellungen…** (gilt für alle Lieder,
gespeichert in `song_display.json`): Ordner der Bibliothek und der PC, auf dem
sie liegt, **Liedtext zeigen auf** (siehe unten), Vollbild, Schrift, Schriftgröße
(px bei 1080p, gleich auf allen Folien eines Liedes, siehe unten), Text- und
Hintergrundfarbe, Titel und Copyright auf der ersten Folie,
Ausrichtung (links, mittig, rechts) und **Vorschau nächste Folie**: wo unter dem
Text Platz bleibt, stehen dort in Grau (Textfarbe in den Hintergrund gemischt)
die ersten Zeilen der nächsten Folie, höchstens halb so groß. Der Text selbst
bleibt dabei unverändert groß und an seiner Stelle; nach der letzten Folie gibt
es keine Vorschau. Ein Anzeige-PC mit älterem Display-Agent bekommt die Folie
ohne Vorschau und Ausrichtung, bis der Agent aktualisiert ist (die Update-Taste
aktualisiert die Agents aller PCs).
**Zeilen, Schriftgröße und geteilte Strophen** (`app/slide_layout.py`): Der
Service setzt jedes Lied selbst, für jeden Bildschirm; Display-Agent und Seite
zeichnen nur noch die Zeilen in der Größe, die er schickt (`layout`).
1. Alle Folien eines Liedes haben auf einem Bildschirm **dieselbe
   Schriftgröße**: möglichst die eingestellte.
2. Eine Zeile der Datei, die zu breit ist (FoilPresenter-Dateien haben oft eine
   ganze Strophe in einer Zeile), wird umgebrochen – bevorzugt nach Satzende,
   dann nach Komma, Semikolon, Doppelpunkt oder Gedankenstrich, eine Zeile nie
   mit „und“, „die“ & Co. am Ende, die Zeilen etwa gleich lang; wo Platz ist,
   lieber eine Zeile mehr als ein Umbruch mitten in der Wendung. Zeilen der
   Datei werden nie zusammengezogen; Wiederholungszeichen (`|:` / `:|`) bleiben
   an ihrem Wort.
3. Passt eine Folie nicht, wird **das ganze Lied** kleiner, höchstens um 18 %
   (`SHRINK_LIMIT` = 0,82).
4. Reicht das nicht, wird die Strophe auf **zwei oder mehr Folien geteilt**: an
   einem Zeilenumbruch der Datei (am liebsten an einer Leerzeile oder nach einem
   Satzende), in einer langen Zeile nach Satzende oder Komma, die Teile etwa
   gleich lang. Die Teile heißen z. B. „2.1“ und „2.2“, zählen bei Weiter/Zurück,
   in „Folie x/y“, bei den Tasten am Programmpunkt und bei Sprüngen wie eigene
   Folien; Titel und Copyright stehen nur auf dem ersten Teil.
5. Nur eine Folie, die sich nicht teilen lässt (die Titelfolie, ein riesiges
   Wort), wird bis 40 px (bei 1080p) kleiner und notfalls unten abgeschnitten;
   die Vorschau markiert sie mit „zu lang“. Sie zieht die übrigen Folien nicht
   mit herunter.

Die Breiten misst der Display-Agent: Der Service fragt ihn einmal je Schrift
nach den Zeichenbreiten und der Fenstergröße seines Monitors (`/song/metrics`)
und merkt sie sich; gesetzt wird im Service (in einem eigenen Thread, auch
300 Folien in Bruchteilen einer Sekunde) und nur neu, wenn sich Lied,
Einstellungen oder Breiten ändern; dabei bleibt die gezeigte Folie bei ihrer
Strophe (aus „2.2“ wird z. B. „2“, nicht die nächste Strophe), und ein Sprung
von einer Seite mit älterer Nummerierung (`rev`) landet auf dem gemeinten Teil.
Nach dem Speichern geänderter Einstellungen zeigen die Bildschirme die
aktuelle Folie gleich neu (nicht erst beim nächsten Weiter; nichts, solange
schwarz oder ausgeblendet). Gemessen wird je Bildschirm für höchstens vier
Schriften (die Einstellungs-Vorschau misst jeden getippten Schriftnamen); die
zuletzt benutzten und die gespeicherte bleiben.
**Mehrere Bildschirme**: Geteilt wird zuerst für den Bildschirm, der die
meisten Folien braucht, dann weiter, wo ein anderer es noch braucht – so
zeigen alle dieselben Folien, bleiben im Takt und keiner wird kleiner als
seine 18 %-Grenze; jeder bekommt seine eigene einheitliche Größe. Meldet ein
Agent eine andere Fenstergröße (Auflösung geändert), wird neu gesetzt.
Die Vorschau auf der Seite zeigt den ersten Bildschirm genau so, wie der Service
ihn setzt (`tests/test_slide_parity.py`); Einstellungs- und Neues-Lied-Vorschau
fragen dafür `POST /songs/layout`. **Ältere Display-Agents** kennen weder
`/song/metrics` noch `layout`: Der Service schätzt die Breiten dann (Verdana,
1920 × 1080) und teilt die Strophen damit, schickt die Folie ohne `layout` (der
Agent passt sie wie früher selbst ein) und merkt sich das je PC für 10 Minuten,
damit jede Folie nur eine Anfrage braucht – aber nur bei einer Absage wegen
eines unbekannten Felds (400), und vergessen, sobald der Agent mit einer
anderen Startzeit antwortet (aktualisiert oder neu gestartet). Lehnt ein
aktueller Agent einen Wert ab (422), bekommt er diese Folie gleich noch einmal
ohne `layout`, statt dunkel zu bleiben; gemerkt wird das nicht. Die Liste der
Lieder zählt Strophen und Refrains (`slide_count`), nicht die geteilten Folien.
**Titelfolie** (Standard: an): Jedes Lied beginnt mit einer eigenen Folie, auf
der der Titel in der Mitte steht und das Copyright darunter (mit dem Schalter
*Copyright*). Sie ist Folie 0, die Liedfolien zählen ab 1; ein Programmpunkt
startet mit ihr. Der Schalter *Titel* (Titel oben auf der ersten Liedfolie) gilt
nur ohne Titelfolie. Die Titelfolie wird als normale, zentrierte Folie geschickt,
jeder Display-Agent zeigt sie also ohne Update.
**Speichern und Bibliothek laden** liest die Bibliothek (neu) ein.
**Mehrere Bildschirme**: Unter *Liedtext zeigen auf* wird je PC ein Schalter
(und dann dessen Monitor) gewählt, z. B. der Beamer am Anzeige-PC und ein Monitor
am Streaming-PC (`targets`: bis zu 4, je PC einer – ein Display-Agent hat ein
Liedfenster). Jede Folie, Schwarz und Aus geht gleichzeitig an alle, je
Bildschirm der Reihe nach; jeder Agent passt den Text an seinen eigenen
Bildschirm an. Ein Schritt wartet höchstens 1 s und nie auf einen PC, der schon
nicht geantwortet hat: der bekommt nur die jeweils neueste Folie, alle 5 s neu,
bis er wieder antwortet (dann zeigt er gleich die aktuelle). Die Liedanzeige
nennt ihn in Gelb mit Grund, die Leiste am Programmpunkt zeigt ⚠ (antippen nennt
ihn), und beim ersten Mal kommt eine Meldung (auch beim Start eines
Programmpunkts). Erst wenn kein Bildschirm erreichbar ist, bleibt die Folie
stehen (502); die Tasten, die keinen Bildschirm erreicht haben, zählen dann
nicht, und zurückkommende Bildschirme zeigen dieselbe Folie wie die Seite. Ältere Einstellungen (`target_ip` + `monitor`) gelten als ein
Bildschirm; `target_ip` ist seither der PC mit der Bibliothek, `monitor` der
des ersten Bildschirms; eine ältere Seite ändert damit nur den ersten
Bildschirm. Ein ungültiger oder doppelter Bildschirm in `song_display.json`
fällt allein weg. Im Test-Dienst spielt `--dead-agent IP` einen PC, dessen
Agent aus ist (jeder Aufruf hängt bis zum Timeout).

Ein Programmpunkt bekommt sein Lied in der Spalte *Liedtext* („♪ Lied wählen“,
Suche nach Titel oder Liederbuch-Nummer; eine eingetippte Nummer zeigt die Lieder
mit genau dieser Nummer zuerst); beim Start zeigt er die erste Folie.
Die Steuerung dafür ist der eigene Bereich **Liedanzeige**: eine 16:9-Vorschau,
alle Folien als Miniaturen (antippen = anzeigen), **Zurück / Weiter / Schwarz /
Aus** sowie *Lied zeigen…*, *Neues Lied…* und *Einstellungen…*. Er wird wie
alle anderen Bereiche auf- und zugeklappt und mit *Anordnen* platziert.
Im Stage-Modus ist er ein eigener Bereich wie alle anderen. Nach
der letzten Folie schaltet *Weiter* auf schwarz.
**Am laufenden Programmpunkt**: Hat der laufende Programmpunkt ein Lied, fährt
über seiner Zeile eine Leiste aus dem oberen Rand heraus (`js/song-dock.js`):
links eine kleine Vorschau dessen, was die Bildschirme zeigen (auch schwarz oder
aus), rechts Zurück, je Folie ein Knopf (T, 1, R, 2 …; antippen = anzeigen),
Aus, Schwarz und ein großes Weiter, dazu die erste Zeile der nächsten Folie. Sie
steuert dasselbe Lied wie der Bereich Liedanzeige (gleiche Aufrufe, beide zeigen
immer dasselbe). Ist ein anderes oder kein Lied auf dem Bildschirm, bleibt eine
Zeile mit *Zeigen* (das Lied des Programmpunkts ab der ersten Folie); läuft ein
anderer Programmpunkt, fährt sie wieder ein.
Wird ein Programmpunkt hier mit PLAY gestartet, bleibt PLAY unter dem Finger
(die Seite oder die Stage scrollt um die Höhe der Leiste). Geht das nicht (Stage,
deren Programm ganz hineinpasst), bleiben die Tasten der Leiste, die jetzt dort
liegen, wo PLAY war, gelb markiert gesperrt, bis woanders getippt wird; ein
zweites Tippen „auf PLAY“ löst nie Schwarz oder Weiter aus.

Tastatur und Presenter (immer, solange die Seite offen ist; nicht beim Tippen in
einem Feld oder bei offenem Dialog):

| Taste | Wirkung |
|---|---|
| Bild ab | nächste Folie des Lieds; ist das Lied durch (schwarz nach der letzten Folie) oder läuft keins: nächster Programmpunkt |
| →, Leertaste | nur nächste Liedfolie, nie ein Programmpunkt (eine versehentlich gedrückte Taste schaltet so keine Mutegruppe) |
| Bild auf, ← | vorige Folie (im Programm geht es nie zurück) |
| B oder . | Bildschirm schwarz / wieder an (ohne Lied: Anzeige-PC schwarz / aus) |
| H | Beamer hide / unhide (wie die Tasten „Beamer hide/unhide“; nach dem Start gilt der Beamer als sichtbar) |

**Neues Lied…** (im Liedauswahl-Fenster) legt ein Lied an: Titel, Copyright
und Liedtext einfügen; jede Leerzeile beginnt eine neue Folie, die Folien werden
durchnummeriert. Eine Folie kann als Refrain markiert werden; er kommt dann
automatisch nach jeder Strophe (Schalter: am Anfang, nach jeder Strophe, am
Ende). Die Reihenfolge lässt sich auch von Hand eintippen, z. B. `R 1 2 R 3 R`
(R = Refrain); die Vorschau zeigt alle Folien in dieser Reihenfolge. Gespeichert
wird als FoilPresenter-`.foil`-Datei in der Bibliothek auf dem Anzeige-PC
(Dateiname wie in FoilPresenter, z. B. `Gott_ du bist da.foil`); eine vorhandene
Datei wird nie überschrieben.

Der Agent braucht dafür die neue Version (Weboberfläche auf dem Anzeige-PC:
*Update*). Endpunkte, z. B. für Companion:

| Methode | Pfad | |
|---|---|---|
| GET/PUT | `/songs/settings` | Liedanzeige-Einstellungen |
| POST | `/songs/library/reload` | Bibliothek vom Anzeige-PC lesen |
| GET | `/songs`, `/songs/{datei}` | Liste, ein Lied mit allen Folien |
| GET | `/songs/live` | was gerade angezeigt wird; `screens`: je Bildschirm ok oder Grund |
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

**OBS-Szene pro Kamera** (Dropdown neben dem Preset der Kamera, Liste aus OBS):
Statt automatisch „Cam N Live“ zu schalten, schaltet OBS in Schritt 3 auf die
Szene der Kamera, die ihr Preset angefahren hat. Ist keine Kamera live, kommt
die Szene einer bewegten Kamera (Cam 1 zuerst) ebenfalls erst nach der
Wartezeit (die Kameras fahren noch). Eine Szene zählt nur für eine Kamera,
deren Preset angefahren wird; bewegt sich nichts, bleibt OBS, wie es ist.
*OBS · automatisch* lässt alles wie oben beschrieben.

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
wie es danach aussieht. **Programm ersetzen** (mit Rückfrage) ersetzt das ganze
Programm und setzt den Ablauf zurück.

**Gottesdienstablauf-PDF**: **PDF importieren** liest das ausgefüllte Formular
„Gottesdienstablauf“ (AcroForm-Textfelder `Lied1`…`Lied6`, `Textlesung`,
`Predigt`, `Begrüßung1.Stunde`, …). Die Lieder kommen in die Liedzeilen (eine
reine Nummer wie `101` findet das Lied, wenn sie nur in einem Liederbuch
vorkommt), die Personen in die Vorlagenzeilen mit passendem Titel – in der
Reihenfolge des Ablaufs, Liedplätze ausgenommen. Personen ohne passende Zeile
nennt die Vorschau. Der letzte Import bleibt gespeichert (`program_pdf.json`)
und steht als **Zuletzt empfangen** bereit; so kann auch ein anderes Programm
das PDF schicken, z. B.
`curl -X POST --data-binary @Gottesdienstablauf-20261126.pdf -H "Content-Type: application/pdf" "http://192.168.188.20:8765/program/pdf?filename=Gottesdienstablauf-20261126.pdf"`.

| Methode | Pfad | |
|---|---|---|
| GET | `/program/templates` | Vorlagen |
| POST | `/program/templates` `{"name", "slots", "replace"}` | aktuelles Programm speichern (`slots`: IDs der Liedplatz-Zeilen) |
| DELETE | `/program/templates/{id}` | Vorlage löschen |
| POST | `/program/templates/{id}/apply` `{"songs": [{"song_id", "text"}], "persons": [{"id", "person"}], "dry_run"}` | anwenden (`dry_run`: nur Vorschau; `persons` aus dem PDF-Import) |
| POST | `/songs/match` `{"lines"}` | Liedzeilen in der Bibliothek suchen |
| POST | `/program/pdf?filename=` (Body: PDF, max. 5 MB) | Gottesdienstablauf-PDF einlesen und als letzten Import speichern |
| GET | `/program/pdf` | letzter Import (`songs`, `points`, `date`) oder `null` |

## Licht

Die Section **Licht** steuert das ganze Licht; in der Steuerung gibt es die
Kategorien Licht und Mixer nicht mehr (Katalog v8 räumt sie aus, eigene Tasten
darin bleiben). Programmzeilen, Overall-Makros und eigene Tasten, die diese
Tasten per ID nutzen, funktionieren weiter; Companion/Stream Deck kann sie auch
weiter über `/companion/buttons/{id}/press` drücken.

- **Presets** (oben, die Hauptsache im Alltag): große Kacheln, Antippen blendet
  das Preset mit dem eingestellten Übergang ein. Eine Kachel leuchtet („aktiv“),
  solange das Licht genau so ist, wie das Preset es setzt (ein ausgeschalteter
  Scheinwerfer zählt dabei unabhängig vom gemerkten Wert; ein Raumlicht-Kreis
  des Presets ohne Antwort heißt „nicht sicher gleich“, die Kachel leuchtet dann
  nicht). Das zuletzt abgerufene Preset, das danach
  von Hand verändert wurde, zeigt „geändert“. Die Punkte auf der Kachel zeigen
  jeden Scheinwerfer (Farbe und Helligkeit, Ring = aus, Strich = bleibt, wie er
  ist), dazu die Raumlicht-Kreise („Raum 2 an · 1 aus“). *+ Preset* speichert
  das aktuelle Licht der gewählten Lichter: jeder gewählte Scheinwerfer mit
  an/aus, Helligkeit und Farbe, jeder gewählte Kreis mit seinem Zustand.
  Das „⋮“ jeder Kachel hat Umbenennen, Mit aktuellem Licht überschreiben… (die
  Auswahl der Lichter lässt sich dabei ändern, Platz und Programmzeilen bleiben;
  ein Kreis des Presets, der gerade nicht antwortet, behält seinen bisherigen
  Zustand, die Meldung sagt es), Nach vorne / Nach hinten und Löschen (mit
  Rückfrage).
  Presets heißen in der API weiter `scenes`. Alte Szenen (ein Warm/Kalt für alle,
  0 = aus) werden beim Start umgewandelt: jede Zone mit ihrer Helligkeit bzw. aus
  (den gemerkten Wert behält der Scheinwerfer), die gespeicherte Farbe für jeden
  dieser Scheinwerfer; die Reihenfolge wird einmal nach Namen gesetzt.
- **Bühne** (Art-Net, 4 Scheinwerfer): je Scheinwerfer eine Karte mit An/Aus,
  Helligkeit und Farbe. **Aus behält die Helligkeit**: Der Regler bleibt stehen
  und wird grau, gesendet wird 0; Einschalten bringt den Wert zurück (ohne
  gemerkten Wert: *Einschalten auf*). Ein Regler eines ausgeschalteten
  Scheinwerfers merkt sich den Wert nur, das Licht bleibt aus (ein versehentlich
  berührter Regler schaltet im Gottesdienst kein Licht ein). Die Farbe ist ein
  Regler warm ↔ kalt über die beiden Weiß-Kanäle (0–510): von links nur Warm
  (255), Kalt wird bis zur Mitte (beide 255) dazugemischt, dann nimmt Warm nach
  rechts ab. Der stärkere Kanal steht immer auf 255, die Helligkeit regelt Kanal 1.
  Companions Farbe (Warm 255, Kalt 50) liegt genau darauf. *Alle gleich*: ein
  Regler bewegt Helligkeit bzw. Farbe aller vier (pro Gerät gemerkt). *Alle an*
  schaltet jeden auf seinen Wert, *Alle aus* aus. Beim Ziehen folgt das Licht
  direkt, Tasten und Presets blenden mit dem Übergang. Darunter zwei kleine
  Chips: *Übergang* und *Einschalten auf* (Antippen öffnet den Regler). Der
  Dimmschritt der Heller/Dunkler-Tasten (über Companion/Stream Deck) steht nicht
  mehr auf der Seite, bleibt aber gespeichert (`PUT /lights/settings {"steps"}`).
  Die Tasten der Steuerung und Overall-Makros arbeiten wie bisher: An/Aus-Taste
  einer Zone bringt den gemerkten Wert zurück, Overall-Tasten schalten eine
  ausgeschaltete Zone auf *Einschalten auf* (eine, die das Makro ausschaltet,
  behält ihren Wert), Wärmer/Kälter ändert jeden Scheinwerfer um denselben
  Schritt (Antwort `level`: der gemeinsame Wert bzw. der Mittelwert, sobald die
  Scheinwerfer sich unterscheiden; `levels`: je Scheinwerfer). Eine ungültige
  Eingabe an `POST /lights/stage` (unbekannte Zone, Wert außerhalb 0–255, leere
  oder unbekannte Änderung) gibt 400, es wird nichts gesendet; 502 heißt: Senden
  gescheitert. Ist der gespeicherte Frame beim Start ungültig, werden alle
  Scheinwerfer als aus geladen (ihre Werte bleiben), ein Scheinwerfer ohne
  gültigen Eintrag nimmt Helligkeit und Farbe aus dem gespeicherten Frame.
  Art-Net wie in Companion (Verbindung `generic-artnet`): Knoten
  192.168.188.48, Universe 0, je Scheinwerfer Kanal 1 Helligkeit, 2 Warm,
  3 Kalt (Links 1–3, Mitte links 5–7, Mitte rechts 9–11, Rechts 13–15).
  Ein Frame hat immer alle 512 Kanäle, Sequenz 0. Wie Companion schickt der
  Dienst den aktuellen Frame **jede Sekunde neu**, auch ohne Änderung, und
  beim Blenden alle 40 ms. Ohne dieses Dauersenden fällt die Bühne nach
  wenigen Sekunden auf schwarz. Das Dauersenden beginnt mit dem ersten
  Bühnenbefehl aus der App und läuft danach auch nach einem Neustart weiter
  (`stage_active` in `native_control_state.json`). Bis dahin sendet der
  Dienst nichts, damit er ein noch genutztes Companion nicht stört.
  Alle Pakete gehen über einen Socket mit festem Absender-Port 6454. Ein Regler
  schickt höchstens alle 60 ms und nie zwei Werte gleichzeitig (sonst konnten
  sie sich überholen und das Licht flackerte).
  **Nur ein Programm darf die Bühne steuern: In Companion muss die
  Verbindung „artnet“ dauerhaft deaktiviert sein.** Der Knoten (Electroconcept
  „Node 1“) mischt zwei Quellen nach HTP, pro Kanal gewinnt der höhere Wert:
  Solange Companion mitsendet, kann die App nicht unter Companions Werte
  dimmen oder ausschalten. Jeder Bühnenbefehl schaltet das Dauersenden ein,
  auch eine Programmzeile mit Lichtszene oder eine Overall-Taste, und es
  bleibt an. Ein Zurückgeben an Companion gibt es nicht. Ist der gespeicherte
  Frame nach einem Neustart ungültig, wartet das Dauersenden auf den nächsten
  Befehl (kein Null-Frame, keine Verdunkelung). `GET /lights/artnet` fragt den Knoten per ArtPoll und
  meldet mit `"merging": true`, dass er zwei Quellen mischt; `"sending"` zeigt,
  ob der Dienst die Bühne hält und ob das Senden scheitert.
- **Raumlicht** (Shelly, 7 Kreise): An/Aus je Kreis mit dem Zustand, den der
  Shelly meldet (alle paar Sekunden gefragt); ein Kreis, der nicht antwortet,
  zeigt „Offline“ und wird über der Section genannt („Raumlicht nicht
  erreichbar: …“). Ein Shelly, der nicht im Netz ist, wird nach 1 s Verbindungsaufbau
  aufgegeben (`SHELLY_CONNECT_TIMEOUT`); ein verbundener hat 2,5 s für die Antwort.
  Schalten versucht den Verbindungsaufbau ein zweites Mal (WLAN-Aussetzer; vor dem
  Verbinden ist nichts gesendet, es schaltet also nie doppelt), Abfragen nicht.
  Eine einzelne verpasste Abfrage ändert nichts: Ein Kreis mit bekanntem Zustand
  gilt erst nach 3 Abfragen ohne Antwort in Folge als nicht erreichbar
  (`CIRCUIT_FAILURES_UNREACHABLE`); bis dahin zeigt er seinen letzten Zustand
  (`"stale": true` in `GET /lights`, nur im Tooltip sichtbar). Alle offenen Seiten
  teilen sich die Abfragen: Jeder Shelly wird höchstens einmal je 2 s gefragt,
  egal wie viele Seiten offen sind, in eigenen Threads des Dienstes. Angetippt zeigt die Taste den
  neuen Zustand sofort (die Lampe pulsiert), bis der Shelly ihn bestätigt; schlägt
  es fehl, kommt der echte Zustand zurück. Jeder Shelly wird für sich gefragt:
  `GET /lights` wartet höchstens 0,4 s und nie auf einen Kreis, der schon zuletzt
  nicht geantwortet hat, ein toter Kreis bremst also weder die Seite noch die
  anderen. „Alle an/aus“ (auch die Steuerung-Tasten) schaltet alle gleichzeitig
  und jeden Kreis, der antwortet; ein Preset schaltet Raumlicht und Bühne
  gleichzeitig.

Im **Programm** hat jede Zeile die Spalte *Licht*: ein Preset, das beim Start
der Zeile eingeblendet wird (vor Tasten und Lied). Wird das Preset gelöscht,
startet die Zeile nicht mehr („unknown light scene“), bis ein anderes gewählt ist.

| Methode | Pfad | |
|---|---|---|
| GET | `/lights` | Bühne (`stage.fixtures`: je Scheinwerfer `on`, `level`, `warm`, `cold`; `stage.zones`: was er zeigt, 0 = aus), Einstellungen, Raumlicht-Kreise, Presets (`active`), `last_scene` |
| POST | `/lights/stage` `{"zones": {"left": {"on", "level", "warm", "cold"}}, "warm", "cold", "fade"}` | Scheinwerfer setzen (je Wert optional; eine Zahl statt Objekt: > 0 an auf diesen Wert, 0 aus mit gemerktem Wert; `warm`/`cold` oben: für alle; `fade: false` springt) |
| PUT | `/lights/settings` `{"start", "steps", "fade_ms"}` | Einstellungen |
| POST | `/lights/circuits` `{"states": {"light-front": true}}` | Raumlicht schalten; Antwort `circuits`: Zustand aller Kreise |
| GET | `/lights/scenes` | nur die Szenen |
| POST | `/lights/scenes` `{"name", "replace", "id", "lights": {"zones", "circuits"}}` | aktuelles Licht als Preset speichern; mit `id`: dieses Preset überschreiben |
| POST | `/lights/scenes/order` `{"ids"}` | Reihenfolge der Presets (jede ID genau einmal) |
| POST | `/lights/scenes/{id}/apply` | Szene einblenden |
| GET  | `/lights/artnet` | Bühnen-Knoten per ArtPoll fragen: Name, `merging` (zweiter Sender), `sending` (Dauersenden aktiv, Fehler) |
| DELETE | `/lights/scenes/{id}` | Szene löschen |

## Umbenennen

„Umbenennen“ im „⋮“-Menü macht den Namen zum Textfeld: Enter oder Wegtippen
speichert, Escape bricht ab. Die Namen speichert der Dienst, alle Geräte sehen
sie.

- **Licht**: Presets (ihr „⋮“). Scheinwerfer und Raumlicht-Kreise: das „⋮“ des
  Blocks (Bühne bzw. Raumlicht) startet einen Umbenennen-Modus (Namen
  gestrichelt, Leiste mit „Fertig“); dann öffnet ein Tipp den Namen, statt das
  Licht zu schalten. Bei Zonen und Kreisen stellt ein leerer Name den
  eingebauten Namen wieder her.
- **Steuerung** (mit *Bearbeiten*): Tasten, Kategorien und die Geräte unter
  *Alles ausschalten* (Beamer, Agent-PCs; leer = Name aus den Desktop-Profilen).
- **Programm → Vorlagen**: die Vorlagen (ihr „⋮“).
- **Kameras**: „⋮“ der Kamera → Presets umbenennen (Tasten gestrichelt); dann
  öffnet ein Tippen auf ein Preset den Namen, statt die Kamera zu bewegen.
  Gespeichert wird mit der Kamera davor („CAM1 Kanzel“), damit Programmzeilen
  die Kameras unterscheiden; leer = Name aus dem Katalog (ein selbst
  gespeichertes Preset braucht einen Namen).

Ein Umbenennen-Modus endet mit „Fertig“, wenn ein anderer beginnt, wenn sein
Bereich zugeklappt wird, und von selbst nach 30 s ohne Umbenennen; danach
schalten die Tasten wieder.

Kanäle, Mutegruppen, Steuerungstasten, Kameras und Desktop-Profile ließen sich
schon vorher umbenennen.

| Methode | Pfad | |
|---|---|---|
| PUT | `/names/{art}/{id}` `{"name"}` | `light-zone`, `light-circuit`, `power-target` (IP oder `beamer`); `""` = eingebauter Name |
| PUT | `/lights/scenes/{id}` `{"name"}` | Szene umbenennen |
| PUT | `/program/templates/{id}` `{"name"}` | Vorlage umbenennen |
| POST | `/companion/categories/{id}/rename` `{"name"}` | Kategorie umbenennen |
| PUT | `/camera-presets/{tasten-id}/label` `{"name"}` | Kamera-Preset umbenennen (`""` = Katalogname) |

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
