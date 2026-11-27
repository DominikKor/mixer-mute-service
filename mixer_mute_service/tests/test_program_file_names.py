"""Readable names in the program file (app/program_file.py): the export puts a
name next to every ID, the import trusts the ID and falls back to an exact name
(any case) that fits one item; unknown or ambiguous references are dropped with
a warning; files without names (and unknown *_name fields) still import. The
Desktop steps (app/row_files.py) carry one name each; old files with Playback /
Übersetzung come in as steps."""

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


# A key from the old Übersetzung field, two Desktop keys, the song.
STEPS = ["key:native-cam1-preset-1", "qr", "logo", "@song"]
STEP_NAMES = ["CAM1 Pult nah", "QR Gemeinde", "Logo", "Liedtext"]


def setup(client):
    client.app.state.songs.songs = dict(LIBRARY)
    asyncio.run(client.app.state.companion_button_store.save_camera_preset("cam1", 9, "Kanzel"))
    light = scene(client, "Predigt hell")
    row = {"time": "10:00", "title": "Predigt", "person": "Anna", "group": "Predigt", "light": light["id"],
           "desktop": STEPS, "song": "a.foil", "cam1_preset": 9}
    assert client.post("/program/items", json=row).status_code == 200
    return light


def export(client):
    return client.get("/program/export").json()


def import_(client, payload):
    return client.post("/program/import", json=payload)


class ProgramFileNameTests(unittest.TestCase):
    def test_export_writes_opaque_ids_as_names(self):
        with app_client(CONFIG) as (client, _):
            setup(client)
            payload = export(client)
            self.assertNotIn("warnings", payload)
            item = payload["items"][0]
            self.assertEqual(item["light"], "Predigt hell")  # the scene's hex ID is written as its name
            self.assertNotIn("light_name", item)
            self.assertEqual((item["desktop"], item["desktop_names"]), (STEPS, STEP_NAMES))  # catalog key: its ID
            self.assertEqual((item["song"], item["song_title"], item["song_book"]),
                             ("a.foil", "Above all", "Glauben Hoffen Singen 371"))
            self.assertEqual(item["cam1_preset"], 9)
            self.assertEqual(item["cam1_preset_name"], "9 · Kanzel")
            self.assertIsNone(item["playback"])
            self.assertEqual(item["group"], "Predigt")  # readable IDs stay: group, song file, desktop profile

    def test_user_keys_and_saved_presets_are_written_as_names(self):
        with app_client(CONFIG) as (client, _):
            setup(client)
            store = client.app.state.companion_button_store
            key = asyncio.run(store.create_button("Kollekte Musik", "overall", {"kind": "channel", "channel": 1, "operation": "mute"}))
            self.assertTrue(key["id"].startswith("user-"))
            row = client.get("/program/items").json()[0]
            # (a key that switches mutes may not share a row with a group)
            steps = ["key:" + key["id"], "key:native-cam1-preset-9", "qr", "@song"]  # a saved preset: "9 · Kanzel"
            self.assertEqual(client.put(f"/program/items/{row['id']}", json={**row, "desktop": steps, "group": ""}).status_code, 200)
            payload = export(client)
            self.assertNotIn("warnings", payload)
            item = payload["items"][0]
            self.assertEqual(item["desktop"], ["key:Kollekte Musik", "key:Kanzel", "qr", "@song"])
            self.assertEqual(item["desktop_names"], ["Kollekte Musik", "Kanzel", "QR Gemeinde", "Liedtext"])
            self.assertEqual(import_(client, payload).json()["warnings"], [])  # and back to the IDs
            self.assertEqual(client.get("/program/items").json()[0]["desktop"], steps)
            # A file from before the steps, with the names in Playback / Übersetzung: steps as well.
            item.update(desktop=["qr"], desktop_names=["QR Gemeinde"], playback="Kanzel", translation="kollekte MUSIK")
            self.assertEqual(import_(client, payload).json()["warnings"], [])
            now = client.get("/program/items").json()[0]
            self.assertEqual(now["desktop"], ["key:native-cam1-preset-9", "key:" + key["id"], "qr", "@song"])
            self.assertEqual((now["playback"], now["translation"]), (None, None))

    def test_a_named_key_comes_before_a_catalog_key_of_the_same_name(self):
        with app_client(CONFIG) as (client, _):
            setup(client)
            store = client.app.state.companion_button_store
            key = asyncio.run(store.create_button("Beamer hide", "overall", {"kind": "companion", "target_button_id": "beamer-hide"}))
            payload = export(client)
            payload["items"][0].pop("desktop_names")
            payload["items"][0].update(desktop=["key:beamer HIDE", "key:beamer-hide"], group="")
            self.assertEqual(import_(client, payload).json()["warnings"], [])
            self.assertEqual(client.get("/program/items").json()[0]["desktop"],
                             ["key:" + key["id"], "key:beamer-hide", "@song"])  # an existing ID stays itself
            self.assertEqual(export(client)["items"][0]["desktop"], ["key:Beamer hide", "key:beamer-hide", "@song"])

    def test_a_stale_name_next_to_a_key_step_does_not_win(self):
        with app_client(CONFIG) as (client, _):
            setup(client)
            store = client.app.state.companion_button_store
            action = {"kind": "channel", "channel": 1, "operation": "mute"}
            first = asyncio.run(store.create_button("Kollekte", "overall", action))
            asyncio.run(store.create_button("Segen", "overall", action))
            payload = export(client)
            # edited by hand: the step says "Kollekte", the name next to it is still the old one
            payload["items"][0].update(desktop=["key:kollekte", "key:user-000000000000"], group="",
                                       desktop_names=["Segen", "Gibt es nicht"])
            r = import_(client, payload)
            self.assertEqual([w["reason"] for w in r.json()["warnings"]],
                             ["Taste „Gibt es nicht“ nicht gefunden; Verweis entfernt"])
            self.assertEqual(client.get("/program/items").json()[0]["desktop"], ["key:" + first["id"], "@song"])

    def test_round_trip_is_byte_stable(self):
        with app_client(CONFIG) as (client, _):
            setup(client)
            key = asyncio.run(client.app.state.companion_button_store.create_button(
                "Kollekte Musik", "overall", {"kind": "channel", "channel": 1, "operation": "mute"}))
            client.post("/program/items", json={"time": "10:30", "title": "Zweite Zeile", "person": "", "group": "",
                                                "song": "b.foil", "desktop": ["key:" + key["id"], "logo",
                                                                             "via:qr|C:\\Bilder\\b.pdf", "@freeze"]})
            self.assertIn('"key:Kollekte Musik"', client.get("/program/export").text)
            first = client.get("/program/export").content
            self.assertEqual(import_(client, client.get("/program/export").json()).status_code, 200)
            second = client.get("/program/export").content
            self.assertEqual(first, second)
            self.assertEqual(import_(client, client.get("/program/export").json()).status_code, 200)
            self.assertEqual(second, client.get("/program/export").content)

    def test_old_exports_with_ids_and_names_still_import(self):
        with app_client(CONFIG) as (client, _):
            light = setup(client)
            old = export(client)
            item = old["items"][0]
            item.update(light=light["id"], light_name="Predigt hell")  # the form before names were the keys
            r = import_(client, old)
            self.assertEqual((r.status_code, r.json()["warnings"]), (200, []))
            before = client.get("/program/items").json()[0]
            self.assertEqual((before["light"], before["desktop"], before["song"]), (light["id"], STEPS, "a.foil"))
            for key in [k for k in item if k.endswith("_name") or k.endswith("_names") or k in ("song_title", "song_book")]:
                item.pop(key)  # even without any name next to the ID
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
            item["desktop"] = ["key:native-gibt-es-nicht", "alt1", "via:alt2|C:\\Bilder\\a.png", "@song"]
            item["desktop_names"] = ["cam1 pult nah", "qr gemeinde", "QR GEMEINDE", "Liedtext"]
            item["song"] = "weg.foil"
            item["song_title"] = "ACH MEIN HERR JESU"
            r = import_(client, payload)
            self.assertEqual((r.status_code, r.json()["warnings"]), (200, []), r.text)
            now = client.get("/program/items").json()[0]
            self.assertEqual(now["light"], light["id"])
            # A key with another file keeps its file.
            self.assertEqual(now["desktop"], ["key:native-cam1-preset-1", "qr", "via:qr|C:\\Bilder\\a.png", "@song"])
            self.assertEqual(now["song"], "b.foil")

    def test_the_id_wins_when_it_exists(self):
        with app_client(CONFIG) as (client, _):
            light = setup(client)
            other = scene(client, "Andere")
            payload = export(client)
            payload["items"][0]["light"] = light["id"]  # the old form: the ID, and a name that points elsewhere
            payload["items"][0]["light_name"] = "Andere"
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
            self.assertIn("Desktop-Taste „logo“ mehrdeutig (2 passen)", reasons)
            self.assertIn("Licht „Gibt es nicht“ nicht gefunden", reasons)
            self.assertIn("Mute-Gruppe „Gibt es nicht“ nicht gefunden", reasons)
            self.assertIn("Lied „Gibt es nicht“ nicht gefunden", reasons)
            self.assertEqual({w["kind"] for w in r.json()["warnings"]}, {"Licht", "Desktop-Taste", "Lied", "Mute-Gruppe"})
            self.assertEqual(r.json()["warnings"][0]["title"], "Predigt")
            now = client.get("/program/items").json()[0]
            self.assertEqual((now["desktop"], now["light"], now["song"], now["group"]), (["qr"], None, None, ""))
            self.assertEqual(now["title"], "Predigt")  # the row itself is imported


class UniqueNamesTests(unittest.TestCase):
    def test_duplicate_key_names_are_refused_on_save(self):
        from app.companion_buttons import DuplicateLabelError

        with app_client(CONFIG) as (client, _):
            store = client.app.state.companion_button_store
            action = {"kind": "channel", "channel": 1, "operation": "mute"}
            first = asyncio.run(store.create_button("Kollekte", "overall", action))
            with self.assertRaises(DuplicateLabelError) as caught:
                asyncio.run(store.create_button("  kollekte ", "overall", action))
            self.assertIn("eindeutig", str(caught.exception))
            other = asyncio.run(store.create_button("Andere", "overall", action))
            with self.assertRaises(DuplicateLabelError):
                asyncio.run(store.update_button(other["id"], "KOLLEKTE", "overall"))
            asyncio.run(store.update_button(first["id"], "Kollekte", "overall"))  # its own name is fine
            asyncio.run(store.create_button("Beamer hide", "overall", action))  # a catalog key's name is fine:
            # catalog keys keep their readable IDs, and the named key comes first when a file says "Beamer hide"
            asyncio.run(store.save_camera_preset("cam1", 9, "CAM1 Kanzel"))
            asyncio.run(store.save_camera_preset("cam1", 9, "CAM1 Kanzel"))  # saving the same preset again is fine
            with self.assertRaises(DuplicateLabelError):  # a saved preset is a named key too
                asyncio.run(store.save_camera_preset("cam2", 9, "cam1  kanzel"))
            with self.assertRaises(DuplicateLabelError):
                asyncio.run(store.set_camera_label("native-cam1-preset-9", "Kollekte"))
            with self.assertRaises(DuplicateLabelError):  # and a user key may not take its name
                asyncio.run(store.create_button("cam1 KANZEL", "overall", action))
            # the page gets the German reason
            r = client.post("/companion/buttons", json={"label": "kollekte", "category_id": "overall", "kind": "channel",
                                                        "channel": 1, "operation": "mute"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("eindeutig", r.json()["reason"])

    def test_duplicate_scene_names_are_refused_on_save(self):
        with app_client(CONFIG) as (client, _):
            scenes = client.app.state.light_scenes
            entry = {"name": "Predigt hell", "stage": {"fixtures": {"left": {"on": True, "level": 100}}}}
            asyncio.run(scenes.save(entry))
            for name in ("predigt HELL", " Predigt   hell "):
                with self.assertRaises(KeyError):
                    asyncio.run(scenes.save({**entry, "name": name}))
            other = asyncio.run(scenes.save({**entry, "name": "Andere"}))
            with self.assertRaises(KeyError):
                asyncio.run(scenes.save({**entry, "name": "predigt hell"}, scene_id=other["id"]))
            with self.assertRaises(FileExistsError):
                asyncio.run(scenes.rename(other["id"], "PREDIGT HELL"))

    def test_existing_duplicates_keep_working_are_logged_and_reported_in_the_export(self):
        from app.lights import LightSceneStore

        with app_client(CONFIG) as (client, folder):
            light = setup(client)
            scenes = client.app.state.light_scenes
            twin = {**asyncio.run(scenes.get(light["id"])), "id": "d" * 32}  # stored data with a duplicate name
            scenes._scenes.append(twin)
            row = client.get("/program/items").json()[0]
            client.put(f"/program/items/{row['id']}", json={**row, "light": twin["id"]})
            payload = export(client)
            item = payload["items"][0]
            self.assertEqual((item["light"], item["light_name"]), (twin["id"], "Predigt hell"))  # still the ID
            self.assertEqual(payload["warnings"][0]["kind"], "Licht")
            self.assertIn("nicht eindeutig (2×)", payload["warnings"][0]["reason"])
            # importing that file by ID works; by the shared name it is ambiguous and dropped
            self.assertEqual(import_(client, payload).json()["warnings"], [])
            payload["items"][0].pop("light_name")
            payload["items"][0]["light"] = "predigt hell"
            r = import_(client, payload)
            self.assertIn("mehrdeutig (2 passen)", r.json()["warnings"][0]["reason"])
            # a store that loads such a file logs it
            path = folder / "dups.json"
            import json
            path.write_text(json.dumps([light | {"id": "1" * 32} if False else
                                        {"id": "1" * 32, "name": "Gleich", "stage": {"fixtures": {"left": {"on": True, "level": 1}}}},
                                        {"id": "2" * 32, "name": "gleich", "stage": {"fixtures": {"left": {"on": True, "level": 2}}}}]))
            with self.assertLogs("mixer.lights", "WARNING") as logged:
                loaded = LightSceneStore(path)
            self.assertEqual(len(asyncio.run(loaded.list())), 2)
            self.assertIn("Gleich ×2", logged.output[0])

    def test_duplicate_key_names_in_stored_data_are_logged(self):
        with app_client(CONFIG) as (client, _):
            store = client.app.state.companion_button_store
            action = {"kind": "channel", "channel": 1, "operation": "mute"}
            asyncio.run(store.create_button("Zwilling", "overall", action))
            store._buttons.append({"id": "user-" + "a" * 12, "label": "zwilling", "category_id": "overall", **action})
            self.assertEqual(asyncio.run(store.duplicate_labels()), [("Zwilling", 2)])

    def test_a_duplicate_camera_preset_name_does_not_touch_the_camera(self):
        from unittest.mock import AsyncMock, patch

        with app_client(CONFIG) as (client, _):
            store = client.app.state.companion_button_store
            asyncio.run(store.save_camera_preset("cam1", 9, "CAM1 Kanzel"))
            visca = AsyncMock(return_value={"ok": True})
            with patch.object(client.app.state.native_controls, "store_visca_preset", visca):
                r = client.post("/cameras/cam1/presets", json={"preset": 10, "name": "kanzel"})
                self.assertEqual(r.status_code, 400)
                self.assertIn("eindeutig", r.json()["reason"])
                visca.assert_not_called()  # the camera's position at 10 was not overwritten
                self.assertIsNone(asyncio.run(store.camera_preset("cam1", 10)))
                ok = client.post("/cameras/cam1/presets", json={"preset": 10, "name": "Orgel"})  # a free name stores
                self.assertEqual(ok.status_code, 200)
                self.assertEqual(visca.await_count, 1)

    def test_renaming_a_light_preset_to_a_taken_name_answers_in_german(self):
        with app_client(CONFIG) as (client, _):
            scene(client, "Predigt hell")
            other = scene(client, "Andere")
            r = client.put(f"/lights/scenes/{other['id']}", json={"name": "predigt HELL"})
            self.assertEqual(r.status_code, 409)
            self.assertIn("eindeutig", r.json()["reason"])

    def test_existing_duplicates_do_not_block_unrelated_edits(self):
        with app_client(CONFIG) as (client, _):
            store = client.app.state.companion_button_store
            action = {"kind": "channel", "channel": 1, "operation": "mute"}
            first = asyncio.run(store.create_button("Zwilling", "overall", action))
            twin_id = "user-" + "a" * 12
            store._buttons.append({"id": twin_id, "label": "zwilling", "category_id": "overall", **action})
            categories = asyncio.run(store.snapshot())["categories"]
            other = next(c["id"] for c in categories if c["id"] != "overall")
            moved = asyncio.run(store.update_button(twin_id, "zwilling", other))  # same name, new category
            self.assertEqual(moved["category_id"], other)
            asyncio.run(store.update_button(first["id"], " ZWILLING ", "overall"))
            from app.companion_buttons import DuplicateLabelError
            with self.assertRaises(DuplicateLabelError):  # but a new key with a taken name is still refused
                asyncio.run(store.create_button("zwilling", "overall", action))
            # a saved preset keeps its name through an unchanged rename, too
            asyncio.run(store.save_camera_preset("cam1", 9, "CAM1 Kanzel"))
            store._saved_presets["native-cam2-preset-9"] = "CAM1 Kanzel"
            asyncio.run(store.set_camera_label("native-cam2-preset-9", "cam1  kanzel"))
            asyncio.run(store.save_camera_preset("cam2", 9, "CAM1 Kanzel"))

    def test_names_that_look_like_an_id_or_a_prefix_keep_the_id(self):
        with app_client(CONFIG) as (client, _):
            light = setup(client)
            store = client.app.state.companion_button_store
            action = {"kind": "channel", "channel": 1, "operation": "mute"}
            for label in ("desktop:qr", "file:x", "via:y", "key:z", "@Anna", "beamer-hide"):
                key = asyncio.run(store.create_button(label, "overall", action))
                row = client.get("/program/items").json()[0]
                client.put(f"/program/items/{row['id']}", json={**row, "desktop": ["key:" + key["id"]], "group": ""})
                payload = export(client)
                item = payload["items"][0]
                self.assertEqual((item["desktop"], item["desktop_names"]), (["key:" + key["id"], "@song"], [label, "Liedtext"]), label)
                self.assertEqual(payload["warnings"][0]["kind"], "Taste", label)
                self.assertIn("sieht aus wie eine ID", payload["warnings"][0]["reason"])
                self.assertEqual(import_(client, payload).json()["warnings"], [])  # by ID: the same key again
                self.assertEqual(export(client)["items"][0]["desktop"][0], "key:" + key["id"])
                asyncio.run(store.delete_button(key["id"]))
            # a light preset named like its own or another scene's ID
            other = scene(client, light["id"])
            row = client.get("/program/items").json()[0]
            client.put(f"/program/items/{row['id']}", json={**row, "light": other["id"], "desktop": ["qr"]})
            item = export(client)["items"][0]
            self.assertEqual((item["light"], item["light_name"]), (other["id"], light["id"]))


class ProgramFileRobustnessTests(unittest.TestCase):
    def test_a_reference_never_changes_its_kind(self):
        with app_client(CONFIG) as (client, _):
            setup(client)
            payload = export(client)
            item = payload["items"][0]
            item["playback"], item["playback_name"] = "desktop:gone", "CAM1 Pult nah"  # a key's name for a profile
            item["translation"], item["translation_name"] = "native-gone", "QR Gemeinde"  # a profile's name for a key
            r = import_(client, payload)
            self.assertEqual(r.status_code, 200, r.text)
            now = client.get("/program/items").json()[0]
            self.assertEqual((now["playback"], now["translation"]), (None, None))
            kinds = {(w["field"], w["kind"]) for w in r.json()["warnings"]}
            self.assertEqual(kinds, {("playback", "Desktop-Profil"), ("translation", "Taste")})

    def test_malformed_values_are_a_400_not_a_500(self):
        with app_client(CONFIG) as (client, _):
            setup(client)
            for field, value in (("light", ["a"]), ("light", {"a": 1}), ("desktop", [{"a": 1}]), ("desktop", "qr"),
                                 ("song", {"a": 1}), ("group", ["Predigt"]), ("playback", ["x"]), ("playback", {"a": 1}),
                                 ("song_title", ["x"]), ("light_name", {"a": 1}), ("desktop_names", "qr")):
                payload = export(client)
                payload["items"][0][field] = value
                r = import_(client, payload)
                if field.endswith("_name") or field.endswith("_names") or field == "song_title":
                    self.assertEqual(r.status_code, 200, (field, value, r.text))  # only read where they are texts
                else:
                    self.assertEqual(r.status_code, 400, (field, value, r.text))
                self.assertLess(r.status_code, 500, (field, value))

    def test_the_book_decides_between_songs_with_the_same_title(self):
        with app_client(CONFIG) as (client, _):
            setup(client)
            twin = {"title": "Above all", "titles": ["Above all"], "copyright": "", "slides": [{"key": "1", "text": "a"}]}
            client.app.state.songs.songs = {
                "ghs.foil": {**twin, "books": [{"name": "Glauben Hoffen Singen", "number": "371"}]},
                "wlg.foil": {**twin, "books": [{"name": "Wir loben Gott", "number": "5"}]},
            }
            payload = export(client)
            item = payload["items"][0]
            item.update(song="weg.foil", song_title="above ALL", song_book="wir loben gott 5")
            self.assertEqual(import_(client, payload).json()["warnings"], [])
            self.assertEqual(client.get("/program/items").json()[0]["song"], "wlg.foil")
            item.update(song="weg.foil", song_book="Gibt es nicht 1")  # the book fits none: still ambiguous
            r = import_(client, payload)
            self.assertEqual([w["reason"] for w in r.json()["warnings"]],
                             ["Lied „above ALL“ mehrdeutig (2 passen); Verweis entfernt"])
            item.pop("song_book")
            self.assertEqual(len(import_(client, payload).json()["warnings"]), 1)

    def test_a_name_typed_into_the_id_field(self):
        with app_client(CONFIG) as (client, _):
            light = setup(client)
            payload = export(client)
            item = payload["items"][0]
            for key in [k for k in item if k.endswith("_name") or k.endswith("_names") or k in ("song_title", "song_book")]:
                item.pop(key)
            item.update(light="predigt HELL", desktop=["qr gemeinde", "Logo2"], song="Above all",
                        playback="desktop:QR Gemeinde", translation="CAM1 Pult nah", group="predigt")
            r = import_(client, payload)
            self.assertEqual(r.status_code, 200, r.text)
            now = client.get("/program/items").json()[0]
            # An old file: Playback (a profile already among the steps) and Übersetzung
            # (a key) become steps, the song the last one.
            self.assertEqual((now["light"], now["desktop"], now["song"], now["group"]),
                             (light["id"], ["key:native-cam1-preset-1", "qr", "@song"], "a.foil", "Predigt"))
            self.assertEqual((now["playback"], now["translation"]), (None, None))
            self.assertEqual([w["name"] for w in r.json()["warnings"]], ["Logo2"])

    def test_a_row_without_title_is_still_reported(self):
        with app_client(CONFIG) as (client, _):
            setup(client)
            payload = export(client)
            payload["items"][0].update(title="", light="e" * 32, light_name="Weg")
            r = import_(client, payload)
            self.assertEqual(r.status_code, 400)  # an empty title is invalid: nothing imported, no crash
            payload["items"][0]["title"] = " x "
            payload["items"][0].pop("title")
            self.assertLess(import_(client, payload).status_code, 500)


if __name__ == "__main__":
    unittest.main()
