"""Song display (app/foil.py, app/songs.py) with the display agent stubbed.

Verifies: .foil parsing (order with a repeated refrain, old DOCTYPE accepted,
entities refused, songs without text refused), settings validation, reading
the library through the agent, the slide sequence show -> next -> ... ->
blank after the last slide -> back, blank/hide, that a failing agent leaves
the live state alone, and program rows with a song (unknown songs refused,
starting a row shows its first slide).
"""

import json
import os
import urllib.error
from urllib.parse import quote

from tests.helpers import use_app_config  # noqa: E402

_tmp = use_app_config("mixer_songs_",
    "desktop_display:\n  enabled: true\n  token_env: SONG_TEST_TOKEN\n  profiles: {}\n")
os.environ["SONG_TEST_TOKEN"] = "test-token"

from fastapi.testclient import TestClient  # noqa: E402

import app.songs as songs  # noqa: E402
from app.foil import FoilError, parse_foil  # noqa: E402
from app.main import app  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


SONG = """<?xml version="1.0" encoding="UTF-8"?>
<foilpresenterfolie version="00300.000105">
<titel><titelstring>Allein deine Gnade</titelstring><titelstring>Your grace</titelstring></titel>
<reihenfolge><name>Standard</name>
<strophennummer>2</strophennummer><strophennummer>0</strophennummer>
<strophennummer>2</strophennummer><strophennummer>1</strophennummer>
<strophennummer>7</strophennummer></reihenfolge>
<strophen>
<strophe><key>1</key><text>Erste Strophe
zweite Zeile</text></strophe>
<strophe><key>2</key><text>Zweite Strophe</text></strophe>
<strophe><key>Ref</key><text>Refrain</text></strophe>
</strophen>
<copyright><font>Verdana</font><text>Text und Melodie: Test</text></copyright>
<buch><bucheintrag><name>Glauben Hoffen Singen</name><nummer>371</nummer></bucheintrag></buch>
</foilpresenterfolie>"""

OLD = """<?xml version="1.0" encoding="UTF-8"?><!DOCTYPE foilpresenterfolie SYSTEM "http://www.andreasbe.de/foil/foil.dtd">
<foilpresenterfolie version="00204.029004"><titel><titelstring>Lobe den Herren</titelstring></titel>
<strophen><strophe><key>1</key><text>Lobe den Herren</text></strophe><strophe><key>2</key><text>Lobe den Herren, der alles</text></strophe></strophen>
</foilpresenterfolie>"""


class FakeAgent:
    def __init__(self):
        self.calls = []
        self.fail = False
        self.files = []

    def __call__(self, url, token, payload, timeout):
        self.calls.append((url, token, payload))
        if self.fail:
            raise urllib.error.URLError("down")
        if url.endswith("/songs/library"):
            return {"ok": True, "files": self.files, "truncated": False}
        return {"ok": True, "action": payload["action"]}

    def last(self):
        return self.calls[-1][2]


def main():
    print("== foil parsing ==")
    song = parse_foil(SONG.encode())
    check("title and alternative titles", song["title"] == "Allein deine Gnade" and song["titles"] == ["Allein deine Gnade", "Your grace"])
    check("order with repeated refrain, out-of-range index skipped",
          [s["key"] for s in song["slides"]] == ["Ref", "1", "Ref", "2"])
    check("multi-line verse kept", song["slides"][1]["text"] == "Erste Strophe\nzweite Zeile")
    check("copyright and book", song["copyright"] == "Text und Melodie: Test"
          and song["books"] == [{"name": "Glauben Hoffen Singen", "number": "371"}])
    check("old file with external DOCTYPE: verses in file order",
          [s["key"] for s in parse_foil(OLD.encode())["slides"]] == ["1", "2"])
    for name, data in [
        ("entity definitions refused", b'<!DOCTYPE x [<!ENTITY a "b">]><foilpresenterfolie/>'),
        ("not XML refused", b"<foilpresenterfolie>"),
        ("other XML refused", b"<other/>"),
        ("song without text refused", b"<foilpresenterfolie><strophen><strophe><text></text></strophe></strophen></foilpresenterfolie>"),
    ]:
        try:
            parse_foil(data)
            check(name, False)
        except FoilError:
            check(name, True)

    agent = FakeAgent()
    songs._agent_post = agent
    with TestClient(app) as client:
        print("== settings ==")
        s = client.get("/songs/settings").json()
        check("defaults: .11, monitor 2, full screen", s["target_ip"] == "192.168.188.11" and s["monitor"] == 2 and s["fullscreen"])
        r = client.put("/songs/settings", json={"text_color": "red"})
        check("bad colour refused", r.status_code == 400 and "text_color" in r.json()["reason"])
        r = client.put("/songs/settings", json={"library_path": "foilpresenter"})
        check("relative path refused", r.status_code == 400)
        r = client.post("/songs/library/reload")
        check("reload without folder -> 400", r.status_code == 400)
        r = client.put("/songs/settings", json={"library_path": r"C:\Users\Beamer\foilpresenter Bibliothek", "monitor": 3, "text_color": "#FFFF00"})
        check("valid settings saved", r.status_code == 200 and r.json()["settings"]["monitor"] == 3
              and r.json()["settings"]["text_color"] == "#ffff00")
        check("stored in song_display.json", json.loads((_tmp / "song_display.json").read_text())["monitor"] == 3)

        print("== library ==")
        agent.files = [
            {"name": "Allein deine Gnade.foil", "xml": SONG},
            {"name": "Lobe den Herren.foil", "xml": OLD},
            {"name": "Kaputt.foil", "xml": "<nope"},
            {"name": "notes.txt", "xml": SONG},
        ]
        r = client.post("/songs/library/reload")
        check("library read through the agent", r.status_code == 200 and r.json()["count"] == 2
              and [e["file"] for e in r.json()["errors"]] == ["Kaputt.foil"])
        check("agent asked with the folder and token",
              agent.last() == {"path": r"C:\Users\Beamer\foilpresenter Bibliothek"} and agent.calls[-1][1] == "test-token"
              and agent.calls[-1][0] == "http://192.168.188.11:8766/songs/library")
        lst = client.get("/songs").json()["songs"]
        check("list sorted by title with slide counts",
              [(x["title"], x["slide_count"]) for x in lst] == [("Allein deine Gnade", 4), ("Lobe den Herren", 2)])
        r = client.get("/songs/" + quote("Allein deine Gnade.foil"))
        check("one song by file name", r.status_code == 200 and r.json()["slides"][0]["key"] == "Ref")
        check("unknown song -> 404", client.get("/songs/nope.foil").status_code == 404)

        print("== live slides ==")
        r = client.post("/songs/next")
        check("next without a song -> 409", r.status_code == 409)
        r = client.post("/songs/show", json={"song_id": "Allein deine Gnade.foil", "slide": 0})
        p = agent.last()
        check("first slide shown with title and copyright", r.status_code == 200 and p["action"] == "show"
              and p["text"] == "Refrain" and p["title"] == "Allein deine Gnade" and p["copyright"] == "Text und Melodie: Test")
        check("screen settings sent", p["monitor"] == 3 and p["fullscreen"] is True
              and p["style"] == {"font": "Verdana", "font_size": 72, "text_color": "#ffff00", "background_color": "#000000"})
        check("live state", r.json()["song_id"] == "Allein deine Gnade.foil" and r.json()["slide"] == 0
              and r.json()["song"]["title"] == "Allein deine Gnade")
        client.post("/songs/next")
        p = agent.last()
        check("next: slide 2 without title/copyright", p["text"] == "Erste Strophe\nzweite Zeile" and p["title"] == "" and p["copyright"] == "")
        client.post("/songs/next")
        client.post("/songs/next")
        check("on the last slide", client.get("/songs/live").json()["slide"] == 3)
        r = client.post("/songs/next")
        check("next after the last slide blanks", agent.last()["action"] == "blank" and r.json()["blank"] is True)
        n = len(agent.calls)
        r = client.post("/songs/next")
        check("next while dark at the end sends nothing", len(agent.calls) == n and r.json()["blank"] is True)
        client.post("/songs/prev")
        live = client.get("/songs/live").json()
        check("back from dark shows the last slide again", agent.last()["text"] == "Zweite Strophe" and live["slide"] == 3 and not live["blank"])
        client.post("/songs/prev")
        check("back one slide", client.get("/songs/live").json()["slide"] == 2)
        r = client.post("/songs/show", json={"song_id": "Allein deine Gnade.foil", "slide": 9})
        check("missing slide -> 400", r.status_code == 400)

        print("== agent down ==")
        agent.fail = True
        r = client.post("/songs/next")
        live = client.get("/songs/live").json()
        check("502 with reason, live state unchanged", r.status_code == 502 and r.json()["reason"] == "display agent unavailable"
              and live["slide"] == 2)
        agent.fail = False

        print("== blank / hide ==")
        r = client.post("/songs/blank")
        check("blank keeps the song", r.json()["blank"] is True and r.json()["song_id"] == "Allein deine Gnade.foil")
        r = client.post("/songs/hide")
        check("hide closes the window", agent.last() == {"action": "hide"} and r.json()["song_id"] is None)

        print("== program rows ==")
        row = {"time": "10:00", "title": "Lied", "group": "", "song": "nope.foil"}
        r = client.post("/program/items", json=row)
        check("unknown song refused", r.status_code == 400 and r.json()["reason"] == "unknown song")
        r = client.post("/program/items", json={**row, "song": "Lobe den Herren.foil"})
        check("row with a song saved", r.status_code == 200 and r.json()["item"]["song"] == "Lobe den Herren.foil")
        item_id = r.json()["item"]["id"]
        r = client.post(f"/program/items/{item_id}/play")
        check("starting the row shows its first slide", r.status_code == 200 and agent.last()["text"] == "Lobe den Herren"
              and agent.last()["title"] == "Lobe den Herren" and r.json()["song"]["slide"] == 0)
        agent.fail = True
        r = client.post(f"/program/items/{item_id}/play")
        check("display PC down: row start reports it", r.status_code == 502 and "song could not be shown" in r.json()["reason"])
        agent.fail = False

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
