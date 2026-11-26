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

from app.program_templates import switch_spotlight
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
            slot = put(client, "10:05", "Lied", song="neu.foil", **SETTINGS)
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
            self.assertEqual(after["Lied"]["song"], "zweit.foil")
            for title, row in before.items():
                for field in ("id", "time", "group", "light", "cam1_preset", "cam2_preset", "camera_wait_seconds",
                              "cam1_scene", "cam2_scene", "desktop", "beamer", "hidden", "last_played_at"):
                    self.assertEqual(after[title][field], row[field], (title, field))
            self.assertEqual(after["Lied"]["cam1_preset"], 9)
            self.assertEqual(after["Lied"]["desktop"], ["bild3"])
            self.assertTrue(after["Predigt"]["hidden"])
            self.assertIsNotNone(after["Begrüßung"]["last_played_at"])
            self.assertEqual(client.get("/program/status").json()["current_id"], before["Begrüßung"]["id"])

    def test_rows_only_in_the_program_stay_and_nothing_is_reordered_or_renamed(self):
        with app_client() as (client, _):
            client.app.state.songs.songs = dict(LIBRARY)
            put(client, "10:00", "Begrüßung")
            put(client, "10:02", "Technik-Check", person="Tom", cam1_preset=2)  # not in the template
            slot = put(client, "10:05", "Lied", **SETTINGS)
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
            slot = put(client, "10:05", "Lied", **SETTINGS)
            template = client.post("/program/templates", json={"name": "GD", "slots": [slot["id"]]}).json()["template"]
            r = apply(client, template, [{"song_id": None, "text": "Unbekanntes Lied"}], [("begruessung1", "")])
            self.assertEqual(r.status_code, 200, r.text)
            titles = {r["title"]: r for r in rows(client)}
            self.assertEqual(titles["Begrüßung"]["person"], "")  # mapped by the order: last week's name is wrong
            self.assertEqual(titles["Predigt"]["person"], "Bleibt")  # not mapped: kept
            row = titles["Lied: Unbekanntes Lied"]
            self.assertEqual((row["song"], row["cam1_preset"], row["light"]), (None, 3, "licht1"))
            # next week: the title goes back to the slot's name
            r = apply(client, template, [{"song_id": "neu.foil", "text": ""}])
            titles = {r["title"]: r for r in rows(client)}
            self.assertEqual(titles["Lied"]["song"], "neu.foil")
            self.assertEqual(titles["Lied"]["cam2_preset"], 5)

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
            slot = put(client, "10:05", "Lied", **SETTINGS)
            put(client, "10:30", "Predigt", **SETTINGS)
            template = client.post("/program/templates", json={"name": "GD", "slots": [slot["id"]]}).json()["template"]
            before = {r["title"]: r["id"] for r in rows(client)}
            r = apply(client, template, [{"song_id": "neu.foil", "text": ""}, {"song_id": "zweit.foil", "text": ""}])
            self.assertEqual(r.status_code, 200, r.text)
            result = rows(client)
            self.assertEqual([x["title"] for x in result], ["Begrüßung", "Lied", "Lied", "Predigt"])
            self.assertEqual([x["song"] for x in result[1:3]], ["neu.foil", "zweit.foil"])
            self.assertTrue(all(x["cam1_preset"] == 3 for x in result[1:]))
            self.assertEqual((result[0]["id"], result[1]["id"], result[3]["id"]),
                             (before["Begrüßung"], before["Lied"], before["Predigt"]))
            apply(client, template, [{"song_id": "neu.foil", "text": ""}])
            result = rows(client)
            self.assertEqual([(x["title"], x["song"]) for x in result][1:3], [("Lied", "neu.foil"), ("Lied", None)])
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
