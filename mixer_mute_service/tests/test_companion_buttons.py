import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.companion_buttons import (
    CAMERA_BUTTON_IDS, COMPANION_BUTTONS, COMPANION_BUTTONS_BY_ID, DEFAULT_CATEGORIES, CompanionButtonStore,
)


class CompanionButtonCatalogTests(unittest.TestCase):
    def test_seed_catalog_has_unique_button_ids(self):
        button_ids = [button["id"] for button in COMPANION_BUTTONS]
        self.assertEqual(len(button_ids), len(set(button_ids)))

    def test_v3_migration_adds_new_buttons_without_restoring_deleted_ones(self):
        live = deepcopy(next(
            button for button in COMPANION_BUTTONS if button["id"] == "native-obs-cam1-live"
        ))
        live["native_action"]["scene"] = "Old scene"
        stored = {
            "categories": deepcopy(DEFAULT_CATEGORIES),
            "buttons": [live],
            "native_catalog_version": 2,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))

        migrated_ids = {button["id"] for button in migrated["buttons"]}
        migrated_live = next(button for button in migrated["buttons"] if button["id"] == live["id"])
        self.assertEqual(migrated["native_catalog_version"], 8)
        self.assertEqual(migrated_live["native_action"]["scene"], "Cam 1")
        self.assertIn("native-obs-cam1-toggle", migrated_ids)
        self.assertIn("beamer-unhide", migrated_ids)
        self.assertIn("beamer-power-off", migrated_ids)
        self.assertIn("beamer-power-on", migrated_ids)
        self.assertNotIn("native-cam1-preset-1", migrated_ids)
        self.assertEqual(len(migrated_ids), len(migrated["buttons"]))

    def test_v4_migration_keeps_deleted_v3_buttons_deleted(self):
        macro = deepcopy(next(
            button for button in COMPANION_BUTTONS if button["id"] == "native-overall-predigt"
        ))
        macro["native_action"] = {"type": "group-macro", "group": "Predigt",
                                  "buttons": ["native-artnet-zone-left-toggle"]}
        stored = {"categories": deepcopy(DEFAULT_CATEGORIES), "buttons": [macro],
                  "native_catalog_version": 3}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(migrated["native_catalog_version"], 8)
        self.assertEqual([b["id"] for b in migrated["buttons"]], [
            "native-overall-predigt", "beamer-unhide", "beamer-power-off", "beamer-power-on",
        ])
        action = migrated["buttons"][0]["native_action"]
        self.assertEqual(action["light_zones_on"], ["left", "center-left", "center-right", "right"])
        self.assertNotIn("native-artnet-zone-left-toggle", action["buttons"])

    def test_v4_migration_updates_hide_and_adds_unhide(self):
        hide = deepcopy(next(button for button in COMPANION_BUTTONS if button["id"] == "beamer-hide"))
        hide["native_action"] = {"type": "beamer-script", "script": "beamer-hide"}
        stored = {"categories": deepcopy(DEFAULT_CATEGORIES), "buttons": [hide], "native_catalog_version": 4}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))

        by_id = {button["id"]: button for button in migrated["buttons"]}
        self.assertEqual(migrated["native_catalog_version"], 8)
        self.assertEqual(by_id["beamer-hide"]["native_action"], {"type": "beamer-set", "hidden": True})
        self.assertEqual(by_id["beamer-unhide"]["native_action"], {"type": "beamer-set", "hidden": False})

    def test_v5_migration_updates_freeze_and_adds_power_controls(self):
        freeze = deepcopy(next(button for button in COMPANION_BUTTONS if button["id"] == "beamer-freeze"))
        freeze["native_action"] = {"type": "beamer-script", "script": "beamer-freeze"}
        stored = {"categories": deepcopy(DEFAULT_CATEGORIES), "buttons": [freeze], "native_catalog_version": 5}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))

        by_id = {button["id"]: button for button in migrated["buttons"]}
        self.assertEqual(migrated["native_catalog_version"], 8)
        self.assertEqual(by_id["beamer-freeze"]["native_action"], {"type": "beamer-command", "command": "freeze"})
        self.assertEqual(by_id["beamer-power-off"]["native_action"]["command"], "power-off")
        self.assertEqual(by_id["beamer-power-on"]["native_action"]["command"], "power-on")

    def test_fresh_store_has_no_camera_keys(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "buttons.json"
            store = CompanionButtonStore(path, COMPANION_BUTTONS)
            stored = json.loads(path.read_text(encoding="utf-8"))
            camera_buttons = asyncio.run(store.camera_buttons())

        self.assertEqual(stored["native_catalog_version"], 8)
        self.assertFalse(CAMERA_BUTTON_IDS & {button["id"] for button in stored["buttons"]})
        self.assertNotIn("cameras", {category["id"] for category in stored["categories"]})
        self.assertIn("native-overall-predigt", {button["id"] for button in stored["buttons"]})
        self.assertEqual({button["id"] for button in camera_buttons}, CAMERA_BUTTON_IDS)

    def test_v7_migration_removes_camera_keys_and_keeps_macros(self):
        legacy_categories = deepcopy(DEFAULT_CATEGORIES) + [{"id": "cameras", "name": "Kameras", "visible": True}]
        stored_buttons = deepcopy(COMPANION_BUTTONS)
        by_id = {button["id"]: button for button in stored_buttons}
        by_id["native-cam1-preset-1"]["label"] = "CAM1 Kanzel"   # renamed by the user
        by_id["native-cam2-zoom-in"]["category_id"] = "obs"     # moved by the user
        macro_before = deepcopy(by_id["native-overall-gebet"])
        stored = {"categories": legacy_categories, "buttons": stored_buttons, "native_catalog_version": 6}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            store = CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))
            camera_buttons = {button["id"]: button for button in asyncio.run(store.camera_buttons())}
            reloaded = asyncio.run(CompanionButtonStore(path, COMPANION_BUTTONS).camera_buttons())

        migrated_ids = [button["id"] for button in migrated["buttons"]]
        self.assertEqual(migrated["native_catalog_version"], 8)
        self.assertFalse(CAMERA_BUTTON_IDS & set(migrated_ids))
        self.assertNotIn("cameras", {category["id"] for category in migrated["categories"]})
        macro = next(button for button in migrated["buttons"] if button["id"] == "native-overall-gebet")
        self.assertEqual(macro, macro_before)
        self.assertIn("native-cam1-preset-prayer", macro["native_action"]["buttons"])
        # Every macro still links catalog camera actions that exist.
        for button in migrated["buttons"]:
            for linked_id in button.get("native_action", {}).get("buttons", []):
                self.assertIn(linked_id, COMPANION_BUTTONS_BY_ID)
        # The camera section keeps the user's label.
        self.assertEqual(migrated["camera_labels"], {"native-cam1-preset-1": "CAM1 Kanzel"})
        self.assertEqual(camera_buttons["native-cam1-preset-1"]["label"], "CAM1 Kanzel")
        self.assertEqual(camera_buttons["native-cam2-preset-1"]["label"], "CAM2 Pult nah")
        self.assertEqual({b["id"]: b["label"] for b in reloaded}["native-cam1-preset-1"], "CAM1 Kanzel")

    def test_v7_migration_keeps_cameras_category_with_user_keys(self):
        legacy_categories = deepcopy(DEFAULT_CATEGORIES) + [{"id": "cameras", "name": "Kameras", "visible": True}]
        preset = deepcopy(COMPANION_BUTTONS_BY_ID["native-cam1-preset-2"])
        own = {"id": "user-abc", "label": "Mein Preset", "category_id": "cameras",
               "kind": "companion", "target_button_id": "native-cam1-preset-2"}
        stored = {"categories": legacy_categories, "buttons": [preset, own], "native_catalog_version": 6}

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(list(Path(temp_dir).glob("*.corrupt*")), [])

        self.assertEqual(migrated["buttons"], [own])
        self.assertIn("cameras", {category["id"] for category in migrated["categories"]})

    def test_macros_link_only_existing_native_actions(self):
        by_id = {button["id"]: button for button in COMPANION_BUTTONS}
        for button in COMPANION_BUTTONS:
            action = button.get("native_action", {})
            if action.get("type") != "group-macro":
                continue
            for linked_id in action["buttons"]:
                self.assertEqual(by_id[linked_id]["kind"], "native", linked_id)
                self.assertNotIn("toggle", by_id[linked_id]["native_action"]["type"], linked_id)


if __name__ == "__main__":
    unittest.main()