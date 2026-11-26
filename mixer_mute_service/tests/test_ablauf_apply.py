"""A Gottesdienstablauf (songs and persons) applied through a program template.

The program is never replaced: the order updates the rows it maps to (by title
or point of the order, never by position). Such a row keeps its place, ID, play
state and every setting; only the person changes (the order's, empty too) and,
for a song slot, the song and title. No row is deleted or renamed. The
Missionsbericht person "Mission Spotlight" shows the Mission Spotlight row and
hides the normal Missionsbericht; any other name does the opposite and goes
into the normal row.
"""

import asyncio
import unittest
from datetime import datetime
from types import SimpleNamespace

from app.program_templates import _song_keys, is_place_named, switch_spotlight
from tests.helpers import app_client

SETTINGS = {
    "group": "Chor", "light": "licht1", "cam1_preset": 3, "cam2_preset": 5, "camera_wait_seconds": 4.5,
    "cam1_scene": "Cam 1 Nah", "cam2_scene": "Cam 2 Weit", "desktop": ["bild1", "bild2"], "beamer": "unhide",
}
LIBRARY = {
    "neu.foil": {"title": "Neues Lied", "titles": ["Neues Lied"], "books": [], "copyright": "",
                 "slides": [{"key": "1", "text": "a"}]},
    "zweit.foil": {"title": "Zweites Lied", "titles": ["Zweites Lied"], "books": [], "copyright": "",
                   "slides": [{"key": "1", "text": "a"}]},
}
for _file, _title in (("above.foil", "Above all"), ("ach.foil", "Ach mein Herr Jesu"), ("sorgen.foil", "All eure Sorgen")):
    LIBRARY[_file] = {"title": _title, "titles": [_title], "books": [], "copyright": "", "slides": [{"key": "1", "text": "a"}]}


def put(client, time, title, **extra):
    """A program row straight into the store (no check against configured lights or profiles)."""
    store = client.app.state.program_items
    data = {"time": time, "title": title, "person": "", **extra}
    return asyncio.run(store.create(data))


def rows(client):
    return client.get("/program/items").json()


def by_title(client, title):
    return next(r for r in rows(client) if r["title"] == title)


def clear(client):
    for item in rows(client):
        client.delete(f"/program/items/{item['id']}")
    for template in client.get("/program/templates").json()["templates"]:
        client.delete(f"/program/templates/{template['id']}")


def apply(client, template, songs=(), persons=()):
    return client.post(f"/program/templates/{template['id']}/apply", json={
        "songs": list(songs), "persons": [{"id": i, "person": p} for i, p in persons]})


class AblaufApplyTests(unittest.TestCase):
    def test_only_person_and_song_change(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            put(client, "10:00", "Begrüßung", **SETTINGS)
            slot = put(client, "10:05", "Lied 1", song="neu.foil", **SETTINGS)
            put(client, "10:30", "Predigt", **{**SETTINGS, "cam1_preset": 7})
            template = client.post("/program/templates", json={"name": "GD", "slots": [slot["id"]]}).json()["template"]
            # Changed in the program after the template was saved: must survive applying it.
            for item in client.app.state.program_items._items:
                item.update(cam1_preset=9, camera_wait_seconds=6.0, light="licht2", desktop=["bild3"])
            asyncio.run(client.app.state.program_items.set_hidden(by_title(client, "Predigt")["id"], True))
            asyncio.run(client.app.state.program_items.mark_played(by_title(client, "Begrüßung")["id"], datetime.now().astimezone()))
            before = {r["title"]: r for r in rows(client)}
            r = apply(client, template, [{"song_id": "zweit.foil", "text": "Zweites Lied"}],
                      [("begruessung1", "Anna Leiterin"), ("predigt", "J. Prediger")])
            self.assertEqual(r.status_code, 200, r.text)
            after = {r["title"]: r for r in rows(client)}
            self.assertEqual(set(after), set(before))
            self.assertEqual((after["Begrüßung"]["person"], after["Predigt"]["person"]), ("Anna Leiterin", "J. Prediger"))
            self.assertEqual(after["Lied 1"]["song"], "zweit.foil")
            for title, row in before.items():
                for field in ("id", "time", "group", "light", "cam1_preset", "cam2_preset", "camera_wait_seconds",
                              "cam1_scene", "cam2_scene", "desktop", "beamer", "hidden", "last_played_at"):
                    self.assertEqual(after[title][field], row[field], (title, field))
            self.assertEqual(after["Lied 1"]["cam1_preset"], 9)
            self.assertEqual(after["Lied 1"]["desktop"], ["bild3"])
            self.assertTrue(after["Predigt"]["hidden"])
            self.assertIsNotNone(after["Begrüßung"]["last_played_at"])
            self.assertEqual(client.get("/program/status").json()["current_id"], before["Begrüßung"]["id"])

    def test_rows_only_in_the_program_stay_and_nothing_is_reordered_or_renamed(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            put(client, "10:00", "Begrüßung")
            put(client, "10:02", "Technik-Check", person="Tom", cam1_preset=2)  # not in the template
            slot = put(client, "10:05", "Lied 1", **SETTINGS)
            put(client, "10:20", "Gebet")
            put(client, "10:30", "Predigt", person="Alt")
            template = client.post("/program/templates", json={"name": "GD", "slots": [slot["id"]]}).json()["template"]
            # the user's own names and a row the template has not got
            program = client.app.state.program_items._items
            program[0]["title"] = "Willkommen heute"
            put(client, "10:25", "Sonderpunkt", person="Sonja")
            order = [r["id"] for r in rows(client)]
            titles = [r["title"] for r in rows(client)]
            r = apply(client, template, [{"song_id": "neu.foil", "text": ""}],
                      [("begruessung1", "Anna"), ("gebet_predigt", "Gero")])
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual([r["id"] for r in rows(client)], order)
            self.assertEqual([r["title"] for r in rows(client)], titles)  # "Willkommen heute" was not renamed
            now = {r["title"]: r for r in rows(client)}
            self.assertEqual(now["Willkommen heute"]["person"], "Anna")  # mapped by the title's keyword
            self.assertEqual(now["Gebet"]["person"], "Gero")
            self.assertEqual((now["Technik-Check"]["person"], now["Technik-Check"]["cam1_preset"]), ("Tom", 2))
            self.assertEqual(now["Sonderpunkt"]["person"], "Sonja")
            self.assertEqual(now["Predigt"]["person"], "Alt")  # the order does not map it

    def test_no_positional_pairing(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            put(client, "10:00", "Eröffnung", person="Erika", cam1_preset=2)
            slot = put(client, "10:05", "Lied", song="neu.foil", cam1_preset=3)
            put(client, "10:30", "Schluss", person="Susi")
            template = client.post("/program/templates", json={"name": "GD", "slots": [slot["id"]]}).json()["template"]
            for item in client.app.state.program_items._items:  # same places, other names
                item["title"] = {"Lied": "Singen"}.get(item["title"], item["title"])
            before = rows(client)
            r = apply(client, template, [],  # (a song without a slot row would become a new row at the end)
                      [("begruessung1", "Anna"), ("predigt", "J. Prediger"), ("segen", "J. Prediger")])
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(rows(client), before)  # nothing matched by title: nothing touched

    def test_empty_person_clears_the_name_and_text_song_goes_into_the_title(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            put(client, "10:00", "Begrüßung", **{**SETTINGS, "person": "Letzte Woche"})
            put(client, "10:10", "Predigt", person="Bleibt")
            slot = put(client, "10:05", "Lied 1", **SETTINGS)
            template = client.post("/program/templates", json={"name": "GD", "slots": [slot["id"]]}).json()["template"]
            r = apply(client, template, [{"song_id": None, "text": "Unbekanntes Lied"}], [("begruessung1", "")])
            self.assertEqual(r.status_code, 200, r.text)
            titles = {r["title"]: r for r in rows(client)}
            self.assertEqual(titles["Begrüßung"]["person"], "")  # mapped by the order: last week's name is wrong
            self.assertEqual(titles["Predigt"]["person"], "Bleibt")  # not mapped: kept
            row = titles["Lied 1: Unbekanntes Lied"]
            self.assertEqual((row["song"], row["cam1_preset"], row["light"]), (None, 3, "licht1"))
            # next week: the title goes back to the slot's name
            r = apply(client, template, [{"song_id": "neu.foil", "text": ""}])
            titles = {r["title"]: r for r in rows(client)}
            self.assertEqual(titles["Lied 1"]["song"], "neu.foil")
            self.assertEqual(titles["Lied 1"]["cam2_preset"], 5)

    def test_without_songs_the_song_rows_stay(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            slot = put(client, "10:05", "Lied", song="neu.foil", **SETTINGS)
            template = client.post("/program/templates", json={"name": "GD", "slots": [slot["id"]]}).json()["template"]
            apply(client, template, [], [("begruessung1", "Anna")])
            self.assertEqual(by_title(client, "Lied")["song"], "neu.foil")

    def test_extra_songs_are_new_rows_fewer_songs_delete_nothing(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            put(client, "10:00", "Begrüßung")
            slot = put(client, "10:05", "Lied 1", **SETTINGS)
            put(client, "10:30", "Predigt", **SETTINGS)
            template = client.post("/program/templates", json={"name": "GD", "slots": [slot["id"]]}).json()["template"]
            before = {r["title"]: r["id"] for r in rows(client)}
            r = apply(client, template, [{"song_id": "neu.foil", "text": ""}, {"song_id": "zweit.foil", "text": ""}])
            self.assertEqual(r.status_code, 200, r.text)
            result = rows(client)
            self.assertEqual([x["title"] for x in result], ["Begrüßung", "Lied 1", "Lied 2", "Predigt"])
            self.assertEqual([x["song"] for x in result[1:3]], ["neu.foil", "zweit.foil"])
            self.assertTrue(all(x["cam1_preset"] == 3 for x in result[1:]))
            self.assertEqual((result[0]["id"], result[1]["id"], result[3]["id"]),
                             (before["Begrüßung"], before["Lied 1"], before["Predigt"]))
            apply(client, template, [{"song_id": "neu.foil", "text": ""}])
            result = rows(client)
            self.assertEqual([(x["title"], x["song"]) for x in result][1:3], [("Lied 1", "neu.foil"), ("Lied 2", None)])
            self.assertEqual(len(result), 4)  # the extra row is not deleted
            self.assertEqual(by_title(client, "Predigt")["light"], "licht1")

    def test_empty_program_comes_from_the_template(self):
        with app_client() as (client, _):
            put(client, "10:00", "Begrüßung", **SETTINGS)
            slot = put(client, "10:05", "Lied", **SETTINGS)
            template = client.post("/program/templates", json={"name": "GD", "slots": [slot["id"]]}).json()["template"]
            for item in rows(client):
                client.delete(f"/program/items/{item['id']}")
            self.assertEqual(apply(client, template, [], [("begruessung1", "Anna")]).status_code, 200)
            self.assertEqual(by_title(client, "Begrüßung")["cam1_preset"], 3)
            self.assertEqual(by_title(client, "Begrüßung")["person"], "Anna")


# The production template and program (read-only look, 2026-11-26): the template's
# slot titles are last time's songs, the program's song rows are "Lied 1", "Lied 2".
PROD_TEMPLATE = [
    ("Begrüßung", 0), ("Textlesung", 0), ("Above all", 1), ("Gebet im Knien", 0), ("Missionsbericht", 0),
    ("Mission spotlight", 0), ("Missionsgaben", 0), ("Dankgebet Missionsgaben", 0), ("Ach mein Herr Jesu", 1),
    ("Bibelgespräch", 0), ("All eure Sorgen", 1), ("Gebet", 0),
]
PROD_PROGRAM = [
    ("Begrüßung", {}), ("Textlesung", {}), ("Lied 1", {"cam1_preset": 3}), ("Gebet im Knien", {}),
    ("Missionsbericht", {"hidden": True}), ("Mission spotlight", {}), ("Missionsgaben", {}),
    ("Dankgebet Missionsgaben", {}), ("Lied 2", {"cam1_preset": 4}), ("Bibelgespräch", {}),
    ("All eure Sorgen", {"cam1_preset": 5}), ("Gebet", {"hidden": True}),
]


def production_setup(client):
    """The production rows (times 09:00, 09:05 ...) and template, as the live program is built."""
    client.app.state.songs.songs = dict(LIBRARY)
    slots = []
    for n, (title, extra) in enumerate(PROD_PROGRAM):
        extra = dict(extra)
        hidden = extra.pop("hidden", False)
        row = put(client, f"09:{n * 5:02d}", title, **extra)
        if hidden:
            asyncio.run(client.app.state.program_items.set_hidden(row["id"], True))
        if PROD_TEMPLATE[n][1]:
            slots.append(row["id"])
    # the template as it was saved: other slot titles (last time's songs) than the program rows have now
    template = client.post("/program/templates", json={"name": "Gottesdienst", "slots": slots}).json()["template"]
    store = client.app.state.program_templates
    for row, (title, _) in zip(next(t for t in store._templates if t["id"] == template["id"])["rows"], PROD_TEMPLATE):
        row["title"] = title
    return template


class SongSlotAnchorTests(unittest.TestCase):
    def test_production_lists(self):
        with app_client() as (client, _):
            template = production_setup(client)
            before = {r["title"]: r for r in rows(client)}
            r = apply(client, template, [{"song_id": "neu.foil", "text": ""}, {"song_id": "zweit.foil", "text": ""},
                                         {"song_id": None, "text": "Dritte Nummer"}],
                      [("begruessung1", "Anna"), ("gebet_knie", "Karl"), ("missionsbericht", "Mission Spotlight"),
                       ("missionsgaben", "Mia"), ("dankgebet", "Dora"), ("bibelgespraech", "Bea"),
                       ("gebet_ende1", "Gero")])
            self.assertEqual(r.status_code, 200, r.text)
            after = rows(client)
            self.assertEqual([x["id"] for x in after], [x["id"] for x in rows(client)])
            self.assertEqual(len(after), 12, "no new rows: every song found its row")
            found = {x["id"]: x for x in after}
            self.assertEqual(found[before["Lied 1"]["id"]]["song"], "neu.foil")
            self.assertEqual(found[before["Lied 2"]["id"]]["song"], "zweit.foil")
            third = found[before["All eure Sorgen"]["id"]]
            self.assertEqual((third["song"], third["title"]), (None, "Lied 3: Dritte Nummer"))
            self.assertEqual([found[before[t]["id"]]["title"] for t in ("Lied 1", "Lied 2", "All eure Sorgen")],
                             ["Lied 1", "Lied 2", "Lied 3: Dritte Nummer"])  # named by place, not by last time's song
            self.assertEqual([found[before[t]["id"]]["cam1_preset"] for t in ("Lied 1", "Lied 2", "All eure Sorgen")], [3, 4, 5])
            self.assertEqual(found[before["Begrüßung"]["id"]]["person"], "Anna")
            self.assertEqual([found[before[t]["id"]]["person"] for t in (
                "Gebet im Knien", "Missionsgaben", "Dankgebet Missionsgaben", "Bibelgespräch", "Gebet")],
                ["Karl", "Mia", "Dora", "Bea", "Gero"])
            self.assertEqual(found[before["Mission spotlight"]["id"]]["hidden"], False)
            self.assertEqual(found[before["Missionsbericht"]["id"]]["hidden"], True)
            self.assertEqual(found[before["Gebet"]["id"]]["hidden"], True)  # a hidden row stays hidden

    def test_preview_counts_the_programs_song_rows(self):
        with app_client() as (client, _):
            template = production_setup(client)
            r = client.post(f"/program/templates/{template['id']}/apply", json={
                "dry_run": True, "songs": [{"song_id": "neu.foil", "text": ""}], "persons": []})
            self.assertEqual(r.json()["slots"], 3)
            self.assertEqual(len(r.json()["rows"]), 12)

    def test_extra_song_row_is_hidden_while_empty_and_shown_with_a_song(self):
        with app_client() as (client, _):
            template = production_setup(client)
            four = [{"song_id": "neu.foil", "text": ""}] * 3 + [{"song_id": "zweit.foil", "text": ""}]
            apply(client, template, four)
            after = rows(client)
            self.assertEqual([x["title"] for x in after if x["title"].startswith("Lied")], ["Lied 1", "Lied 2", "Lied 3", "Lied 4"])
            extra = next(x for x in after if x["title"] == "Lied 4")  # the 4th song: a row after the last slot
            self.assertEqual((extra["song"], extra["hidden"]), ("zweit.foil", False))
            apply(client, template, four[:3])  # next time only three songs: the extra row stays, hidden
            after = rows(client)
            self.assertEqual(len(after), 13)
            extra = next(x for x in after if x["id"] == extra["id"])
            self.assertEqual((extra["song"], extra["hidden"]), (None, True))
            self.assertEqual(client.get("/program/status").json()["next_id"], rows(client)[0]["id"])  # "Weiter" skips it
            apply(client, template, four)
            extra = next(x for x in rows(client) if x["id"] == extra["id"])
            self.assertEqual((extra["song"], extra["hidden"]), ("zweit.foil", False))
            # the template's own empty slots are not hidden
            apply(client, template, four[:1])
            now = {x["title"]: x for x in rows(client)}
            self.assertEqual((now["Lied 2"]["song"], now["Lied 2"]["hidden"]), (None, False))

    def test_text_song_keeps_its_number_and_text(self):
        with app_client() as (client, _):
            template = production_setup(client)
            songs = [{"song_id": "neu.foil", "text": ""}] * 3 + [{"song_id": None, "text": "300"}]
            apply(client, template, songs)
            now = {x["title"]: x for x in rows(client)}
            self.assertEqual(now["Lied 4: 300"]["song"], None)
            self.assertEqual(now["Lied 4: 300"]["hidden"], False)
            self.assertEqual(now["Lied 3"]["song"], "neu.foil")

    def test_hint_does_not_count_a_hidden_extra_as_an_empty_slot(self):
        with app_client() as (client, _):
            template = production_setup(client)
            four = [{"song_id": "neu.foil", "text": ""}] * 4
            apply(client, template, four)
            apply(client, template, four[:3])  # the 4th row stays, hidden
            preview = lambda songs: client.post(f"/program/templates/{template['id']}/apply", json={
                "dry_run": True, "songs": songs, "persons": []}).json()
            self.assertEqual(preview(four[:3])["slots"], 3)  # three that can take a song, not four
            self.assertEqual(preview(four)["slots"], 4)  # with a fourth song the extra row is used
            self.assertEqual(preview([])["slots"], 3)

    def test_only_song_like_rows_get_a_song(self):
        with app_client() as (client, _):
            template = production_setup(client)
            # a row between the same anchors as the third slot: no song, no song title
            put(client, "09:51", "Kinderansprache", cam1_preset=8)
            apply(client, template, [{"song_id": "neu.foil", "text": ""}] * 3)
            now = {x["title"]: x for x in rows(client)}
            self.assertEqual((now["Kinderansprache"]["song"], now["Kinderansprache"]["cam1_preset"]), (None, 8))
            self.assertEqual(now["Lied 3"]["song"], "neu.foil")
        with app_client() as (client, _):  # alone between the anchors, with a slot there: still no song
            client.app.state.songs.songs = dict(LIBRARY)
            put(client, "10:00", "Begrüßung")
            put(client, "10:05", "Kinderansprache")
            put(client, "10:30", "Predigt")
            template = client.post("/program/templates", json={"name": "GD", "slots": []}).json()["template"]
            client.app.state.program_templates._templates[0]["rows"].insert(1, {
                **client.app.state.program_templates._templates[0]["rows"][0], "title": "Lied 1", "song_slot": True})
            apply(client, template, [{"song_id": "neu.foil", "text": ""}])
            self.assertEqual(by_title(client, "Kinderansprache")["song"], None)
            self.assertEqual(len(rows(client)), 4)  # the song became a new row

    def test_eingangs_and_schlusslied_are_song_rows_and_keep_their_names(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            put(client, "10:00", "Begrüßung")
            first = put(client, "10:05", "Eingangslied", cam1_preset=3)
            put(client, "10:30", "Predigt")
            last = put(client, "10:55", "Schlusslied", cam1_preset=4)
            put(client, "11:00", "Segen")
            template = client.post("/program/templates", json={"name": "GD", "slots": [first["id"], last["id"]]}).json()["template"]
            apply(client, template, [{"song_id": "neu.foil", "text": ""}, {"song_id": "zweit.foil", "text": ""}])
            now = {x["title"]: x for x in rows(client)}
            self.assertEqual((now["Eingangslied"]["song"], now["Schlusslied"]["song"]), ("neu.foil", "zweit.foil"))
            self.assertEqual((now["Eingangslied"]["cam1_preset"], now["Schlusslied"]["cam1_preset"]), (3, 4))
            self.assertEqual(len(now), 5)

    def test_template_stores_slots_as_lied_n(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            put(client, "10:00", "Begrüßung")
            a = put(client, "10:05", "Above all", song="above.foil")
            put(client, "10:10", "Gebet")
            b = put(client, "10:15", "Ach mein Herr Jesu", song="ach.foil")
            c = put(client, "10:20", "Eingangslied", song="neu.foil")
            d = put(client, "10:25", "Lobpreis")  # no song, a name the user gave
            t = client.post("/program/templates", json={"name": "GD", "slots": [a["id"], b["id"], c["id"], d["id"]]}).json()["template"]
            self.assertEqual([r["title"] for r in t["rows"]], [
                "Begrüßung", "Lied 1", "Gebet", "Lied 2", "Eingangslied", "Lobpreis"])
            self.assertTrue(all(r["song"] is None for r in t["rows"] if r["song_slot"]))

    def test_empty_program_uses_the_same_names(self):
        with app_client() as (client, _):
            template = production_setup(client)
            for item in rows(client):
                client.delete(f"/program/items/{item['id']}")
            songs = [{"song_id": "neu.foil", "text": ""}, {"song_id": "zweit.foil", "text": ""},
                     {"song_id": "neu.foil", "text": ""}, {"song_id": None, "text": "300"}]
            self.assertEqual(apply(client, template, songs).status_code, 200)
            now = [(x["title"], x["song"]) for x in rows(client) if "Lied" in x["title"] or "Sorgen" in x["title"]
                   or x["song"]]
            self.assertEqual(now, [("Lied 1", "neu.foil"), ("Lied 2", "zweit.foil"), ("Lied 3", "neu.foil"),
                                   ("Lied 4: 300", None)])
            titles = [x["title"] for x in rows(client)]
            self.assertNotIn("Above all", titles)
            self.assertNotIn("Ach mein Herr Jesu", titles)
            self.assertNotIn("All eure Sorgen", titles)
            self.assertNotIn("All eure Sorgen: 300", titles)

    def test_empty_program_without_songs_still_names_the_slots_by_place(self):
        with app_client() as (client, _):
            template = production_setup(client)
            for item in rows(client):
                client.delete(f"/program/items/{item['id']}")
            apply(client, template, [])
            self.assertEqual([x["title"] for x in rows(client) if x["title"].startswith("Lied")], ["Lied 1", "Lied 2", "Lied 3"])

    def test_empty_program_keeps_custom_slot_names(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            slot = put(client, "10:05", "Lobpreis")
            template = client.post("/program/templates", json={"name": "GD", "slots": [slot["id"]]}).json()["template"]
            client.delete(f"/program/items/{slot['id']}")
            apply(client, template, [{"song_id": "neu.foil", "text": ""}, {"song_id": "zweit.foil", "text": ""}])
            self.assertEqual([(x["title"], x["song"]) for x in rows(client)],
                             [("Lobpreis", "neu.foil"), ("Lied 2", "zweit.foil")])

    def test_one_rule_for_saving_and_applying(self):
        keys = _song_keys(LIBRARY)
        for title, song, expected in [
            ("Lied", None, True), ("Lied 3", None, True), ("lied  2: 300", None, True),  # already by place
            ("Above all", None, True), ("ABOVE ALL: x", None, True),  # the title of a library song
            ("Eigenes Lied", "Eigenes Lied.foil", True),  # the file name of its song (library not loaded)
            ("Eigenes Lied", "andere.foil", False),
            ("Eingangslied", None, False), ("Schlusslied", "neu.foil", False), ("Lobpreis", None, False),  # the user's
            ("Kinderlied", None, False),
        ]:
            self.assertEqual(is_place_named(title, song, keys), expected, (title, song))

    def test_saving_and_applying_agree_for_a_row_titled_by_its_song_file(self):
        with app_client() as (client, _):  # no library loaded: only the file name tells
            put(client, "10:00", "Begrüßung")
            row = put(client, "10:05", "Above all", song="Above all.foil")
            put(client, "10:30", "Predigt")
            template = client.post("/program/templates", json={"name": "GD", "slots": [row["id"]]}).json()["template"]
            self.assertEqual([r["title"] for r in template["rows"]], ["Begrüßung", "Lied 1", "Predigt"])
            client.app.state.songs.songs = {}
            put_back = apply(client, template, [{"song_id": None, "text": "5"}])
            self.assertEqual(put_back.status_code, 200)
            self.assertEqual([x["title"] for x in rows(client)], ["Begrüßung", "Lied 1: 5", "Predigt"])

    def test_anchors_missing_fall_back_to_the_titles(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            put(client, "10:00", "Lied 1", cam1_preset=3)
            put(client, "10:10", "Technik-Check")
            put(client, "10:20", "Lied 2", cam1_preset=4)
            template = client.post("/program/templates", json={"name": "GD", "slots": [
                r["id"] for r in rows(client) if r["title"].startswith("Lied")]}).json()["template"]
            apply(client, template, [{"song_id": "neu.foil", "text": ""}, {"song_id": "zweit.foil", "text": ""}])
            now = rows(client)
            self.assertEqual([(x["title"], x["song"]) for x in now],
                             [("Lied 1", "neu.foil"), ("Technik-Check", None), ("Lied 2", "zweit.foil")])

    def test_a_row_between_the_anchors_that_is_no_song_is_left_alone(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            put(client, "10:00", "Begrüßung")
            put(client, "10:02", "Technik-Check", cam1_preset=2)
            put(client, "10:05", "Lied 1", cam1_preset=3)
            put(client, "10:30", "Predigt")
            template = client.post("/program/templates", json={"name": "GD", "slots": [
                by_title(client, "Lied 1")["id"]]}).json()["template"]
            apply(client, template, [{"song_id": "neu.foil", "text": ""}])
            now = {x["title"]: x for x in rows(client)}
            self.assertEqual((now["Technik-Check"]["song"], now["Lied 1"]["song"]), (None, "neu.foil"))
            self.assertEqual(len(now), 4)


class MissionSpotlightTests(unittest.TestCase):
    def setup_program(self, client, normal=True, spotlight="Mission Spotlight"):
        put(client, "10:00", "Begrüßung")
        if spotlight:  # before the Missionsbericht: it must not get the PDF's person
            put(client, "10:10", spotlight, desktop=["spotlight"], person="Nie")
        if normal:
            put(client, "10:11", "Missionsbericht", cam1_preset=4)
        put(client, "10:30", "Predigt")
        template = client.post("/program/templates", json={"name": "GD", "slots": []}).json()["template"]
        if spotlight:  # hidden is a state of the program, not of the template
            asyncio.run(client.app.state.program_items.set_hidden(by_title(client, spotlight)["id"], True))
        return template

    def state(self, client, spotlight="Mission Spotlight"):
        found = {r["title"]: r for r in rows(client)}
        return {t: (r["hidden"], r["person"]) for t, r in found.items() if t in (spotlight, "Missionsbericht")}

    def test_switching_back_and_forth(self):
        with app_client() as (client, _):
            template = self.setup_program(client)
            ids = {r["title"]: r["id"] for r in rows(client)}
            r = apply(client, template, persons=[("missionsbericht", "  mission   SPOTLIGHT ")])
            self.assertEqual((r.status_code, r.json()["mission"]), (200, "spotlight"), r.text)
            self.assertEqual(self.state(client), {"Mission Spotlight": (False, "Nie"), "Missionsbericht": (True, "")})
            r = apply(client, template, persons=[("missionsbericht", "Dora Mission")])
            self.assertEqual(r.json()["mission"], "normal")
            self.assertEqual(self.state(client), {"Mission Spotlight": (True, "Nie"), "Missionsbericht": (False, "Dora Mission")})
            self.assertEqual(by_title(client, "Missionsbericht")["cam1_preset"], 4)
            apply(client, template, persons=[("missionsbericht", "Mission Spotlight")])
            self.assertEqual(self.state(client), {"Mission Spotlight": (False, "Nie"), "Missionsbericht": (True, "")})
            apply(client, template, persons=[("missionsbericht", "Jan Berichter")])
            self.assertEqual(self.state(client), {"Mission Spotlight": (True, "Nie"), "Missionsbericht": (False, "Jan Berichter")})
            self.assertEqual({r["title"]: r["id"] for r in rows(client)}, ids)  # same rows all along

    def test_name_and_title_variants(self):
        for person in ("Missions Spotlight", "Missions-Spotlight", "Mission-Spotlight", "MISSION  spotlight", "mission spotlight"):
            with app_client() as (client, _):
                template = self.setup_program(client)
                r = apply(client, template, persons=[("missionsbericht", person)])
                self.assertEqual(r.json()["mission"], "spotlight", person)
        for title in ("Mission spotlight", "Missions-Spotlight", "MISSION SPOTLIGHT", "Mission-Spotlight"):
            with app_client() as (client, _):  # the production row is "Mission spotlight"
                template = self.setup_program(client, spotlight=title)
                apply(client, template, persons=[("missionsbericht", "Mission Spotlight")])
                self.assertEqual(self.state(client, title), {title: (False, "Nie"), "Missionsbericht": (True, "")}, title)
                apply(client, template, persons=[("missionsbericht", "Dora")])
                self.assertEqual(self.state(client, title), {title: (True, "Nie"), "Missionsbericht": (False, "Dora")}, title)
        with app_client() as (client, _):  # a person that only contains the words is a name
            template = self.setup_program(client)
            r = apply(client, template, persons=[("missionsbericht", "Mission Spotlight Team")])
            self.assertEqual(r.json()["mission"], "normal")

    def test_no_person_changes_nothing(self):
        with app_client() as (client, _):
            template = self.setup_program(client)
            apply(client, template, persons=[("missionsbericht", "Mission Spotlight")])
            before = self.state(client)
            r = apply(client, template, persons=[("begruessung1", "Anna")])  # the order has no Missionsbericht point
            self.assertIsNone(r.json()["mission"])
            self.assertEqual(self.state(client), before)
            r = apply(client, template)
            self.assertIsNone(r.json()["mission"])
            self.assertEqual(self.state(client), before)

    def test_preview_and_single_row(self):
        with app_client() as (client, _):
            template = self.setup_program(client)
            r = client.post(f"/program/templates/{template['id']}/apply", json={
                "dry_run": True, "songs": [], "persons": [{"id": "missionsbericht", "person": "Mission Spotlight"}]})
            self.assertEqual(r.json()["mission"], "spotlight")
            self.assertEqual({x["title"]: x["hidden"] for x in r.json()["rows"]}["Missionsbericht"], True)
            self.assertEqual(self.state(client)["Missionsbericht"][0], False)  # nothing was applied
        with app_client() as (client, _):
            template = self.setup_program(client, spotlight=None)  # no Spotlight row: just the name
            apply(client, template, persons=[("missionsbericht", "Dora Mission")])
            self.assertEqual(self.state(client), {"Missionsbericht": (False, "Dora Mission")})
            apply(client, template, persons=[("missionsbericht", "Mission Spotlight")])
            self.assertEqual(self.state(client), {"Missionsbericht": (True, "")})
        with app_client() as (client, _):
            template = self.setup_program(client, normal=False)  # only the Spotlight row
            apply(client, template, persons=[("missionsbericht", "Dora Mission")])
            self.assertEqual(self.state(client), {"Mission Spotlight": (True, "Nie")})

    def test_spotlight_row_found_by_its_desktop_profile(self):
        profiles = {"spot": SimpleNamespace(mission_spotlight=True), "bild": SimpleNamespace(mission_spotlight=False)}
        rows_ = [{"title": "Missionsbericht", "desktop": [], "_point": "missionsbericht"},
                 {"title": "Video", "desktop": ["bild", "spot"]}, {"title": "Bild", "desktop": ["bild"]}]
        self.assertEqual(switch_spotlight(rows_, "Mission Spotlight", profiles), "spotlight")
        self.assertEqual([r.get("hidden") for r in rows_], [True, False, None])
        self.assertEqual(switch_spotlight(rows_, "Dora", profiles), "normal")
        self.assertEqual([r.get("hidden") for r in rows_], [False, True, None])


if __name__ == "__main__":
    unittest.main()
