# Mixer Mute Service – Arbeitsregeln

Lokaler FastAPI-Dienst (`mixer_mute_service/`) für ein Studiomaster-DigiLive-16-Mischpult (192.168.188.30, TCP 6400). Er läuft als Windows-Dienst auf dem Streaming-PC (192.168.188.20) und steuert Mutegruppen, Fader, Programm, Lieder, Kameras, Licht, OBS und den Beamer. Bedient wird er über `app/static/` vom iPad und von Desktop-PCs. Überblick, API und Konfiguration stehen im README von `mixer_mute_service`; das ursprüngliche Briefing liegt nur noch als Archiv in `docs/history/briefing.md` (veraltet).

## Sicherheit am Mischpult (oberste Regel)

Jeder Mute- oder Fader-Befehl schreibt Fader, Mute **und** Solo gleichzeitig. Ein falscher Wert verstellt live das Pult, deshalb gilt:
- Nie senden ohne Wert, der auf der aktuellen Verbindung vom Pult beobachtet wurde. Keine Default- oder Dummy-Werte, keine rohen Bytes, kein Senden an der Sicherheitsprüfung (`app/safety.py`, `app/controller.py`) vorbei.
- Unbekannt heißt gesperrt: unbekannter Fader, Mute- oder Solo-Zustand, unbekanntes Feld in einer Kanalmeldung, keine oder eine tote Verbindung, Panic.
- Gruppen nur ganz oder gar nicht. Ein unsicherer Kanal blockiert die ganze Aktion.
- Werte von vor einem Reconnect zählen nicht. Im Direkt-Modus kommt alle 10 s ein Zustands-Dump.
- Lieber eine Aktion blockieren (mit klarem `reason`) als einen Fader falsch setzen.

## Protokoll (am Pult verifiziert)

- **Frame:** `5a×6 | Typ u32 BE | Länge u32 BE | varint msg_len | protobuf`. Typen: `6e` Befehl / `6d` Status (Eingänge), `72` / `71` (Busse), `01` kompletter Zustands-Dump (Antwort auf den Subscribe-Handshake).
- **Eingangs-Abschnitt:** 1 Index (für Kanal 1 weggelassen; Kanal 1 hat stattdessen das feste Feld `15 00abc93d`), 3 Solo, 4 Mute, 5 Fader. **Bus-Abschnitt:** 1 Objekt-ID, 2 Solo, 3 Mute, 7 Fader.
- **Fader:** little-endian float32 von 0 bis 1000; 0 = −∞, das Feld wird dann weggelassen; 750 ≈ 0 dB. Nie Werte außerhalb 0..1000 senden (das hat das Pult schon einmal abstürzen lassen).
- **Dump:** Eingänge in den Top-Level-Feldern 1/2, Busse in 3. Die Kanal-Bearbeitung (Gain, HPF, EQ, Gate, Kompressor) steht in den Objektfeldern 3/4/5; die Feldbelegung steht in `app/channel_settings.py`.
- Das Pult schickt eigene Befehle eines Clients nicht an andere Clients zurück.

## Entwicklung

- **Tests:** alle Dateien in `mixer_mute_service/tests/`, aus `mixer_mute_service` mit `python -m tests.<name>` (bzw. `python -m unittest discover -s tests -t . -v`). Neue Funktionen bekommen Tests.
- **Kein Test, Skript oder Tool darf das echte Pult oder andere Geräte im LAN ansprechen.** Der Entwicklungs-Mac hängt oft im Mixer-Netz.
- **Am Pult messen:** Wenn etwas am Pult nachgewiesen werden muss, nur lesend (Subscribe-Handshake, keine Befehle) und nur mit Zustimmung des Nutzers.
- **Mehrere Sessions:** Jede arbeitet auf ihrem eigenen Branch. Eine Session führt zusammen, testet und pusht.

## Release

- Jeder Push auf `main` startet die CI (Tests, dann Windows-Release-Build). Der PC holt das Release über den UPDATE-Knopf.
- Nach einem Push den Build bis zum fertigen Release abwarten (`gh run watch`), bevor etwas als erledigt gemeldet wird.
- PowerShell: In Strings `"${var}:"` schreiben, nie `"$var:"` – das hat den Build schon einmal gebrochen.

## Weboberfläche: iPad UND Desktop gleichwertig

Die Weboberfläche (mixer_mute_service/app/static/: index.html, css/app.css, js/*.js) wird auf einem 13-Zoll-iPad-Pro in Safari bedient (Querformat und Hochformat) UND genauso auf Desktop-PCs im Browser (Maus, breite Bildschirme). Beide sind gleich wichtig: keine Änderung darf eine der beiden Umgebungen schlechter machen.
Regeln für jede UI-Änderung:
1. Ohne horizontales Scrollen in allen Zielgrößen: iPad Querformat ca. 1376 × 1032 CSS-Pixel, Hochformat ca. 1032 × 1376 (ältere 12,9-Zoll-Modelle: 1366 × 1024); Desktop 1280 × 800, 1440 × 900, 1920 × 1080 und 2560 × 1440.
2. Auf breiten Desktop-Bildschirmen sauber ausrichten: Bedienelemente eines Abschnitts (z. B. der Kompakt-Schalter) schließen bündig mit dessen Inhalt ab und stehen nicht verloren im leeren Raum; keine großen ungenutzten Flächen ohne Grund.
3. Alles, was angetippt wird, mindestens 44 × 44 Pixel groß (die sichtbare Form darf kleiner sein, die Tippfläche nicht); Abschnitts-Köpfe (Desktop, Channels, Mute Gruppen) sind über die ganze Zeile antippbar und mindestens 52 Pixel hoch.
4. Keine Funktion nur per Hover oder Tastatur erreichbar machen; Ziehen (Fader) muss mit dem Finger und mit der Maus gehen.
5. Vor dem Commit im Browser in den iPad-Größen (1376 × 1032, 1032 × 1376) und mindestens in 1920 × 1080 und 1280 × 800 prüfen.
Die API-Dokumentation steht nicht auf der Seite, sondern in der Swagger-Oberfläche unter /docs.
