"""The Overall keys are gone (catalog v12, app/companion_buttons.py): the
migration removes them, the own keys that ran one and the empty Overall
category; stored program rows, templates and shortcuts drop the steps that
ran one (app/row_files.py remove_overall_references); the program file
import drops them with a warning. Nothing else changes, and no mute path
changes: channel and group keys still go to the controller as before."""

import asyncio
import json
import os
import tempfile
import unittest
from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import actions
from app.companion_buttons import (
    COMPANION_BUTTONS, COMPANION_BUTTONS_BY_ID, DEFAULT_CATEGORIES, CompanionButtonStore, is_overall_id,
)
from app.program_items import ProgramItemStore
from app.program_templates import ProgramTemplateStore
from app.row_files import drop_key_steps, remove_overall_references
from app.shortcuts import ShortcutStore

from tests.helpers import no_mediamtx, safe_config_yaml


def overall(key, label, group, category="overall"):
    """An Overall key as catalog v11 stored it."""
    return {"id": f"native-overall-{key}", "label": label, "kind": "native", "category_id": category,
            "native_action": {"type": "group-macro", "group": group,
                              "stage": {"on": ["left", "right"], "off": ["center-left", "center-right"]},
                              "buttons": [], "cameras": {"cam1": 1, "cam2": 1}, "camera_wait_seconds": 4.0}}


OWN_CATEGORY = {"id": "category-gd", "name": "Gottesdienst", "visible": True}
# Own keys: one ran an Overall key, the others do something else.
RUNS_OVERALL = {"id": "user-aaaaaaaaaaaa", "label": "Predigt jetzt", "category_id": "category-gd",
                "kind": "companion", "target_button_id": "native-overall-predigt"}
GROUP_KEY = {"id": "user-bbbbbbbbbbbb", "label": "Lobpreis an", "category_id": "category-gd",
             "kind": "group", "group": "Lobpreis", "operation": "unmute"}
CHANNEL_KEY = {"id": "user-cccccccccccc", "label": "Pult stumm", "category_id": "obs",
               "kind": "channel", "channel": 1, "operation": "mute"}
OBS_SHORTCUT = {"id": "user-dddddddddddd", "label": "Kanzel live", "category_id": "category-gd",
                "kind": "companion", "target_button_id": "native-obs-cam1-live"}


def catalog_buttons():
    """The keys a fresh store has (the current catalog in Steuerung)."""
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "buttons.json"
        CompanionButtonStore(path, COMPANION_BUTTONS)
        return json.loads(path.read_text(encoding="utf-8"))["buttons"]


def v11_store(extra_overall_category_keys=()):
    """companion_buttons.json as v11 left it: the catalog, three Overall keys
    (one renamed and moved by the user), own keys."""
    categories = deepcopy(DEFAULT_CATEGORIES) + [{"id": "overall", "name": "Overall", "visible": True}, OWN_CATEGORY]
    buttons = catalog_buttons()
    buttons.insert(2, overall("predigt", "Predigt", "Predigt"))
    buttons.append(overall("gebet", "Gebet!", "Gebet", category="category-gd"))  # renamed and moved
    buttons.append(overall("ende", "Ende", "Klavier"))
    buttons += [deepcopy(RUNS_OVERALL), deepcopy(GROUP_KEY), deepcopy(CHANNEL_KEY), deepcopy(OBS_SHORTCUT)]
    buttons += [deepcopy(b) for b in extra_overall_category_keys]
    return {"categories": categories, "buttons": buttons, "native_catalog_version": 11}


def migrate(stored):
    with tempfile.TemporaryDirectory() as folder:
        path = Path(folder) / "buttons.json"
        path.write_text(json.dumps(stored), encoding="utf-8")
        store = CompanionButtonStore(path, COMPANION_BUTTONS)
        migrated = json.loads(path.read_text(encoding="utf-8"))
        again = CompanionButtonStore(path, COMPANION_BUTTONS)
        assert json.loads(path.read_text(encoding="utf-8")) == migrated, "a second start changes nothing"
        assert list(Path(folder).glob("*.corrupt*")) == []
        return store, again, migrated


class CatalogTests(unittest.TestCase):
    def test_catalog_has_no_overall_keys(self):
        self.assertFalse([b["id"] for b in COMPANION_BUTTONS if is_overall_id(b["id"])])
        self.assertFalse([b["id"] for b in COMPANION_BUTTONS
                          if b.get("native_action", {}).get("type") == "group-macro"])
        self.assertNotIn("overall", {c["id"] for c in DEFAULT_CATEGORIES})

    def test_fresh_store_has_no_overall_category(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "buttons.json"
            store = CompanionButtonStore(path, COMPANION_BUTTONS)
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(stored["native_catalog_version"], 12)
        self.assertEqual([c["id"] for c in stored["categories"]], ["beamer", "obs"])
        self.assertEqual(store.removed_overall_keys, [])


class MigrationTests(unittest.TestCase):
    def test_overall_keys_own_keys_running_one_and_the_empty_category_go(self):
        stored = v11_store()
        with self.assertLogs("mixer.companion_buttons", "INFO") as logs:
            store, again, migrated = migrate(stored)
        self.assertEqual(migrated["native_catalog_version"], 12)
        removed = {"native-overall-predigt", "native-overall-gebet", "native-overall-ende", RUNS_OVERALL["id"]}
        # Everything else stays exactly as it was, in its order.
        self.assertEqual(migrated["buttons"], [b for b in stored["buttons"] if b["id"] not in removed])
        self.assertEqual(migrated["categories"], [c for c in stored["categories"] if c["id"] != "overall"])
        self.assertEqual(store.removed_overall_keys, [RUNS_OVERALL["id"]])
        self.assertEqual(again.removed_overall_keys, [], "only the start that migrated knows them")
        self.assertTrue(any("Predigt jetzt" in line and "native-overall-predigt" in line and "WARNING" in line
                            for line in logs.output), logs.output)

    def test_overall_category_with_other_own_keys_stays(self):
        own = {"id": "user-eeeeeeeeeeee", "label": "Beamer weg", "category_id": "overall",
               "kind": "companion", "target_button_id": "beamer-hide"}
        stored = v11_store([own])
        _store, _again, migrated = migrate(stored)
        self.assertIn("overall", {c["id"] for c in migrated["categories"]})
        self.assertEqual([b for b in migrated["buttons"] if b["category_id"] == "overall"], [own])
        self.assertFalse([b for b in migrated["buttons"] if is_overall_id(b["id"])])

    def test_old_store_versions_lose_their_overall_keys_too(self):
        old = overall("predigt", "Predigt", "Predigt")
        for version in (3, 8, 9):
            with self.subTest(version=version):
                _store, _again, migrated = migrate(
                    {"categories": deepcopy(DEFAULT_CATEGORIES) + [{"id": "overall", "name": "Overall", "visible": True}],
                     "buttons": [deepcopy(old)], "native_catalog_version": version})
                self.assertFalse([b for b in migrated["buttons"] if is_overall_id(b["id"])])
                self.assertNotIn("overall", {c["id"] for c in migrated["categories"]})

    def test_current_store_is_left_alone(self):
        stored = {"categories": deepcopy(DEFAULT_CATEGORIES) + [OWN_CATEGORY],
                  "buttons": [deepcopy(GROUP_KEY)], "native_catalog_version": 12}
        _store, _again, migrated = migrate(stored)
        self.assertEqual(migrated["buttons"], [GROUP_KEY])


ROW_STEPS = ["key:native-overall-predigt", "qr", "key:native-obs-cam1-live", f"key:{RUNS_OVERALL['id']}", "@song"]
KEPT_STEPS = ["qr", "key:native-obs-cam1-live", "@song"]


def row(title, **fields):
    return {"time": "10:00", "title": title, "person": "", "group": "Predigt", "desktop": list(ROW_STEPS),
            "song": "a.foil", "cam1_preset": 3, "playback_description": "", "translation_description": "",
            "lyrics_description": "", **fields}


class ReferenceTests(unittest.TestCase):
    def test_drop_key_steps_keeps_everything_else(self):
        data = {"playback": "native-overall-gebet", "translation": "native-obs-cam1-live",
                "desktop": list(ROW_STEPS), "group": "Predigt"}
        dropped = drop_key_steps(data, is_overall_id)
        self.assertEqual(dropped, ["native-overall-gebet", "native-overall-predigt"])
        self.assertEqual(data, {"playback": None, "translation": "native-obs-cam1-live", "group": "Predigt",
                                "desktop": ["qr", "key:native-obs-cam1-live", f"key:{RUNS_OVERALL['id']}", "@song"]})
        self.assertEqual(drop_key_steps(data, is_overall_id), [])

    def test_rows_templates_and_shortcuts_drop_the_steps(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / "program_items.json").write_text(json.dumps([
                {"id": "r1", **row("Predigt")},
                {"id": "r2", **row("Lied", desktop=["qr"])},
            ]), encoding="utf-8")
            (folder / "shortcuts.json").write_text(json.dumps([
                {"id": "s1", **row("Abendmahl")},
            ]), encoding="utf-8")
            (folder / "program_templates.json").write_text(json.dumps([
                {"id": "t1", "name": "Sonntag", "rows": [row("Predigt"), row("Segen", desktop=["@song"])]},
            ]), encoding="utf-8")
            state = SimpleNamespace(
                program_items=ProgramItemStore(folder / "program_items.json", {}),
                shortcuts=ShortcutStore(folder / "shortcuts.json"),
                program_templates=ProgramTemplateStore(folder / "program_templates.json", {}),
            )
            before = {
                "program": asyncio.run(state.program_items.list()),
                "shortcuts": asyncio.run(state.shortcuts.list()),
                "templates": asyncio.run(state.program_templates.list()),
            }
            with self.assertLogs("mixer.row_files", "WARNING") as logs:
                summary = asyncio.run(remove_overall_references(state, [RUNS_OVERALL["id"]]))
            self.assertEqual(summary, {"program": 1, "templates": 1, "shortcuts": 1})
            self.assertTrue(any("'Predigt'" in line and "native-overall-predigt" in line for line in logs.output))

            def without_steps(rows):
                return [{k: v for k, v in r.items() if k != "desktop"} for r in rows]

            program = asyncio.run(state.program_items.list())
            self.assertEqual([r["desktop"] for r in program], [KEPT_STEPS, ["qr"]])
            self.assertEqual(without_steps(program), without_steps(before["program"]), "the rest stays")
            shortcuts = asyncio.run(state.shortcuts.list())
            self.assertEqual(shortcuts[0]["desktop"], KEPT_STEPS)
            self.assertEqual(without_steps(shortcuts), without_steps(before["shortcuts"]))
            templates = asyncio.run(state.program_templates.list())
            self.assertEqual([r["desktop"] for r in templates[0]["rows"]], [KEPT_STEPS, ["@song"]])
            self.assertEqual(without_steps(templates[0]["rows"]), without_steps(before["templates"][0]["rows"]))
            # Saved, and a second start changes nothing (the own key is not known any more).
            self.assertEqual(json.loads((folder / "program_items.json").read_text(encoding="utf-8"))[0]["desktop"],
                             KEPT_STEPS)
            self.assertEqual(asyncio.run(remove_overall_references(state)),
                             {"program": 0, "templates": 0, "shortcuts": 0})


CONFIG = ("desktop_display:\n  enabled: true\n  profiles:\n"
          "    qr:\n      title: QR Gemeinde\n      kind: image\n      path: 'C:\\Bilder\\qr.png'\n"
          "groups:\n  Predigt:\n    channels: [1]\n  Lobpreis:\n    channels: [2]\n")


@contextmanager
def service(files=None):
    """The service on a safe config with data files written before it starts."""
    from fastapi.testclient import TestClient

    from app.main import app

    with tempfile.TemporaryDirectory(prefix="mixer_app_") as name:
        folder = Path(name)
        (folder / "config.yaml").write_text(safe_config_yaml(CONFIG), encoding="utf-8")
        for file_name, data in (files or {}).items():
            (folder / file_name).write_text(json.dumps(data), encoding="utf-8")
        with patch.dict(os.environ, {"MIXER_CONFIG": str(folder / "config.yaml")}), no_mediamtx(), \
                TestClient(app) as client:
            yield client, folder


class ServiceTests(unittest.TestCase):
    def test_start_migrates_keys_and_rows(self):
        files = {
            "companion_buttons.json": v11_store(),
            "program_items.json": [{"id": "r1", **row("Predigt", song=None, desktop=list(ROW_STEPS[:-1]))}],
        }
        with service(files) as (client, folder):
            data = client.get("/companion/buttons").json()
            self.assertNotIn("overall", {c["id"] for c in data["categories"]})
            ids = {b["id"] for b in data["buttons"]}
            self.assertFalse({i for i in ids if is_overall_id(i)})
            self.assertNotIn(RUNS_OVERALL["id"], ids)
            self.assertTrue({GROUP_KEY["id"], CHANNEL_KEY["id"], OBS_SHORTCUT["id"]} <= ids)
            self.assertFalse([t for t in data.get("companion_targets", []) if is_overall_id(t.get("id"))])
            items = client.get("/program/items").json()
            self.assertEqual(items[0]["desktop"], ["qr", "key:native-obs-cam1-live"])
            self.assertEqual((items[0]["group"], items[0]["cam1_preset"]), ("Predigt", 3))

    def test_old_overall_id_is_unknown_and_touches_no_mute(self):
        with service() as (client, _folder):
            controller = client.app.state.controller
            calls = []

            async def group_set(group, muted):
                calls.append((group, muted))
                return {"ok": True}

            with patch.object(controller, "group_set", group_set):
                r = client.post("/companion/buttons/native-overall-predigt/press")
                self.assertEqual(r.status_code, 404)
                self.assertEqual(calls, [])
                # A group key runs through the same controller call as before.
                key = client.post("/companion/buttons", json={"label": "Predigt an", "category_id": "obs",
                                                              "kind": "group", "group": "Predigt",
                                                              "operation": "unmute"}).json()["button"]
                r = client.post(f"/companion/buttons/{key['id']}/press")
                self.assertEqual(r.status_code, 200, r.text)
                self.assertEqual(calls, [("Predigt", False)])

    def test_import_drops_overall_steps_with_a_warning(self):
        with service() as (client, _folder):
            # An own key named like the old Overall key must not take its place.
            r = client.post("/companion/buttons", json={"label": "Predigt", "category_id": "obs", "kind": "group",
                                                        "group": "Lobpreis", "operation": "unmute"})
            self.assertEqual(r.status_code, 200, r.text)
            exported = client.get("/program/export").json()
            imported = {"time": "10:00", "title": "Predigt", "group": "",
                        "desktop": ["key:native-overall-predigt", "qr", "key:native-obs-cam1-live"],
                        "desktop_names": ["Predigt", "QR Gemeinde", "Cam 1 Live"],
                        "playback": "native-overall-gebet", "playback_name": "Gebet"}
            r = client.post("/program/import", json={**exported, "items": [imported]})
            self.assertEqual(r.status_code, 200, r.text)
            body = r.json()
            self.assertEqual(body["items"][0]["desktop"], ["qr", "key:native-obs-cam1-live"])
            reasons = [w["reason"] for w in body["warnings"]]
            self.assertEqual(len(reasons), 2, reasons)
            self.assertTrue(all("Overall-Taste" in reason and "Verweis entfernt" in reason for reason in reasons))
            self.assertEqual({w["name"] for w in body["warnings"]}, {"Predigt", "Gebet"})


class SharedBehaviourTests(unittest.IsolatedAsyncioTestCase):
    """What the Overall code shared with other keys still works."""

    async def test_obs_program_keys_cancel_a_waiting_camera_cut(self):
        cancelled = []

        async def press(action):
            return {"ok": True}

        request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            native_controls=SimpleNamespace(press=press),
            camera_program=SimpleNamespace(cancel=lambda: cancelled.append(1)),
        )))
        for button_id in ("native-obs-picture", "native-obs-cam1-live", "native-obs-bible-cam2"):
            await actions.press_native(request, button_id, COMPANION_BUTTONS_BY_ID[button_id]["native_action"])
        self.assertEqual(len(cancelled), 3)
        for button_id in ("native-obs-cam1-toggle", "beamer-hide"):
            await actions.press_native(request, button_id, COMPANION_BUTTONS_BY_ID[button_id]["native_action"])
        self.assertEqual(len(cancelled), 3, "keys that do not change the program leave it alone")

    def test_only_channel_and_group_keys_switch_mutes(self):
        self.assertTrue(actions.switches_mutes(GROUP_KEY))
        self.assertTrue(actions.switches_mutes(CHANNEL_KEY))
        self.assertFalse(actions.switches_mutes(OBS_SHORTCUT))
        self.assertFalse(actions.switches_mutes(COMPANION_BUTTONS_BY_ID["native-obs-cam1-live"]))

    def test_room_light_keys_leave_the_technik_circuit_alone(self):
        for key in ("native-shelly-all-on", "native-shelly-all-off"):
            self.assertNotIn("light-tech", COMPANION_BUTTONS_BY_ID[key]["native_action"]["targets"])
        self.assertEqual(sorted(COMPANION_BUTTONS_BY_ID["native-shelly-center-on"]["native_action"]["targets"]),
                         ["light-center-left", "light-center-right", "light-front"])


if __name__ == "__main__":
    unittest.main()
