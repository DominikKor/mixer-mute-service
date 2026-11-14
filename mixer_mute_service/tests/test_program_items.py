import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.companion_buttons import COMPANION_BUTTONS, CompanionButtonStore
from app.program_items import ProgramItemStore


def row(time, title, group="Chor"):
    return {"time": time, "title": title, "person": "", "group": group}


class ProgramStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "program.json"
        self.store = ProgramItemStore(self.path)

    async def test_status_counts_only_todays_starts(self):
        first = await self.store.create(row("10:00", "Lied"))
        second = await self.store.create(row("10:10", "Predigt"))
        now = datetime(2026, 11, 8, 10, 5).astimezone()
        await self.store.mark_played(first["id"], now - timedelta(days=7))
        status = await self.store.status(now)
        self.assertEqual(status, {"current_id": None, "next_id": first["id"]})

        await self.store.mark_played(first["id"], now)
        status = await self.store.status(now)
        self.assertEqual(status, {"current_id": first["id"], "next_id": second["id"]})

        await self.store.mark_played(second["id"], now + timedelta(minutes=5))
        status = await self.store.status(now)
        self.assertEqual(status, {"current_id": second["id"], "next_id": None})

    async def test_order_by_time_keeps_insertion_order_on_ties(self):
        a = await self.store.create(row("10:00", "A"))
        b = await self.store.create(row("09:00", "B"))
        c = await self.store.create(row("10:00", "C"))
        dup = await self.store.duplicate(a["id"])
        titles = [item["id"] for item in await self.store.list()]
        self.assertEqual(titles, [b["id"], a["id"], dup["id"], c["id"]])
        # Order survives a restart.
        reloaded = ProgramItemStore(self.path)
        self.assertEqual([i["id"] for i in await reloaded.list()], titles)

    async def test_group_is_optional(self):
        item = await self.store.create(row("10:00", "Nur Kamera", group=None))
        self.assertEqual(item["group"], "")

    async def test_rename_group(self):
        await self.store.create(row("10:00", "A", group="Alt"))
        await self.store.create(row("10:05", "B", group="Andere"))
        self.assertEqual(await self.store.rename_group("Alt", "Neu"), 1)
        self.assertEqual([i["group"] for i in await self.store.list()], ["Neu", "Andere"])


class ProgramHiddenTests(unittest.IsolatedAsyncioTestCase):
    """A hidden row stays in the list, greyed out, and "Weiter" skips it."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "program.json"
        self.store = ProgramItemStore(self.path)
        self.now = datetime(2026, 11, 8, 10, 5).astimezone()

    async def test_next_skips_hidden_rows(self):
        a = await self.store.create(row("10:00", "A"))
        b = await self.store.create(row("10:10", "B"))
        c = await self.store.create(row("10:20", "C"))
        d = await self.store.create(row("10:30", "D"))
        self.assertFalse(a["hidden"])

        await self.store.set_hidden(a["id"], True)
        self.assertEqual((await self.store.status(self.now))["next_id"], b["id"])
        await self.store.mark_played(b["id"], self.now)
        await self.store.set_hidden(c["id"], True)
        self.assertEqual(await self.store.status(self.now), {"current_id": b["id"], "next_id": d["id"]})
        await self.store.set_hidden(d["id"], True)
        self.assertEqual((await self.store.status(self.now))["next_id"], None)   # only hidden rows left
        await self.store.set_hidden(c["id"], False)
        self.assertEqual((await self.store.status(self.now))["next_id"], c["id"])
        self.assertEqual(len(await self.store.list()), 4)                         # nothing removed

    async def test_hidden_survives_edits_restart_and_duplicate(self):
        a = await self.store.create(row("10:00", "A"))
        hidden = await self.store.set_hidden(a["id"], True)
        self.assertTrue(hidden["hidden"])
        edited = await self.store.update(a["id"], row("10:01", "A2"))
        self.assertTrue(edited["hidden"])
        copy = await self.store.duplicate(a["id"])
        self.assertTrue(copy["hidden"])
        reloaded = ProgramItemStore(self.path)
        self.assertTrue((await reloaded.get(a["id"]))["hidden"])
        self.assertIsNone(await self.store.set_hidden("nope", True))

    async def test_old_rows_and_templates_are_visible(self):
        self.path.write_text(json.dumps([{"id": "old", **row("10:00", "Alt")}]), encoding="utf-8")
        self.assertFalse((await ProgramItemStore(self.path).get("old"))["hidden"])
        replaced = await self.store.replace_all([row("11:00", "Neu")])
        self.assertFalse(replaced[0]["hidden"])


class ProgramHiddenRouteTests(unittest.TestCase):
    def test_hide_route_and_next_skips(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                "server:\n  host: 127.0.0.1\n  port: 8765\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MIXER_CONFIG": str(config_path)}):
                from fastapi.testclient import TestClient
                from app.main import app

                with TestClient(app) as client:
                    ids = [client.post("/program/items", json={**row(t, t), "group": ""}).json()["item"]["id"]
                           for t in ("10:00", "10:10", "10:20")]
                    r = client.post(f"/program/items/{ids[0]}/hidden", json={"hidden": True})
                    self.assertEqual(r.status_code, 200)
                    self.assertTrue(r.json()["item"]["hidden"])
                    listed = {i["id"]: i for i in client.get("/program/items").json()}
                    self.assertTrue(listed[ids[0]]["hidden"])
                    self.assertFalse(listed[ids[1]]["hidden"])
                    self.assertEqual(client.get("/program/status").json()["next_id"], ids[1])

                    played = client.post("/program/next", json={"expected_next_id": ids[1]})
                    self.assertEqual(played.status_code, 200, played.text)
                    self.assertEqual(played.json()["program_item_id"], ids[1])
                    self.assertEqual(client.get("/program/status").json()["next_id"], ids[2])

                    self.assertEqual(client.post(f"/program/items/{ids[0]}/hidden", json={"hidden": "ja"}).status_code, 400)
                    self.assertEqual(client.post("/program/items/nope/hidden", json={"hidden": True}).status_code, 404)
                    self.assertFalse(client.post(f"/program/items/{ids[0]}/hidden", json={"hidden": False}).json()["item"]["hidden"])

    def test_program_file_export_import_and_invalid_import_is_atomic(self):
        import os
        from unittest.mock import patch

        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                "server:\n  host: 127.0.0.1\n  port: 8765\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MIXER_CONFIG": str(config_path)}):
                from fastapi.testclient import TestClient
                from app.main import app

                with TestClient(app) as client:
                    created = client.post("/program/items", json={**row("10:00", "Original"), "group": ""})
                    original_id = created.json()["item"]["id"]
                    exported = client.get("/program/export")
                    self.assertEqual(exported.status_code, 200)
                    self.assertIn('filename="programm.json"', exported.headers["content-disposition"])
                    payload = exported.json()
                    self.assertEqual(payload["format"], "mixer-mute-service-program")
                    self.assertEqual(payload["version"], 1)
                    self.assertNotIn("id", payload["items"][0])

                    payload["items"][0]["title"] = "Aus Datei bearbeitet"
                    payload["items"][0]["hidden"] = True
                    imported = client.post("/program/import", json=payload)
                    self.assertEqual(imported.status_code, 200, imported.text)
                    item = imported.json()["items"][0]
                    self.assertEqual(item["title"], "Aus Datei bearbeitet")
                    self.assertTrue(item["hidden"])
                    self.assertNotEqual(item["id"], original_id)
                    imported_id = item["id"]

                    invalid = {
                        **payload,
                        "items": [
                            {**payload["items"][0], "title": "Darf nicht gespeichert werden"},
                            {**payload["items"][0], "time": "25:00"},
                        ],
                    }
                    rejected = client.post("/program/import", json=invalid)
                    self.assertEqual(rejected.status_code, 400)
                    current = client.get("/program/items").json()
                    self.assertEqual(len(current), 1)
                    self.assertEqual(current[0]["id"], imported_id)
                    self.assertEqual(current[0]["title"], "Aus Datei bearbeitet")


class MacroRenameTests(unittest.IsolatedAsyncioTestCase):
    async def test_renamed_macro_group_survives_catalog_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "buttons.json"
            store = CompanionButtonStore(path, COMPANION_BUTTONS)
            self.assertGreaterEqual(await store.rename_group("Predigt", "Predigt neu"), 1)
            data = json.loads(path.read_text(encoding="utf-8"))
            # Pretend an older catalog version: the update rewrites native
            # actions, but must keep the renamed group.
            data["native_catalog_version"] = 3
            path.write_text(json.dumps(data), encoding="utf-8")
            reloaded = CompanionButtonStore(path, COMPANION_BUTTONS)
            macro = await reloaded.get_button("native-overall-predigt")
            self.assertEqual(macro["native_action"]["group"], "Predigt neu")


if __name__ == "__main__":
    unittest.main()
