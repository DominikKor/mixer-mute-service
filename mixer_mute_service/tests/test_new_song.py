"""New songs (POST /songs/create, foil.build_foil), display agent stubbed.

Verifies: the .foil written reads back with the same slides, order, keys
(verses numbered, refrain "Ref"), title and copyright; FoilPresenter-style
file names; input checks; the agent is asked to save into the configured
library; the new song is in the list right away; an existing file name is
refused (by the service, and by the agent on the display PC).
"""

import io
import os
import urllib.error

from tests.helpers import use_app_config  # noqa: E402

_tmp = use_app_config("mixer_newsong_",
    "desktop_display:\n  enabled: true\n  token_env: NEWSONG_TEST_TOKEN\n  profiles: {}\n")
os.environ["NEWSONG_TEST_TOKEN"] = "test-token"

from fastapi.testclient import TestClient  # noqa: E402

import app.songs as songs  # noqa: E402
from app.foil import FoilError, build_foil, parse_foil, song_file_name  # noqa: E402
from app.main import app  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


class FakeAgent:
    def __init__(self):
        self.calls = []
        self.exists = False

    def __call__(self, url, token, payload, timeout):
        self.calls.append((url, payload))
        if self.exists:
            raise urllib.error.HTTPError(url, 409, "exists", {}, io.BytesIO(b""))
        return {"ok": True, "name": payload.get("name")}


SONG = {
    "title": "Gott, du bist da",
    "copyright": "Text: Test & Co",
    "slides": ["Erste Strophe\nzweite Zeile", "Refrain <laut>", "Zweite Strophe"],
    "refrain": 1,
    "order": [1, 0, 1, 2, 1],
}


def main():
    print("== foil file ==")
    xml = build_foil("Test", [{"key": "1", "text": "a"}, {"key": "Ref", "text": "r"}], [1, 0, 1])
    text = xml.decode("utf-8")
    check("XML declaration, CRLF, FoilPresenter root", text.startswith('<?xml version="1.0" encoding="UTF-8"?>\r\n<foilpresenterfolie version="00300.000105">'))
    check("empty elements written out like FoilPresenter", "<ccliid></ccliid>" in text and "<ccliid />" not in text)
    check("reads back", [s["key"] for s in parse_foil(xml)["slides"]] == ["Ref", "1", "Ref"])
    for name, args in [
        ("no title", ("  ", [{"key": "1", "text": "a"}], [0])),
        ("empty slide", ("T", [{"key": "1", "text": " "}], [0])),
        ("order out of range", ("T", [{"key": "1", "text": "a"}], [1])),
    ]:
        try:
            build_foil(*args)
            check(f"{name} refused", False)
        except FoilError:
            check(f"{name} refused", True)
    check("file names like FoilPresenter's", song_file_name("All die Fülle ist in dir, o Herr") == "All die Fuelle ist in dir_ o Herr.foil"
          and song_file_name("Wer/was? <neu>") == "Wer_was_ _neu_.foil")

    agent = FakeAgent()
    songs._agent_post = agent
    with TestClient(app) as client:
        print("== create ==")
        r = client.post("/songs/create", json=SONG)
        check("no library folder -> 400", r.status_code == 400 and "folder" in r.json()["reason"])
        client.put("/songs/settings", json={"library_path": r"C:\Users\Beamer\foilpresenter Bibliothek"})
        for name, body in [
            ("missing title", {**SONG, "title": ""}),
            ("empty slide", {**SONG, "slides": ["a", " "]}),
            ("refrain out of range", {**SONG, "refrain": 5}),
            ("order out of range", {**SONG, "order": [0, 7]}),
            ("empty order", {**SONG, "order": []}),
        ]:
            r = client.post("/songs/create", json=body)
            check(f"{name} -> 400", r.status_code == 400 and r.json()["ok"] is False)
        check("nothing sent to the agent for bad input", agent.calls == [])

        r = client.post("/songs/create", json=SONG)
        check("created", r.status_code == 200 and r.json()["song_id"] == "Gott_ du bist da.foil")
        url, payload = agent.calls[-1]
        check("agent asked to save into the library", url == "http://192.168.188.11:8766/songs/save"
              and payload["path"] == r"C:\Users\Beamer\foilpresenter Bibliothek" and payload["name"] == "Gott_ du bist da.foil")
        saved = parse_foil(payload["xml"].encode("utf-8"))
        check("verses numbered, refrain is Ref, order kept", [s["key"] for s in saved["slides"]] == ["Ref", "1", "Ref", "2", "Ref"])
        check("text, title and copyright kept", saved["slides"][1]["text"] == "Erste Strophe\nzweite Zeile"
              and saved["slides"][0]["text"] == "Refrain <laut>" and saved["title"] == "Gott, du bist da"
              and saved["copyright"] == "Text: Test & Co")
        lst = client.get("/songs").json()["songs"]
        check("in the song list right away", [(s["id"], s["slide_count"]) for s in lst] == [("Gott_ du bist da.foil", 5)])
        r = client.post("/songs/show", json={"song_id": "Gott_ du bist da.foil", "slide": 0})
        check("can be shown", r.status_code == 200 and agent.calls[-1][1]["text"] == "Refrain <laut>")

        print("== existing files ==")
        n = len(agent.calls)
        r = client.post("/songs/create", json=SONG)
        check("same name again refused by the service", r.status_code == 409 and len(agent.calls) == n)
        agent.exists = True
        r = client.post("/songs/create", json={**SONG, "title": "Anderes Lied"})
        check("file already on the display PC -> 409", r.status_code == 409 and "already exists" in r.json()["reason"])
        check("not added to the list", "Anderes Lied.foil" not in [s["id"] for s in client.get("/songs").json()["songs"]])

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
