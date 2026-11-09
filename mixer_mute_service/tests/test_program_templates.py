"""Program templates (app/program_templates.py).

Verifies: saving the program as a template (song slots emptied, fixed songs
kept, name clash only with replace), applying fills the slots in order,
leaves extra slots empty, adds extra songs right after the last slot, keeps an
unknown line in the title, replaces the program and resets start times;
pasted lines are matched to the library by title, other title and book
number, unclear lines are left with candidates.
"""

import os
import pathlib
import tempfile

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="mixer_templates_"))
(_tmp / "config.yaml").write_text("mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n")
os.environ["MIXER_CONFIG"] = str(_tmp / "config.yaml")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.program_templates import clean_line, match_lines  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def song(title, books=(), titles=None):
    return {"title": title, "titles": titles or [title], "books": [{"name": n, "number": x} for n, x in books],
            "copyright": "", "slides": [{"key": "1", "text": "a"}]}


LIBRARY = {
    "Gott_ du bist da.foil": song("Gott, du bist da", [("Feiert Jesus 3", "123")]),
    "Grosser Gott.foil": song("Großer Gott, wir loben dich", [("Evangelisches Gesangbuch", "331")]),
    "Grosser Gott wir.foil": song("Großer Gott, wir danken", []),
    "In Christus.foil": song("In Christus ist mein ganzer Halt", [], ["In Christus ist mein ganzer Halt", "In Christ alone"]),
    "Dir sei Ehre.foil": song("Dir sei Ehre", []),
}


def matching():
    print("== matching ==")
    check("list markers removed", clean_line("1. Gott, du bist da") == "Gott, du bist da" and clean_line("- Dir sei Ehre") == "Dir sei Ehre")
    m = {r["line"]: r for r in match_lines([
        "Gott du bist da", "1) dir sei ehre", "In Christ alone", "EG 331", "Großer Gott", "Unbekanntes Lied", "  ",
        "gross er gott wir loben dich",
    ], LIBRARY)}
    check("blank lines skipped", "" not in m and len(m) == 7)
    check("title without punctuation", m["Gott du bist da"]["song_id"] == "Gott_ du bist da.foil")
    check("case and numbering", m["dir sei ehre"]["song_id"] == "Dir sei Ehre.foil")
    check("other title (English)", m["In Christ alone"]["song_id"] == "In Christus.foil")
    check("book number with initials", m["EG 331"]["song_id"] == "Grosser Gott.foil")
    check("two songs start the same -> left open with both as candidates",
          m["Großer Gott"]["song_id"] is None
          and {c["song_id"] for c in m["Großer Gott"]["candidates"]} >= {"Grosser Gott.foil", "Grosser Gott wir.foil"})
    check("unknown song left open", m["Unbekanntes Lied"]["song_id"] is None)
    check("empty library: nothing matched", match_lines(["Dir sei Ehre"], {})[0]["song_id"] is None)


def row(time, title, **extra):
    return {"time": time, "title": title, "person": "", "group": "", **extra}


def api():
    with TestClient(app) as client:
        app.state.songs.songs = dict(LIBRARY)
        print("== save ==")
        ids = []
        for body in [
            row("10:00", "Begrüßung"),
            row("10:05", "Lobpreis", song="Dir sei Ehre.foil", lyrics_description="Text"),
            row("10:10", "Lobpreis", song="In Christus.foil"),
            row("10:30", "Predigt"),
            row("11:00", "Segenslied", song="Grosser Gott.foil"),
            row("11:05", "Ende"),
        ]:
            ids.append(client.post("/program/items", json=body).json()["item"]["id"])
        r = client.post("/program/templates", json={"name": "Gottesdienst", "slots": [ids[1], ids[2]]})
        tpl = r.json()["template"]
        check("saved", r.status_code == 200 and tpl["slot_count"] == 2 and len(tpl["rows"]) == 6)
        check("slot songs emptied, fixed song kept",
              tpl["rows"][1]["song"] is None and tpl["rows"][4]["song"] == "Grosser Gott.foil"
              and tpl["rows"][1]["lyrics_description"] == "Text")
        check("same name refused", client.post("/program/templates", json={"name": "gottesdienst", "slots": []}).status_code == 409)
        r = client.post("/program/templates", json={"name": "Gottesdienst", "slots": [ids[1]], "replace": True})
        check("replaced on request", r.status_code == 200 and r.json()["template"]["id"] == tpl["id"]
              and r.json()["template"]["slot_count"] == 1)
        client.post("/program/templates", json={"name": "Gottesdienst", "slots": [ids[1], ids[2]], "replace": True})
        check("bad input refused", client.post("/program/templates", json={"name": " ", "slots": []}).status_code == 400
              and client.post("/program/templates", json={"name": "X", "slots": ["nope"]}).status_code == 404)
        check("listed", [t["name"] for t in client.get("/program/templates").json()["templates"]] == ["Gottesdienst"])

        print("== apply ==")
        client.post(f"/program/items/{ids[0]}/play")
        apply = lambda songs: client.post(f"/program/templates/{tpl['id']}/apply", json={"songs": songs})
        before = client.get("/program/items").json()
        r = client.post(f"/program/templates/{tpl['id']}/apply", json={"dry_run": True, "songs": [
            {"song_id": "Gott_ du bist da.foil"}, {"song_id": "Dir sei Ehre.foil"}, {"song_id": "In Christus.foil"}]})
        check("dry run: rows returned, program unchanged",
              [row["song"] for row in r.json()["rows"]][1:4] == ["Gott_ du bist da.foil", "Dir sei Ehre.foil", "In Christus.foil"]
              and client.get("/program/items").json() == before)
        r = apply([{"song_id": "Gott_ du bist da.foil", "text": "Gott du bist da"},
                   {"song_id": None, "text": "1. Neues Lied"},
                   {"song_id": "Dir sei Ehre.foil", "text": ""},
                   {"song_id": "In Christus.foil", "text": ""}])
        items = r.json()["items"]
        check("applied", r.status_code == 200 and r.json()["warnings"] == [])
        check("slots filled in order, extra songs right after the last slot",
              [(i["title"], i["song"]) for i in items] == [
                  ("Begrüßung", None),
                  ("Lobpreis", "Gott_ du bist da.foil"),
                  ("Lobpreis: Neues Lied", None),
                  ("Lobpreis", "Dir sei Ehre.foil"),
                  ("Lobpreis", "In Christus.foil"),
                  ("Predigt", None),
                  ("Segenslied", "Grosser Gott.foil"),
                  ("Ende", None),
              ])
        check("extra rows copy the slot", items[3]["time"] == "10:10" and items[3]["lyrics_description"] == "")
        check("program replaced, start times reset",
              len(client.get("/program/items").json()) == 8 and all(i["last_played_at"] is None for i in items)
              and client.get("/program/status").json()["next_id"] == items[0]["id"])
        r = apply([{"song_id": "Dir sei Ehre.foil"}])
        check("fewer songs: other slots stay empty",
              [i["song"] for i in r.json()["items"]][1:3] == ["Dir sei Ehre.foil", None])
        check("unknown song refused, program kept",
              apply([{"song_id": "Fehlt.foil"}]).status_code == 404 and len(client.get("/program/items").json()) == 6)
        check("unknown template", client.post("/program/templates/nope/apply", json={"songs": []}).status_code == 404)

        print("== template without slots ==")
        r = client.post("/program/templates", json={"name": "Kurz", "slots": []})
        short = r.json()["template"]
        r = client.post(f"/program/templates/{short['id']}/apply", json={"songs": [
            {"song_id": "Dir sei Ehre.foil", "text": "dir sei ehre"}, {"song_id": None, "text": "Fehlt noch"}]})
        tail = [(i["time"], i["title"], i["song"]) for i in r.json()["items"]][-2:]
        check("songs added at the end, named after the song", tail == [
            ("11:05", "Dir sei Ehre", "Dir sei Ehre.foil"), ("11:05", "Fehlt noch", None)])

        print("== match endpoint, delete ==")
        r = client.post("/songs/match", json={"lines": ["Dir sei Ehre", "xyz"]})
        check("match endpoint", r.status_code == 200 and r.json()["library_loaded"]
              and [m["song_id"] for m in r.json()["matches"]] == ["Dir sei Ehre.foil", None])
        check("match input checked", client.post("/songs/match", json={"lines": "x"}).status_code == 400)
        check("deleted", client.delete(f"/program/templates/{short['id']}").status_code == 200
              and client.delete(f"/program/templates/{short['id']}").status_code == 404)

    with TestClient(app) as client:
        check("kept across restarts", [t["name"] for t in client.get("/program/templates").json()["templates"]] == ["Gottesdienst"])


def main():
    matching()
    api()
    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
