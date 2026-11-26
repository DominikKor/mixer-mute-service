"""A program row's shortcut is its whole configuration (app/actions.py):
the row runs the shortcut's fields instead of its own, follows later edits
of the shortcut and falls back to its own fields when the shortcut is
deleted. Rows saved with the old list of shortcuts are migrated once."""

import json
import logging
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from app.actions import with_shortcut
from app.program_items import ProgramItemError, ProgramItemStore, has_own_actions
from app.program_templates import ProgramTemplateStore

from tests.helpers import app_client

PROFILES = ("desktop_display:\n  enabled: true\n  profiles:\n"
            "    qr:\n      title: QR\n      kind: image\n      path: 'C:\\Bilder\\qr.png'\n"
            "    logo:\n      title: Logo\n      kind: image\n      path: 'C:\\Bilder\\logo.png'\n"
            "    bild:\n      title: Bild\n      kind: image\n      path: 'C:\\Bilder\\bild.png'\n"
            "groups:\n  Predigt:\n    channels: [1]\n")
A, B, C = "a" * 32, "b" * 32, "c" * 32


def row(**extra):
    return {"time": "10:00", "title": "Predigt", "person": "Anna", "group": "", **extra}


class Recorder:
    """Fake desktop profiles and beamer: records what a row ran."""

    def __init__(self):
        self.ran = []

    async def run_profile(self, _request, profile_id):
        self.ran.append(profile_id)
        return {"ok": True}, 200

    async def press_native(self, _request, button_id, _action):
        self.ran.append(button_id)
        return {"ok": True, "button": button_id}, 200

    def patched(self):
        stack = ExitStack()
        stack.enter_context(patch("app.api.program.run_profile", self.run_profile))
        stack.enter_context(patch("app.api.program.press_native", self.press_native))
        return stack


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_shortcut_per_row(self):
        with tempfile.TemporaryDirectory() as name:
            store = ProgramItemStore(Path(name) / "program.json")
            created = await store.create(row(shortcut=A, desktop=["qr"]))
            self.assertEqual((created["shortcut"], created["desktop"]), (A, ["qr"]),
                             "the row's own fields stay saved next to its shortcut")
            self.assertNotIn("shortcuts", created)
            self.assertIsNone((await store.create(row(shortcut="")))["shortcut"])
            # The old list: its first entry becomes the shortcut.
            self.assertEqual((await store.create(row(shortcuts=[B, A])))["shortcut"], B)
            self.assertEqual((await store.create(row(shortcut=C, shortcuts=[B])))["shortcut"], C)
            for bad in ({"shortcut": "x"}, {"shortcut": 3}, {"shortcuts": ["x"]}, {"shortcuts": A}):
                with self.assertRaises(ProgramItemError, msg=bad):
                    await store.create(row(**bad))
            self.assertEqual(await store.drop_shortcut(A), 1)
            again = await store.get(created["id"])
            self.assertEqual((again["shortcut"], again["desktop"]), (None, ["qr"]),
                             "a deleted shortcut leaves the row with its own fields")
            self.assertEqual(await store.drop_shortcut(A), 0)

    async def test_migration_of_the_old_list(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "program.json"
            path.write_text(json.dumps([
                {"id": "plain", "time": "09:00", "title": "Ohne", "shortcuts": []},
                {"id": "one", "time": "09:10", "title": "Einer", "shortcuts": [A]},
                {"id": "two", "time": "09:20", "title": "Zwei", "shortcuts": [A, B]},
                {"id": "own", "time": "09:30", "title": "Eigene", "desktop": ["qr"], "shortcuts": [B]},
                {"id": "new", "time": "09:40", "title": "Neu", "shortcut": C},
            ]), encoding="utf-8")
            with self.assertLogs("mixer.program", logging.INFO) as logs:
                store = ProgramItemStore(path, {A: "Predigt", B: "Kollekte"})
            rows = {item["id"]: item for item in await store.list()}
            self.assertEqual({key: item["shortcut"] for key, item in rows.items()},
                             {"plain": None, "one": A, "two": A, "own": B, "new": C})
            self.assertEqual(rows["own"]["desktop"], ["qr"], "own fields are kept")
            self.assertEqual(store.migration, {"rows": 4, "several_shortcuts": ["two"],
                                               "own_steps_and_shortcut": ["own"]})
            text = "\n".join(logs.output)
            self.assertIn("1 had several shortcuts", text)
            self.assertIn("1 had own steps and a shortcut", text)
            self.assertIn(f"„Kollekte“ ({B})", text, "the dropped shortcut is named")
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(all("shortcuts" not in entry for entry in saved))
            self.assertEqual([entry["shortcut"] for entry in saved], [None, A, A, B, C])
            # Idempotent: the second load finds nothing to migrate.
            before = path.read_text(encoding="utf-8")
            again = ProgramItemStore(path)
            self.assertIsNone(again.migration)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    def test_own_actions(self):
        # The song is the row's own whatever its shortcut, so it does not count.
        self.assertFalse(has_own_actions({"group": "", "desktop": [], "camera_wait_seconds": 5.0,
                                          "playback_description": "x", "song": "a.foil"}))
        for key, value in (("group", "Pult"), ("desktop", ["qr"]), ("cam2_scene", "X"), ("beamer", "hide"),
                           ("lyrics", "file:1.2.3.4|C:\\a.txt")):
            self.assertTrue(has_own_actions({key: value}), key)

    async def test_a_song_or_a_lyrics_file_not_both(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "program.json"
            store = ProgramItemStore(path)
            lyrics = "file:192.168.188.11|C:\\Texte\\lied.txt"
            both = await store.create(row(song="lied-12.foil", lyrics=lyrics, shortcut=A))
            self.assertEqual((both["song"], both["lyrics"]), ("lied-12.foil", None),
                             "a hidden own Liedtext file is not saved next to a song")
            only_file = await store.create(row(lyrics=lyrics))
            self.assertEqual(only_file["lyrics"], lyrics)
            # A row saved before with both: the song stays, and the log says so.
            path.write_text(json.dumps([{**row(song="a.foil", lyrics=lyrics), "id": "old", "playback_description": "",
                                         "translation_description": "", "lyrics_description": ""}]), encoding="utf-8")
            with self.assertLogs("mixer.program", logging.WARNING):
                old = await ProgramItemStore(path).get("old")
            self.assertEqual((old["song"], old["lyrics"]), ("a.foil", None))

    def test_the_rows_song_wins_over_the_shortcuts_lyrics_file(self):
        lyrics = "file:192.168.188.11|C:\\Texte\\lied.txt"
        shortcut = {"title": "Lied", "song": None, "lyrics": lyrics, "group": "Band"}
        ran = with_shortcut({"title": "Lied 12", "song": "lied-12.foil", "lyrics": None}, shortcut)
        self.assertEqual((ran["song"], ran["lyrics"], ran["group"]), ("lied-12.foil", None, "Band"))
        ran = with_shortcut({"title": "Ansage", "song": None, "lyrics": None}, shortcut)
        self.assertEqual((ran["song"], ran["lyrics"]), (None, lyrics), "without a song the shortcut's file runs")

    def test_the_song_stays_the_rows(self):
        item = {"title": "Lied", "song": "lied-12.foil", "group": "", "desktop": ["qr"]}
        shortcut = {"title": "Band", "song": "anderes.foil", "group": "Band", "desktop": []}
        ran = with_shortcut(item, shortcut)
        self.assertEqual((ran["song"], ran["group"], ran["desktop"], ran["title"]),
                         ("lied-12.foil", "Band", [], "Lied"))
        self.assertIsNone(with_shortcut({**item, "song": None}, shortcut)["song"],
                          "a shortcut's song never runs for a row")

    async def test_deleted_shortcut_is_noted_on_the_row(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "program.json"
            store = ProgramItemStore(path)
            item = await store.create(row(shortcut=A))
            self.assertIsNone(item["shortcut_deleted"])
            await store.drop_shortcut(A, "Predigt")
            noted = await ProgramItemStore(path).get(item["id"])
            self.assertEqual((noted["shortcut"], noted["shortcut_deleted"]), (None, "Predigt"),
                             "kept across a restart, so every device shows it")
            copy = await store.duplicate(item["id"])
            self.assertEqual(copy["shortcut_deleted"], "Predigt")
            saved = await store.update(item["id"], row())
            self.assertIsNone(saved["shortcut_deleted"], "saving the row from its editor clears the note")

    async def test_template_migration_runs_once(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "templates.json"
            texts = {"person": "", "playback_description": "", "translation_description": "", "lyrics_description": ""}
            path.write_text(json.dumps([{"id": "t", "name": "Gottesdienst", "rows": [
                {**texts, "time": "09:00", "title": "Begrüßung", "shortcuts": [A, B]},
                {**texts, "time": "09:10", "title": "Lied", "desktop": ["qr"], "shortcuts": [B], "song_slot": True},
                {**texts, "time": "09:20", "title": "Neu", "shortcut": C},
            ]}]), encoding="utf-8")
            with self.assertLogs("mixer.program", logging.INFO) as logs:
                store = ProgramTemplateStore(path, {A: "Predigt", B: "Kollekte"})
            template = await store.get("t")
            self.assertEqual([r["shortcut"] for r in template["rows"]], [A, B, C])
            self.assertEqual(store.migration, {"rows": 2, "several_shortcuts": ["Gottesdienst"],
                                               "own_steps_and_shortcut": ["Gottesdienst"]})
            text = "\n".join(logs.output)
            self.assertIn("„Predigt“", text)
            self.assertIn("„Kollekte“", text)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(all("shortcuts" not in r for r in saved[0]["rows"]))
            before = path.read_text(encoding="utf-8")
            self.assertIsNone(ProgramTemplateStore(path).migration)
            self.assertEqual(path.read_text(encoding="utf-8"), before)


class ApiTests(unittest.TestCase):
    def test_row_runs_its_shortcut_instead_of_its_own_fields(self):
        with app_client(PROFILES) as (client, _folder):
            shortcut = client.post("/shortcuts", json={"title": "Logo", "desktop": ["logo"], "beamer": "unhide"}).json()["shortcut"]
            item = client.post("/program/items", json=row(desktop=["qr"], beamer="hide")).json()["item"]
            item = client.put(f"/program/items/{item['id']}", json=row(desktop=["qr"], beamer="hide", shortcut=shortcut["id"])).json()["item"]
            self.assertEqual((item["shortcut"], item["desktop"], item["beamer"]), (shortcut["id"], ["qr"], "hide"),
                             "a shortcut added later keeps the row's own fields")
            recorder = Recorder()
            with recorder.patched():
                played = client.post(f"/program/items/{item['id']}/play").json()
            self.assertTrue(played["ok"], played)
            self.assertEqual(recorder.ran, ["logo", "beamer-unhide"], "only the shortcut's fields run")
            self.assertEqual(played["shortcut"], {"id": shortcut["id"], "title": "Logo"})
            self.assertEqual(played["title"], "Predigt", "the row keeps its title")
            self.assertEqual(client.get("/program/status").json()["current_id"], item["id"])

            # Linked: an edited shortcut changes what the row does.
            client.put(f"/shortcuts/{shortcut['id']}", json={"title": "Bild", "desktop": ["bild", "qr"]})
            recorder.ran.clear()
            with recorder.patched():
                played = client.post(f"/program/items/{item['id']}/play").json()
            self.assertTrue(played["ok"], played)
            self.assertEqual(recorder.ran, ["bild", "qr"])

            # Deleted: the row runs its own fields again.
            deleted = client.delete(f"/shortcuts/{shortcut['id']}").json()
            self.assertEqual(deleted["program_items"], 1)
            fallen = {i["id"]: i for i in client.get("/program/items").json()}[item["id"]]
            self.assertEqual((fallen["shortcut"], fallen["shortcut_deleted"]), (None, "Bild"),
                             "every device sees that the row fell back")
            recorder.ran.clear()
            with recorder.patched():
                played = client.post(f"/program/items/{item['id']}/play").json()
            self.assertTrue(played["ok"], played)
            self.assertEqual(recorder.ran, ["beamer-hide", "qr"])
            self.assertNotIn("shortcut", played)

    def test_play_runs_the_song_not_also_the_shortcuts_lyrics_file(self):
        with app_client(PROFILES) as (client, _folder):
            lyrics = "file:127.0.0.1|C:\\Texte\\lied.txt"
            shortcut = client.post("/shortcuts", json={"title": "Lied", "lyrics": lyrics}).json()["shortcut"]
            with_song = client.post("/program/items", json=row(song="lied-12.foil", shortcut=shortcut["id"])).json()["item"]
            without = client.post("/program/items", json=row(title="Ansage", shortcut=shortcut["id"])).json()["item"]
            shown, files = [], []

            async def fake_show_slide(_request, song, slide):
                shown.append(song)
                return {"ok": True}

            async def fake_run_action(_request, value):
                files.append(value)
                return {"ok": True}, 200

            with patch("app.api.program.show_slide", fake_show_slide), \
                    patch("app.api.program.run_action", fake_run_action):
                first = client.post(f"/program/items/{with_song['id']}/play").json()
                second = client.post(f"/program/items/{without['id']}/play").json()
            self.assertTrue(first["ok"] and second["ok"], (first, second))
            self.assertEqual((shown, files), (["lied-12.foil"], [lyrics]),
                             "the song alone for the row with a song, the file for the one without")

    def test_the_song_of_the_row_is_shown_not_the_shortcuts(self):
        with app_client(PROFILES) as (client, _folder):
            shortcut = client.post("/shortcuts", json={"title": "Lied", "song": "anderes.foil", "beamer": "unhide"}).json()["shortcut"]
            item = client.post("/program/items", json=row(song="lied-12.foil", shortcut=shortcut["id"])).json()["item"]
            self.assertEqual(item["song"], "lied-12.foil", "the song is kept when the row is created with a shortcut")
            shown = []

            async def fake_show_slide(_request, song, slide):
                shown.append((song, slide))
                return {"ok": True}

            recorder = Recorder()
            with recorder.patched(), patch("app.api.program.show_slide", fake_show_slide):
                played = client.post(f"/program/items/{item['id']}/play").json()
            self.assertTrue(played["ok"], played)
            self.assertEqual((shown, recorder.ran), ([("lied-12.foil", 0)], ["beamer-unhide"]))

    def test_new_row_with_a_shortcut_saves_no_prefilled_fields(self):
        with app_client(PROFILES) as (client, _folder):
            shortcut = client.post("/shortcuts", json={"title": "QR", "desktop": ["qr"]}).json()["shortcut"]
            # The editor pre-fills the last row's group, then the shortcut hides it.
            item = client.post("/program/items", json=row(group="Predigt", desktop=["logo"], song="a.foil",
                                                          shortcut=shortcut["id"])).json()["item"]
            self.assertEqual((item["group"], item["desktop"], item["song"]), ("", [], "a.foil"))
            # Without a shortcut a new row keeps what it was given.
            plain = client.post("/program/items", json=row(group="Predigt")).json()["item"]
            self.assertEqual(plain["group"], "Predigt")

    def test_mute_conflict_is_checked_before_play(self):
        with app_client(PROFILES) as (client, _folder):
            overall = "native-overall-gebet"  # an Overall key: it switches the mutes itself
            shortcut = client.post("/shortcuts", json={"title": "QR", "desktop": ["qr"]}).json()["shortcut"]
            refused = client.post("/shortcuts", json={"title": "X", "group": "Predigt", "playback": overall})
            self.assertEqual(refused.status_code, 400, refused.text)
            item = client.post("/program/items", json=row(shortcut=shortcut["id"])).json()["item"]
            # The row's own fields are not checked while its shortcut runs...
            saved = client.put(f"/program/items/{item['id']}",
                               json=row(group="Predigt", playback=overall, shortcut=shortcut["id"]))
            self.assertEqual(saved.status_code, 200, saved.text)
            client.delete(f"/shortcuts/{shortcut['id']}")
            # ...so once it falls back to them, play checks them first.
            recorder = Recorder()
            with recorder.patched():
                answer = client.post(f"/program/items/{item['id']}/play")
            self.assertEqual(answer.status_code, 409, answer.text)
            self.assertIn("switches mutes", answer.json()["reason"])
            self.assertEqual(recorder.ran, [])

    def test_a_put_without_shortcut_keeps_it(self):
        with app_client(PROFILES) as (client, _folder):
            shortcut = client.post("/shortcuts", json={"title": "QR", "desktop": ["qr"]}).json()["shortcut"]
            item = client.post("/program/items", json=row(shortcut=shortcut["id"])).json()["item"]
            # A page from before: no "shortcut" key, an empty list.
            old = client.put(f"/program/items/{item['id']}", json=row(title="Neu", shortcuts=[])).json()["item"]
            self.assertEqual((old["title"], old["shortcut"]), ("Neu", shortcut["id"]))
            old = client.put(f"/program/items/{item['id']}", json=row(title="Neu")).json()["item"]
            self.assertEqual(old["shortcut"], shortcut["id"])
            # A null "shortcut" removes it.
            cleared = client.put(f"/program/items/{item['id']}", json=row(shortcut=None)).json()["item"]
            self.assertIsNone(cleared["shortcut"])

    def test_checks_use_what_the_row_runs(self):
        with app_client(PROFILES) as (client, _folder):
            shortcut = client.post("/shortcuts", json={"title": "QR", "desktop": ["qr"]}).json()["shortcut"]
            # The row's own fields are not run, so they are not what is checked...
            item = client.post("/program/items", json=row(shortcut=shortcut["id"])).json()["item"]
            # ...but the shortcut is: an unknown one is refused.
            refused = client.post("/program/items", json=row(shortcut=C))
            self.assertEqual((refused.status_code, refused.json()["reason"]), (400, "unknown shortcut"))
            refused = client.post("/program/items", json=row(shortcuts=[C]))
            self.assertEqual(refused.json()["reason"], "unknown shortcut", "the old list is checked too")
            refused = client.put(f"/program/items/{item['id']}", json=row(shortcut="x"))
            self.assertEqual(refused.status_code, 400)

            # A shortcut that is gone without the row being told (e.g. a
            # restored file): the row does not start, and says why.
            store = client.app.state.shortcuts
            store._items = [s for s in store._items if s["id"] != shortcut["id"]]
            recorder = Recorder()
            with recorder.patched():
                answer = client.post(f"/program/items/{item['id']}/play")
            self.assertEqual((answer.status_code, answer.json()["reason"]), (409, "unknown shortcut"))
            self.assertEqual(recorder.ran, [], "nothing ran")

    def test_save_as_shortcut_and_export_import(self):
        with app_client(PROFILES) as (client, _folder):
            shortcut = client.post("/shortcuts", json={"title": "QR", "desktop": ["qr"]}).json()["shortcut"]
            item = client.post("/program/items", json=row(desktop=["logo"], shortcut=shortcut["id"])).json()["item"]
            copy = client.post(f"/program/items/{item['id']}/shortcut").json()["shortcut"]
            self.assertEqual((copy["title"], copy["desktop"]), ("Predigt", ["qr"]),
                             "a row with a shortcut is saved as what it runs")

            exported = json.loads(client.get("/program/export").text)
            self.assertEqual(exported["items"][0]["shortcut"], shortcut["id"])
            self.assertNotIn("shortcuts", exported["items"][0])
            # A file from before: a list of shortcuts per row.
            old = {**exported, "items": [{**row(title="Alt"), "shortcuts": [shortcut["id"], copy["id"]]}]}
            imported = client.post("/program/import", json=old).json()
            self.assertTrue(imported["ok"], imported)
            self.assertEqual(imported["items"][0]["shortcut"], shortcut["id"])

    def test_template_rows_keep_their_shortcut(self):
        with app_client(PROFILES) as (client, _folder):
            shortcut = client.post("/shortcuts", json={"title": "QR", "desktop": ["qr"]}).json()["shortcut"]
            client.post("/program/items", json=row(shortcut=shortcut["id"]))
            template = client.post("/program/templates", json={"name": "Gottesdienst"}).json()["template"]
            self.assertEqual(template["rows"][0]["shortcut"], shortcut["id"])
            applied = client.post(f"/program/templates/{template['id']}/apply", json={}).json()
            self.assertEqual((applied["items"][0]["shortcut"], applied["warnings"]), (shortcut["id"], []))


if __name__ == "__main__":
    unittest.main()
