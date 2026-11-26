"""Readable names in the program file (app/program_file.py): the export puts a
name next to every ID, the import trusts the ID and falls back to an exact name
(any case) that fits one item; unknown or ambiguous references are dropped with
a warning; files without names (and unknown *_name fields) still import."""

import asyncio
import unittest

from tests.helpers import app_client

CONFIG = ("desktop_display:\n  enabled: true\n  profiles:\n"
          "    qr:\n      title: QR Gemeinde\n      kind: image\n      path: 'C:\\Bilder\\qr.png'\n"
          "    logo:\n      title: Logo\n      kind: image\n      path: 'C:\\Bilder\\logo.png'\n"
          "    logo2:\n      title: LOGO\n      kind: image\n      path: 'C:\\Bilder\\logo2.png'\n"
          "groups:\n  Predigt:\n    channels: [1]\n")
LIBRARY = {
    "a.foil": {"title": "Above all", "titles": ["Above all"], "books": [{"name": "Glauben Hoffen Singen", "number": "371"}],
               "copyright": "", "slides": [{"key": "1", "text": "a"}]},
    "b.foil": {"title": "Ach mein Herr Jesu", "titles": ["Ach mein Herr Jesu"], "books": [],
               "copyright": "", "slides": [{"key": "1", "text": "a"}]},
}


def scene(client, name):
    """A light scene straight into the store (the route would ask the Shellys)."""
    return asyncio.run(client.app.state.light_scenes.save({"name": name, "stage": {"fixtures": {"left": {"on": True, "level": 100}}}}))


def setup(client):
    client.app.state.songs.songs = dict(LIBRARY)
    asyncio.run(client.app.state.companion_button_store.save_camera_preset("cam1", 9, "Kanzel"))
    light = scene(client, "Predigt hell")
    row = {"time": "10:00", "title": "Predigt", "person": "Anna", "group": "Predigt", "light": light["id"],
           "desktop": ["qr", "logo"], "song": "a.foil", "cam1_preset": 9, "playback": "desktop:qr",
           "translation": "native-cam1-preset-1"}
    assert client.post("/program/items", json=row).status_code == 200
    return light


def export(client):
    return client.get("/program/export").json()


def import_(client, payload):
    return client.post("/program/import", json=payload)


class ProgramFileNameTests(unittest.TestCase):
    def test_export_has_the_names_next_to_the_ids(self):
        with app_client(CONFIG) as (client, _):
            light = setup(client)
            item = export(client)["items"][0]
            self.assertEqual((item["light"], item["light_name"]), (light["id"], "Predigt hell"))
            self.assertEqual((item["desktop"], item["desktop_names"]), (["qr", "logo"], ["QR Gemeinde", "Logo"]))
            self.assertEqual((item["song"], item["song_title"], item["song_book"]),
                             ("a.foil", "Above all", "Glauben Hoffen Singen 371"))
            self.assertEqual(item["cam1_preset_name"], "9 · Kanzel")
            self.assertEqual((item["playback"], item["playback_name"]), ("desktop:qr", "QR Gemeinde"))
            self.assertEqual(item["translation_name"], "CAM1 Pult nah")
            self.assertEqual(item["group"], "Predigt")  # a group's ID is its name already
            keys = list(item)
            self.assertEqual(keys[keys.index("light") + 1], "light_name")  # right next to the ID

    def test_roundtrip_and_old_files_without_names(self):
        with app_client(CONFIG) as (client, _):
            light = setup(client)
            payload = export(client)
            r = import_(client, payload)
            self.assertEqual((r.status_code, r.json()["warnings"]), (200, []))
            before = client.get("/program/items").json()[0]
            self.assertEqual((before["light"], before["desktop"], before["song"]), (light["id"], ["qr", "logo"], "a.foil"))
            old = {**payload, "items": [{k: v for k, v in i.items() if not k.endswith("_name") and not k.endswith("_names")
                                         and k not in ("song_title", "song_book")} for i in payload["items"]]}
            self.assertNotIn("light_name", old["items"][0])
            r = import_(client, old)
            self.assertEqual((r.status_code, r.json()["warnings"]), (200, []))
            self.assertEqual(client.get("/program/items").json()[0]["light"], light["id"])

    def test_unknown_name_fields_are_ignored(self):
        with app_client(CONFIG) as (client, _):
            setup(client)
            payload = export(client)
            payload["items"][0]["zukunft_name"] = "egal"
            self.assertEqual(import_(client, payload).status_code, 200)

    def test_import_by_name_when_the_ids_differ(self):
        with app_client(CONFIG) as (client, _):
            light = setup(client)
            payload = export(client)
            item = payload["items"][0]
            item["light"] = "f" * 32  # the scene has another ID on this installation
            item["light_name"] = "predigt  HELL"
            item["desktop"] = ["alt1", "logo"]
            item["desktop_names"] = ["qr gemeinde", "Logo"]
            item["song"] = "weg.foil"
            item["song_title"] = "ACH MEIN HERR JESU"
            item["playback"] = "desktop:alt"
            item["playback_name"] = "QR Gemeinde"
            item["translation"] = "native-gibt-es-nicht"
            item["translation_name"] = "cam1 pult nah"
            r = import_(client, payload)
            self.assertEqual((r.status_code, r.json()["warnings"]), (200, []), r.text)
            now = client.get("/program/items").json()[0]
            self.assertEqual(now["light"], light["id"])
            self.assertEqual(now["desktop"], ["qr", "logo"])
            self.assertEqual(now["song"], "b.foil")
            self.assertEqual((now["playback"], now["translation"]), ("desktop:qr", "native-cam1-preset-1"))

    def test_the_id_wins_when_it_exists(self):
        with app_client(CONFIG) as (client, _):
            light = setup(client)
            other = scene(client, "Andere")
            payload = export(client)
            payload["items"][0]["light_name"] = "Andere"  # the name points elsewhere, the ID exists
            payload["items"][0]["song_title"] = "Ach mein Herr Jesu"
            self.assertEqual(import_(client, payload).status_code, 200)
            now = client.get("/program/items").json()[0]
            self.assertEqual((now["light"], now["song"]), (light["id"], "a.foil"))
            self.assertNotEqual(other["id"], light["id"])

    def test_ambiguous_or_unknown_names_give_warnings_and_drop_the_reference(self):
        with app_client(CONFIG) as (client, _):
            setup(client)
            scene(client, "Zwilling A")
            payload = export(client)
            item = payload["items"][0]
            item["desktop"] = ["x1", "x2", "qr"]
            item["desktop_names"] = ["logo", "Gibt es nicht", "QR Gemeinde"]  # "logo" fits Logo and LOGO
            item["light"] = "e" * 32
            item["light_name"] = "Gibt es nicht"
            item["song"] = "nope.foil"
            item["song_title"] = "Gibt es nicht"
            item["group"] = "Gibt es nicht"
            r = import_(client, payload)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertEqual(len(r.json()["warnings"]), 5)
            reasons = " | ".join(w["reason"] for w in r.json()["warnings"])
            self.assertIn("ambiguous (2 fit)", reasons)
            self.assertIn("light scene 'Gibt es nicht' not found", reasons)
            self.assertIn("mute group 'Gibt es nicht'", reasons)
            self.assertIn("song 'Gibt es nicht' not found", reasons)
            self.assertEqual(r.json()["warnings"][0]["title"], "Predigt")
            now = client.get("/program/items").json()[0]
            self.assertEqual((now["desktop"], now["light"], now["song"], now["group"]), (["qr"], None, None, ""))
            self.assertEqual(now["title"], "Predigt")  # the row itself is imported


if __name__ == "__main__":
    unittest.main()
