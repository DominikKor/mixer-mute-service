"""Shortcuts and program rows (app/program_items.py): "Shortcut laden"
copies a shortcut's fields into a row's own (a copy: the row always runs
its own fields). Rows and templates saved while a row could refer to a
shortcut get that shortcut's fields copied in once."""

import json
import logging
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.program_items import SHORTCUT_KEYS, ProgramItemStore, apply_shortcut, linked_shortcut_id
from app.program_templates import ProgramTemplateStore

from tests.helpers import app_client

PROFILES = ("desktop_display:\n  enabled: true\n  profiles:\n"
            "    qr:\n      title: QR\n      kind: image\n      path: 'C:\\Bilder\\qr.png'\n"
            "    logo:\n      title: Logo\n      kind: image\n      path: 'C:\\Bilder\\logo.png'\n"
            "groups:\n  Predigt:\n    channels: [1]\n")
A, B, C = "a" * 32, "b" * 32, "c" * 32
LYRICS = "file:192.168.188.11|C:\\Texte\\lied.txt"
TEXTS = {"playback_description": "", "translation_description": "", "lyrics_description": ""}


def row(**extra):
    return {"time": "10:00", "title": "Predigt", "person": "Anna", "group": "", **extra}


def shortcut(shortcut_id, title, **fields):
    base = {"id": shortcut_id, "title": title, "group": "", "playback": None, "translation": None, "lyrics": None,
            **TEXTS, "song": None, "cam1_preset": None, "cam2_preset": None, "camera_wait_seconds": 5.0,
            "light": None, "desktop": [], "cam1_scene": None, "cam2_scene": None, "beamer": None}
    return {**base, **fields}


SHORTCUTS = {
    A: shortcut(A, "Predigt", group="Predigt", cam1_preset=3, camera_wait_seconds=7.0, desktop=["logo"],
                beamer="hide", lyrics=LYRICS, song="anderes.foil"),
    B: shortcut(B, "Kollekte", desktop=["qr"], beamer="unhide"),
}


class CopyTests(unittest.TestCase):
    def test_load_copies_the_action_fields_only(self):
        item = {**row(song=None, desktop=["qr"], beamer="unhide", light="warm"), "id": "r", "hidden": True}
        loaded = apply_shortcut(item, SHORTCUTS[A])
        self.assertEqual({key: loaded[key] for key in ("time", "title", "person", "song", "id", "hidden")},
                         {"time": "10:00", "title": "Predigt", "person": "Anna", "song": None, "id": "r", "hidden": True})
        self.assertEqual((loaded["group"], loaded["cam1_preset"], loaded["camera_wait_seconds"], loaded["desktop"],
                          loaded["beamer"], loaded["light"], loaded["lyrics"]),
                         ("Predigt", 3, 7.0, ["logo"], "hide", None, LYRICS),
                         "every action field is the shortcut's, also the ones it leaves empty")
        loaded["desktop"].append("qr")
        self.assertEqual(SHORTCUTS[A]["desktop"], ["logo"], "a copy, not the shortcut's own list")
        self.assertNotIn("song", SHORTCUT_KEYS)

    def test_a_song_keeps_the_shortcuts_lyrics_file_out(self):
        loaded = apply_shortcut(row(song="lied-12.foil", lyrics=None), SHORTCUTS[A])
        self.assertEqual((loaded["song"], loaded["lyrics"]), ("lied-12.foil", None))

    def test_linked_shortcut_id(self):
        self.assertEqual(linked_shortcut_id({"shortcut": A}), A)
        self.assertEqual(linked_shortcut_id({"shortcuts": [B, A]}), B)
        for entry in ({}, {"shortcut": None}, {"shortcut": "x"}, {"shortcuts": []}, {"shortcuts": "a" * 32}):
            self.assertIsNone(linked_shortcut_id(entry), entry)


class MigrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_linked_rows_get_their_shortcuts_fields_once(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "program.json"
            stored = [
                {**row(title="Ohne"), **TEXTS, "id": "plain"},
                {**row(title="Verlinkt", desktop=["qr"]), **TEXTS, "id": "linked", "shortcut": A, "shortcut_deleted": None},
                {**row(title="Lied", song="lied-12.foil"), **TEXTS, "id": "song", "shortcut": A},
                {**row(title="Alt"), **TEXTS, "id": "list", "shortcuts": [B, A]},
                {**row(title="Weg", beamer="hide"), **TEXTS, "id": "missing", "shortcut": C},
                {**row(title="Zurück", desktop=["logo"]), **TEXTS, "id": "fell-back", "shortcut": None,
                 "shortcut_deleted": "Kollekte"},
            ]
            path.write_text(json.dumps(stored), encoding="utf-8")
            with self.assertLogs("mixer.program", logging.INFO) as logs:
                store = ProgramItemStore(path, SHORTCUTS)
            rows = {item["id"]: item for item in await store.list()}
            self.assertEqual((rows["linked"]["group"], rows["linked"]["desktop"], rows["linked"]["lyrics"],
                              rows["linked"]["person"], rows["linked"]["song"]),
                             ("Predigt", ["logo"], LYRICS, "Anna", None),
                             "it keeps doing what it did: the shortcut's fields, its own person and song")
            self.assertEqual((rows["song"]["song"], rows["song"]["lyrics"], rows["song"]["beamer"]),
                             ("lied-12.foil", None, "hide"), "a song keeps the shortcut's Liedtext file out")
            self.assertEqual((rows["list"]["desktop"], rows["list"]["beamer"]), (["qr"], "unhide"))
            self.assertEqual(rows["missing"]["beamer"], "hide", "a deleted shortcut: its own fields stay")
            self.assertEqual(rows["fell-back"]["desktop"], ["logo"])
            self.assertEqual(rows["plain"]["group"], "")
            self.assertEqual(store.migration, {"rows": 5, "copied": 3, "missing": 1, "none": 1})
            text = "\n".join(logs.output)
            self.assertIn("„Predigt“ copied", text)
            self.assertIn(f"{C} no longer exists", text)
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(all(not set(entry) & {"shortcut", "shortcuts", "shortcut_deleted"} for entry in saved))
            # Idempotent: the second load finds nothing to do.
            before = path.read_text(encoding="utf-8")
            again = ProgramItemStore(path, SHORTCUTS)
            self.assertIsNone(again.migration)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    async def test_templates_too(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "templates.json"
            path.write_text(json.dumps([{"id": "t", "name": "Gottesdienst", "rows": [
                {**row(title="Begrüßung"), **TEXTS, "shortcut": B},
                {**row(title="Lied"), **TEXTS, "shortcuts": [A], "song_slot": True},
                {**row(title="Frei", desktop=["qr"]), **TEXTS},
            ]}]), encoding="utf-8")
            with self.assertLogs("mixer.program", logging.INFO):
                store = ProgramTemplateStore(path, SHORTCUTS)
            rows = (await store.get("t"))["rows"]
            self.assertEqual([(r["title"], r["desktop"], r["song_slot"]) for r in rows],
                             [("Begrüßung", ["qr"], False), ("Lied", ["logo"], True), ("Frei", ["qr"], False)])
            self.assertEqual(store.migration, {"rows": 2, "copied": 2, "missing": 0, "none": 0})
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertTrue(all("shortcut" not in r and "shortcuts" not in r for r in saved[0]["rows"]))
            before = path.read_text(encoding="utf-8")
            self.assertIsNone(ProgramTemplateStore(path, SHORTCUTS).migration)
            self.assertEqual(path.read_text(encoding="utf-8"), before)

    async def test_a_song_or_a_lyrics_file_not_both(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "program.json"
            store = ProgramItemStore(path)
            both = await store.create(row(song="lied-12.foil", lyrics=LYRICS))
            self.assertEqual((both["song"], both["lyrics"]), ("lied-12.foil", None))
            self.assertEqual((await store.create(row(lyrics=LYRICS)))["lyrics"], LYRICS)
            path.write_text(json.dumps([{**row(song="a.foil", lyrics=LYRICS), **TEXTS, "id": "old"}]), encoding="utf-8")
            with self.assertLogs("mixer.program", logging.WARNING):
                old = await ProgramItemStore(path).get("old")
            self.assertEqual((old["song"], old["lyrics"]), ("a.foil", None))


class Recorder:
    def __init__(self):
        self.ran = []

    async def run_profile(self, _request, profile_id):
        self.ran.append(profile_id)
        return {"ok": True}, 200


class ApiTests(unittest.TestCase):
    def test_rows_run_their_own_fields_and_ignore_a_shortcut_key(self):
        with app_client(PROFILES) as (client, _folder):
            made = client.post("/shortcuts", json={"title": "Logo", "desktop": ["logo"]}).json()["shortcut"]
            # A page of v1.2.1-210 still sends "shortcut" (or the list before it).
            item = client.post("/program/items", json=row(desktop=["qr"], shortcut=made["id"])).json()["item"]
            self.assertNotIn("shortcut", item)
            item = client.put(f"/program/items/{item['id']}", json=row(desktop=["qr"], shortcuts=[made["id"]])).json()["item"]
            self.assertEqual(item["desktop"], ["qr"])
            self.assertTrue(all("shortcut" not in i for i in client.get("/program/items").json()))
            recorder = Recorder()
            with patch("app.api.program.run_profile", recorder.run_profile):
                played = client.post(f"/program/items/{item['id']}/play").json()
            self.assertTrue(played["ok"], played)
            self.assertEqual(recorder.ran, ["qr"], "only the row's own fields run")
            self.assertNotIn("shortcut", played)
            # Deleting a shortcut leaves the rows alone.
            self.assertTrue(client.delete(f"/shortcuts/{made['id']}").json()["deleted"])
            self.assertEqual(client.get("/program/items").json()[0]["desktop"], ["qr"])

    def test_import_of_a_file_with_shortcut_references_copies_them(self):
        with app_client(PROFILES) as (client, _folder):
            made = client.post("/shortcuts", json={"title": "Logo", "desktop": ["logo"], "beamer": "hide"}).json()["shortcut"]
            exported = json.loads(client.get("/program/export").text)
            self.assertEqual(exported["items"], [])
            rows = [{**row(title="Verlinkt"), "shortcut": made["id"]},
                    {**row(title="Liste", song="lied.foil"), "shortcuts": [made["id"]]},
                    {**row(title="Gelöscht", desktop=["qr"]), "shortcut": C}]
            imported = client.post("/program/import", json={**exported, "items": rows}).json()
            self.assertTrue(imported["ok"], imported)
            got = {i["title"]: i for i in imported["items"]}
            self.assertEqual((got["Verlinkt"]["desktop"], got["Verlinkt"]["beamer"]), (["logo"], "hide"))
            self.assertEqual((got["Liste"]["desktop"], got["Liste"]["song"]), (["logo"], "lied.foil"))
            self.assertEqual(got["Gelöscht"]["desktop"], ["qr"])
            self.assertTrue(all("shortcut" not in i for i in imported["items"]))
            self.assertNotIn("shortcut", json.loads(client.get("/program/export").text)["items"][0])


if __name__ == "__main__":
    unittest.main()
