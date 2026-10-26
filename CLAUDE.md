Briefing: Sicherer Mixer-Mutegruppen-Webservice mit State Tracking
Ziel
Baue einen lokalen Webservice, der sichere Mutegruppen für ein digitales Mischpult bereitstellt.
Das Mischpult hat eine iPad-App zur Steuerung, unterstützt aber offenbar keine nativen Mutegruppen. Ziel ist, eigene Mutegruppen über einen lokalen Service bereitzustellen, der z. B. von einem Stream Deck per HTTP-Request getriggert werden kann.
Wichtig: Der Service darf niemals blind Byte-Kommandos mit hardcodierten Faderwerten senden, weil das Mischpult bei Mute/Unmute-Kommandos gleichzeitig den Faderwert mitschreibt. Ein falscher oder veralteter Faderwert kann live katastrophale Folgen haben.
Der Service soll deshalb dauerhaft den Mixer-State tracken, insbesondere den letzten bekannten Faderwert pro Kanal, und Mute/Unmute-Kommandos nur senden, wenn der aktuelle Faderwert frisch und valide bekannt ist.

⸻

Bekannte Netzwerkdaten
Mixer IP:
MIXER_IP = "192.168.188.30"
Mixer TCP-Port:
MIXER_PORT = 6400
Kommunikation erfolgt über TCP.

⸻

Bekannter Protokollstand
Es wurden TCP-Payloads zwischen iPad-App und Mixer sowie zwischen Mixer und iPad mitgeschnitten.
Allgemeiner Frame-Header für App → Mixer
Kommandos, die vom Client an den Mixer gesendet und vom Mixer akzeptiert werden, beginnen mit:
5a5a5a5a5a5a0000006e000000
Diesen Header nennen wir:
COMMAND_PREFIX = "5a5a5a5a5a5a0000006e000000"
Wichtig: Kein zusätzliches 00 hinten anhängen. Der Prefix ist exakt wie oben.
Allgemeiner Frame-Header für Mixer → iPad
Statusupdates vom Mixer zur iPad-App beginnen unter anderem mit:
5a5a5a5a5a5a0000006d00000100
Diese Frames sind offenbar Status-/Broadcast-/Update-Frames. Ein Test hat gezeigt, dass der Mixer dieses 6d-Format nicht als eingehenden Steuerbefehl akzeptiert.
Für den Service ist das 6d-Format trotzdem wichtig, weil es Statusupdates enthalten kann, aus denen aktuelle Faderwerte gelesen werden.

⸻

Erkannte Kanal-/Mute-/Fader-Struktur
Für Kanal 2 wurden folgende App-/Mixer-Kommandos beobachtet.
Kanal 2 Unmute bei bestimmter Faderposition
5a5a5a5a5a5a0000006e000000070708012d1f14b643
Kanal 2 Mute bei gleicher Faderposition
5a5a5a5a5a5a0000006e0000000909080120012d1f14b643
Daraus ergibt sich sehr wahrscheinlich:
07 07              = Unmute-Payload-Länge/Typ
09 09              = Mute-Payload-Länge/Typ
08 01              = Kanal 2
20 01              = Mute an
2d <4 bytes>       = Faderwert
Bei Unmute fehlt 20 01.
Der Kanalindex ist nullbasiert:
08 00 = Kanal 1
08 01 = Kanal 2
08 02 = Kanal 3
...
Für Kanal N gilt also:
channel_index = N - 1
Payload-Format für App → Mixer
Mute:
09 09 08 XX 20 01 2d FF FF FF FF
Unmute:
07 07 08 XX 2d FF FF FF FF
Dabei gilt:
XX             = channel_index, also channel - 1
FF FF FF FF    = aktueller Faderwert als 4 Bytes Hex
Beispiel Kanal 2 mute mit Faderwert 1f14b643:
0909080120012d1f14b643
Beispiel Kanal 2 unmute mit Faderwert 1f14b643:
070708012d1f14b643
Vollständige Frames:
COMMAND_PREFIX + payload
Also:
5a5a5a5a5a5a0000006e0000000909080120012d1f14b643

⸻

Kritische Sicherheits-Erkenntnis
Mute/Unmute-Kommandos setzen immer auch den Faderwert.
Tests haben gezeigt:
1. Wird ein valider Faderwert mitgesendet, wird der Mute-Status gesetzt und der Fader bleibt nur dann unverändert, wenn der mitgesendete Faderwert dem aktuellen Faderwert entspricht.
2. Wird 2d mit leerem Faderwert gesendet, passiert nichts.
3. Wird 2d und der Faderwert komplett weggelassen, wird der Befehl teilweise akzeptiert, aber der Fader wird auf 0 beziehungsweise Minimum gesetzt.
4. Dummy-Werte wie 00000000, ffffffff, etc. sind gefährlich, weil sie den Fader auf unerwünschte Positionen setzen können.
5. Es wurde bisher kein sicherer Mute-only-Befehl ohne Faderwert gefunden.
Daraus folgt:
Der Service darf Mute/Unmute nur senden, wenn er für den jeweiligen Kanal einen frischen, validen, vom Mixer beobachteten Faderwert kennt.

⸻

Zielarchitektur
Der Service soll lokal laufen, z. B. auf Laptop, Mini-PC oder Raspberry Pi.
Stream Deck oder andere Buttons rufen nur HTTP-Endpunkte auf, z. B.:
curl http://localhost:8765/groups/drums/toggle
Der Service selbst übernimmt:
1. dauerhafte Verbindung/Mitschnitt/State-Tracking,
2. Extraktion aktueller Faderwerte,
3. Speicherung des letzten bekannten Mute-Status,
4. sichere Validierung vor dem Senden,
5. Erzeugung gültiger Mixer-Kommandos,
6. Logging und Fehlervermeidung.
Grobe Architektur:
Stream Deck
    |
    | HTTP
    v
Local Webservice
    |
    | TCP command
    v
Mixer

Local Webservice
    ^
    | TCP state updates / listener
    |
Mixer

⸻

Technologievorschlag
Bitte in Python bauen.
Empfohlen:
Python 3.11+
FastAPI
uvicorn
asyncio
pydantic
Optional:
typer oder argparse für CLI
rich für schöne Logs
systemd service file für Linux Autostart
Kein schweres Frontend nötig. Eine minimale JSON-API reicht.

⸻

Zentrale Anforderungen
1. Webserver
Der Service soll HTTP-Endpunkte bereitstellen:
GET  /health
GET  /state
GET  /channels/{channel}
POST /channels/{channel}/mute
POST /channels/{channel}/unmute
POST /channels/{channel}/toggle
POST /groups/{group}/mute
POST /groups/{group}/unmute
POST /groups/{group}/toggle
POST /panic/disable
POST /panic/enable
2. State Tracking
Der Service muss pro Kanal mindestens speichern:
channel_state = {
    channel: {
        "fader_hex": str | None,
        "fader_updated_at": float | None,
        "muted": bool | None,
        "muted_updated_at": float | None,
        "last_seen_raw": str | None,
    }
}
fader_hex muss exakt 8 Hex-Zeichen haben.
Beispiel:
{
    2: {
        "fader_hex": "1f14b643",
        "fader_updated_at": 1710000000.123,
        "muted": True,
        "muted_updated_at": 1710000000.123,
        "last_seen_raw": "5a5a..."
    }
}
3. Sicherheitsregeln
Vor jedem Senden müssen diese Checks erfüllt sein:
1. Kanalnummer ist gültig.
2. Für den Kanal ist ein Faderwert bekannt.
3. Faderwert ist exakt 8 Hex-Zeichen.
4. Faderwert stammt aus einem empfangenen Mixer-/App-State-Frame, nicht aus einem Default.
5. Faderwert ist frisch genug.
6. Service ist nicht im Panic-/Disabled-Modus.
7. Bei Gruppen: Alle betroffenen Kanäle müssen sichere Faderwerte haben, sonst ganze Aktion abbrechen.
8. Nie teilweise eine Gruppe muten, wenn ein Kanal unsicher ist, außer ein expliziter allow_partial=true Parameter wird später bewusst implementiert. Default: kein Partial.
9. Niemals einen Fallback-Faderwert wie 00000000 verwenden.
10. Niemals senden, wenn der Faderwert unbekannt oder veraltet ist.
Empfohlene Freshness:
MAX_FADER_AGE_SECONDS = 2.0
Für Live-Betrieb eventuell strenger:
MAX_FADER_AGE_SECONDS = 0.5
Der Wert soll in der Config änderbar sein.
4. Logging
Jeder Sendebefehl muss geloggt werden:
timestamp
action
channel
muted target
fader_hex used
payload sent
success/failure
reason if blocked
Blockierte Befehle sind besonders wichtig.
Beispiel:
BLOCKED channel=2 action=mute reason="fader value stale" age=4.28s
SENT channel=2 action=mute fader=1f14b643 payload=...
5. Kein Blind Send
Es darf keine Funktion geben, die ohne State Check rohe Bytes an den Mixer schickt, außer optional in einem klar benannten Debug-Modus.
Falls Debug-Modus eingebaut wird:
ALLOW_UNSAFE_DEBUG_SEND = False
Default muss False sein.

⸻

Parser für empfangene Frames
Der Service muss TCP-Daten lesen und daraus bekannte Kanalstatus-Frames erkennen.
Zu erkennen sind mindestens diese Muster:
App/Mixer-State kurz
5a5a5a5a5a5a0000006e000000070708012d1f14b643
5a5a5a5a5a5a0000006e0000000909080120012d1f14b643
Mixer-Status lang mit Padding
Hardware-Mute/Unmute vom Mixer zur App sah so aus:
Unmute Kanal 2:
5a5a5a5a5a5a0000006d000001000708012d00005143...
Mute Kanal 2:
5a5a5a5a5a5a0000006d0000010009080120012d00005143...
Hier enthält der Statusframe nach dem eigentlichen State sehr viele Nullen/Padding.
Wichtig: Der Parser soll nicht voraussetzen, dass ein TCP-Read exakt einem Frame entspricht. TCP ist ein Stream. Ein Read kann halbe Frames oder mehrere Frames enthalten.
Implementiere daher einen robusten Buffer-basierten Parser:
1. Eingehende Bytes an Buffer anhängen.
2. Nach Magic suchen:
5a5a5a5a5a5a
3. Alles vor Magic verwerfen.
4. Bekannte Prefixe erkennen:
    * 5a5a5a5a5a5a0000006e000000
    * 5a5a5a5a5a5a0000006d00000100
5. Danach nach bekannten Payload-Mustern suchen.
6. State extrahieren.
7. Buffer nicht unendlich wachsen lassen.
Da das Protokoll nicht vollständig dokumentiert ist, darf der Parser pragmatisch arbeiten und bekannte Muster mit Regex/Hex-Suche erkennen.
Praktischer Parser-Ansatz
Konvertiere empfangene Bytes optional zu Hex-String und suche nach Mustern.
Für 6e-Frames:
Mute:
5a5a5a5a5a5a0000006e000000090908([0-9a-f]{2})20012d([0-9a-f]{8})
Unmute:
5a5a5a5a5a5a0000006e000000070708([0-9a-f]{2})2d([0-9a-f]{8})
Für 6d-Frames:
Mute:
5a5a5a5a5a5a0000006d000001000908([0-9a-f]{2})20012d([0-9a-f]{8})
Unmute:
5a5a5a5a5a5a0000006d000001000708([0-9a-f]{2})2d([0-9a-f]{8})
Beim Match:
channel = int(channel_index_hex, 16) + 1
fader_hex = matched_fader_hex
muted = True/False
Dann State aktualisieren.
Achtung: Der Regex-Ansatz ist für den Anfang okay, aber der Code sollte so geschrieben sein, dass später ein bytebasierter Parser ergänzt werden kann.

⸻

Verbindung und Listener
Unklar ist, ob der Service einfach eine zusätzliche TCP-Verbindung zum Mixer öffnen kann und Statusupdates erhält, oder ob Statusupdates nur an bestehende iPad-Verbindungen gehen.
Implementiere daher diese Phasen:
Phase 1: Direkte TCP-Verbindung zum Mixer
Der Service verbindet sich zu:
(MIXER_IP, MIXER_PORT)
und versucht dauerhaft zu lesen.
Wenn dort Statusframes eintreffen, werden sie geparst.
Phase 2: Falls keine Statusupdates eintreffen
Wenn der Mixer keine Updates an zusätzliche Clients sendet, muss der Service wenigstens beim Senden die Antworten lesen. Es könnte aber unzureichend sein.
Dann gibt es zwei mögliche Erweiterungen:
1. herausfinden, ob beim Verbindungsaufbau ein Initial-Sync-Request gesendet werden muss,
2. oder Traffic passiv mitschneiden.
Für die erste Version reicht direkte TCP-Verbindung plus Logging, aber der Code soll klar anzeigen:
state status: no recent mixer updates received
und Sendebefehle blockieren, solange keine frischen Faderwerte vorhanden sind.
Reconnect
Der Listener muss reconnecten:
- bei Verbindungsfehler
- bei Timeout
- bei Connection reset
Reconnect mit Backoff, z. B.:
1s, 2s, 5s, 10s
Maximal 10s.

⸻

Command-Erzeugung
Implementiere eine Funktion:
def build_mute_payload(channel: int, muted: bool, fader_hex: str) -> bytes:
    ...
Anforderungen:
COMMAND_PREFIX = "5a5a5a5a5a5a0000006e000000"
Kanal prüfen:
1 <= channel <= MAX_CHANNELS
Default:
MAX_CHANNELS = 8
Kann später konfigurierbar sein.
Payload:
idx = channel - 1

if muted:
    payload_hex = f"090908{idx:02x}20012d{fader_hex}"
else:
    payload_hex = f"070708{idx:02x}2d{fader_hex}"

full_hex = COMMAND_PREFIX + payload_hex
return bytes.fromhex(full_hex)
Validiere:
len(fader_hex) == 8
all chars in 0-9a-f

⸻

Command-Senden
Implementiere:
async def send_channel_mute(channel: int, muted: bool) -> SendResult:
    ...
Ablauf:
1. Prüfe global enabled.
2. Hole State für Kanal.
3. Prüfe fader_hex.
4. Prüfe Alter.
5. Baue Payload.
6. Öffne TCP-Verbindung oder nutze persistente Sender-Verbindung.
7. Sende Bytes.
8. Logge Erfolg.
9. Optional: Warte kurz auf Statusbestätigung, falls Listener aktiv ist.
10. Return JSON-kompatibles Ergebnis.
Keine stillen Fehler. Jede Blockade soll eine klare Reason liefern.
Beispiel Response bei Erfolg:
{
  "ok": true,
  "channel": 2,
  "target_muted": true,
  "fader_hex": "1f14b643",
  "sent_hex": "5a5a..."
}
Beispiel Response bei Blockade:
{
  "ok": false,
  "channel": 2,
  "reason": "fader value stale",
  "age_seconds": 3.21
}

⸻

Gruppenlogik
Config-Datei, z. B. config.yaml:
mixer:
  ip: "192.168.188.30"
  port: 6400
  max_channels: 8

safety:
  max_fader_age_seconds: 1.0
  allow_partial_groups: false

groups:
  drums:
    channels: [1, 2, 3, 4]
  vocals:
    channels: [5, 6]
  all:
    channels: [1, 2, 3, 4, 5, 6, 7, 8]
Gruppen-Mute:
POST /groups/drums/mute
Ablauf:
1. Gruppe existiert?
2. Alle Kanäle validieren.
3. Für alle Kanäle Safety Check durchführen.
4. Wenn irgendein Kanal unsicher ist: abbrechen, nichts senden.
5. Wenn alle sicher: nacheinander senden.
6. Kleiner Delay zwischen Sendungen, z. B. 30–100 ms, konfigurierbar.
7. Ergebnis für alle Kanäle zurückgeben.
Wichtig: Default keine Partial-Gruppenaktion.

⸻

Toggle-Logik
Toggle ist schwieriger als Mute/Unmute, weil der Service dafür den aktuellen Mute-Status kennen muss.
Für einzelne Kanäle:
POST /channels/{channel}/toggle
Nur erlauben, wenn:
muted is not None
muted_updated_at frisch genug
Dann:
target = not current_muted
Für Gruppen gibt es zwei mögliche Semantiken:
Empfohlen: Gruppe ist aktiv, wenn alle Kanäle gemutet sind
if all channels muted:
    unmute group
else:
    mute group
Auch hier nur wenn alle Mute-States bekannt und frisch genug sind.
Alternativ kann der Service eigene Gruppen-Zustände speichern. Aber das ist riskanter, wenn jemand parallel am Mixer/iPad mutet. Besser ist der echte beobachtete Mixer-State.

⸻

HTTP API Details
GET /health
Antwort:
{
  "ok": true,
  "enabled": true,
  "mixer_connected": true,
  "last_update_age_seconds": 0.12
}
GET /state
Antwort mit allen Kanälen:
{
  "enabled": true,
  "channels": {
    "1": {
      "fader_hex": "....",
      "fader_age_seconds": 0.4,
      "muted": false,
      "mute_age_seconds": 0.4,
      "safe": true
    }
  }
}
GET /channels/{channel}
Antwort für einen Kanal.
POST /channels/{channel}/mute
Setzt Kanal-Mute auf an, wenn sicher.
POST /channels/{channel}/unmute
Setzt Kanal-Mute auf aus, wenn sicher.
POST /channels/{channel}/toggle
Toggled Kanal-Mute, wenn Fader und Mute-State sicher bekannt sind.
POST /groups/{group}/mute
Mute für Gruppe.
POST /groups/{group}/unmute
Unmute für Gruppe.
POST /groups/{group}/toggle
Toggle-Gruppe.
POST /panic/disable
Deaktiviert alle Sendebefehle. State Tracking darf weiterlaufen.
POST /panic/enable
Aktiviert Sendebefehle wieder.

⸻

Stream Deck Integration
Stream Deck soll keine Bytes direkt senden.
Stattdessen sollen Buttons HTTP-Requests ausführen.
Beispiele:
curl -X POST http://localhost:8765/groups/drums/toggle
curl -X POST http://localhost:8765/channels/2/mute
curl -X POST http://localhost:8765/channels/2/unmute
Für Windows eventuell PowerShell:
Invoke-WebRequest -Method POST http://localhost:8765/groups/drums/toggle

⸻

Sicherheitsverhalten bei Fehlern
Bei Unsicherheit immer blockieren.
Blockieren bei:
- keine Mixer-Verbindung
- kein Faderwert
- veralteter Faderwert
- ungültiger Faderwert
- unbekannter Kanal
- unbekannte Gruppe
- Service disabled
- Mute-State für Toggle unbekannt
- bei Gruppe: irgendein Kanal unsicher
Der Service soll dann HTTP 409 oder 400 mit klarer JSON-Fehlermeldung liefern.
Beispiel:
{
  "ok": false,
  "reason": "unsafe group operation",
  "details": [
    {
      "channel": 4,
      "reason": "fader value stale",
      "age_seconds": 2.84
    }
  ]
}

⸻

Projektstruktur
Vorschlag:
mixer_mute_service/
  app/
    __init__.py
    main.py
    config.py
    protocol.py
    state.py
    mixer_client.py
    safety.py
    routes.py
    models.py
  config.yaml
  requirements.txt
  README.md
  systemd/
    mixer-mute-service.service
protocol.py
Enthält:
COMMAND_PREFIX
STATUS_PREFIXES
build_mute_command()
parse_frames()
is_valid_fader_hex()
state.py
Thread-/async-sicherer State Store.
Wichtig: Da Listener und HTTP-Routes parallel laufen, State mit Lock schützen.
asyncio.Lock
mixer_client.py
TCP-Verbindung, Reconnect, Listener, Sender.
safety.py
Safety Checks für Kanäle und Gruppen.
routes.py
FastAPI-Endpunkte.
main.py
Startet FastAPI und Background Listener.

⸻

Beispielcode für zentrale Funktionen
Validierung Faderwert
def is_valid_fader_hex(value: str) -> bool:
    if not isinstance(value, str):
        return False
    if len(value) != 8:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True
Command bauen
COMMAND_PREFIX = "5a5a5a5a5a5a0000006e000000"

def build_mute_command(channel: int, muted: bool, fader_hex: str) -> bytes:
    if not 1 <= channel <= 8:
        raise ValueError(f"Invalid channel: {channel}")

    if not is_valid_fader_hex(fader_hex):
        raise ValueError(f"Invalid fader_hex: {fader_hex}")

    idx = channel - 1

    if muted:
        payload = f"090908{idx:02x}20012d{fader_hex}"
    else:
        payload = f"070708{idx:02x}2d{fader_hex}"

    return bytes.fromhex(COMMAND_PREFIX + payload)
Parser-Idee
import re
import time

PATTERNS = [
    # 6e mute
    (re.compile(r"5a5a5a5a5a5a0000006e000000090908([0-9a-f]{2})20012d([0-9a-f]{8})"), True),
    # 6e unmute
    (re.compile(r"5a5a5a5a5a5a0000006e000000070708([0-9a-f]{2})2d([0-9a-f]{8})"), False),
    # 6d mute
    (re.compile(r"5a5a5a5a5a5a0000006d000001000908([0-9a-f]{2})20012d([0-9a-f]{8})"), True),
    # 6d unmute
    (re.compile(r"5a5a5a5a5a5a0000006d000001000708([0-9a-f]{2})2d([0-9a-f]{8})"), False),
]

def parse_hex_stream(hex_data: str):
    hex_data = hex_data.lower()
    events = []

    for pattern, muted in PATTERNS:
        for match in pattern.finditer(hex_data):
            channel_index = int(match.group(1), 16)
            fader_hex = match.group(2)
            channel = channel_index + 1

            events.append({
                "channel": channel,
                "muted": muted,
                "fader_hex": fader_hex,
                "timestamp": time.time(),
                "raw_match": match.group(0),
            })

    return events

⸻

Offene Fragen / Unsicherheiten
1. Es ist noch nicht sicher, ob der Mixer einer zusätzlichen TCP-Verbindung automatisch Statusupdates sendet.
2. Es ist noch nicht bekannt, ob ein Initial-Sync-Request nötig ist.
3. Es wurde noch kein echter Mute-only-Befehl gefunden.
4. Das Protokoll ist reverse-engineered und nicht vollständig bekannt.
5. Die ersten Bytes 07 07, 09 09 wirken wie Länge/Typ, sind aber nicht offiziell bestätigt.
6. Das 6d-Frame ist offenbar Mixer → iPad Status und wird vom Mixer nicht als Steuerbefehl akzeptiert.
7. Kanalanzahl aktuell vermutlich 8, aber konfigurierbar machen.

⸻

Akzeptanzkriterien
Die App gilt als erfolgreich, wenn:
1. Service startet lokal.
2. Service verbindet sich mit dem Mixer.
3. Service erkennt eingehende Mute/Fader-Statusframes.
4. /state zeigt pro Kanal aktuelle Faderwerte und Mute-Status.
5. /channels/2/mute sendet nur, wenn Kanal 2 einen frischen Faderwert hat.
6. Beim Senden wird der aktuelle Faderwert wiederverwendet.
7. Fader bewegt sich beim Mute/Unmute nicht.
8. Wenn der Faderwert unbekannt/veraltet ist, wird nicht gesendet.
9. Gruppenaktionen werden vollständig blockiert, wenn ein Kanal unsicher ist.
10. Stream Deck kann per HTTP-Endpunkt Mutegruppen triggern.
11. Logs zeigen alle gesendeten und blockierten Aktionen nachvollziehbar.

⸻

Wichtigste Designentscheidung
Safety first.
Lieber einen Button-Klick ignorieren/blockieren als einen Fader live auf eine falsche Position setzen.
Es darf niemals passieren, dass der Service bei unbekanntem State „irgendeinen“ Faderwert sendet.

⸻

Minimaler erster Meilenstein
Baue zuerst nur:
GET /health
GET /state
POST /channels/{channel}/mute
POST /channels/{channel}/unmute
und hardcode vorerst:
max_channels = 8
mixer_ip = 192.168.188.30
mixer_port = 6400
max_fader_age_seconds = 1.0
Dann:
1. Listener implementieren.
2. Parser implementieren.
3. State aktualisieren.
4. Safety Check implementieren.
5. Senden implementieren.
Erst danach Gruppen, Toggle, Config-Datei, Stream-Deck-Dokumentation und systemd ergänzen.

⸻

Beispiel-Testablauf
1. Service starten.
2. iPad-App oder Hardware nutzen und Kanal 2 bewegen/muten, damit der Service State sieht.
3. GET /state aufrufen.
4. Prüfen, ob Kanal 2 fader_hex und muted zeigt.
5. Auf Testkanal ohne Signal:
curl -X POST http://localhost:8765/channels/2/mute
6. Prüfen:
    * Kanal ist gemutet.
    * Fader bleibt unverändert.
    * Log zeigt verwendeten Faderwert.
7. Dann:
curl -X POST http://localhost:8765/channels/2/unmute
8. Prüfen:
    * Kanal ist ungemutet.
    * Fader bleibt unverändert.

⸻

Nicht bauen / vermeiden
Bitte nicht:
1. keine Stream-Deck-Buttons, die rohe Bytes mit hardcodiertem Faderwert senden,
2. keine Default-Faderwerte verwenden,
3. keine Mutegruppen teilweise senden, wenn ein Kanal unsicher ist,
4. keine Dummy-Werte für Fader verwenden,
5. keine automatische Recovery-Aktion, die Fader verändern könnte,
6. keine Annahme, dass der Mixer-State bekannt ist, nur weil der Service gerade gestartet wurde.

⸻

Zusammenfassung des bekannten Protokolls
Command an Mixer:
PREFIX = 5a5a5a5a5a5a0000006e000000
Kanal N:
XX = N - 1 als 1 Byte Hex
Mute:
PREFIX + 090908XX20012dFFFFFFFF
Unmute:
PREFIX + 070708XX2dFFFFFFFF
FFFFFFFF ist der aktuelle Faderwert, exakt 4 Bytes / 8 Hex-Zeichen.
Beispiele Kanal 2:
Mute:
5a5a5a5a5a5a0000006e0000000909080120012d1f14b643

Unmute:
5a5a5a5a5a5a0000006e000000070708012d1f14b643
Status vom Mixer:
5a5a5a5a5a5a0000006d000001000908XX20012dFFFFFFFF...
5a5a5a5a5a5a0000006d000001000708XX2dFFFFFFFF...
Diese Statusframes können lang sein und viel Null-Padding enthalten.

⸻

Web-Oberfläche: iPad UND Desktop gleichwertig
Die Weboberfläche (mixer_mute_service/app/static/index.html) wird auf einem 13-Zoll-iPad-Pro in Safari bedient (Querformat und Hochformat) UND genauso auf Desktop-PCs im Browser (Maus, breite Bildschirme). Beide sind gleich wichtig: keine Änderung darf eine der beiden Umgebungen schlechter machen.
Regeln für jede UI-Änderung:
1. Ohne horizontales Scrollen in allen Zielgrößen: iPad Querformat ca. 1376 × 1032 CSS-Pixel, Hochformat ca. 1032 × 1376 (ältere 12,9-Zoll-Modelle: 1366 × 1024); Desktop 1280 × 800, 1440 × 900, 1920 × 1080 und 2560 × 1440.
2. Auf breiten Desktop-Bildschirmen sauber ausrichten: Bedienelemente eines Abschnitts (z. B. der Kompakt-Schalter) schließen bündig mit dessen Inhalt ab und stehen nicht verloren im leeren Raum; keine großen ungenutzten Flächen ohne Grund.
3. Alles, was angetippt wird, mindestens 44 × 44 Pixel groß (die sichtbare Form darf kleiner sein, die Tippfläche nicht); Abschnitts-Köpfe (Desktop, Channels, Mute Gruppen) sind über die ganze Zeile antippbar und mindestens 52 Pixel hoch.
4. Keine Funktion nur per Hover oder Tastatur erreichbar machen; Ziehen (Fader) muss mit dem Finger und mit der Maus gehen.
5. Vor dem Commit im Browser in den iPad-Größen (1376 × 1032, 1032 × 1376) und mindestens in 1920 × 1080 und 1280 × 800 prüfen.
Die API-Dokumentation steht nicht auf der Seite, sondern in der Swagger-Oberfläche unter /docs.
