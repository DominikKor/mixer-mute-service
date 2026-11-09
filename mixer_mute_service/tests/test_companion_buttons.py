import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from app.companion_buttons import COMPANION_BUTTONS, DEFAULT_CATEGORIES, CompanionButtonStore


class CompanionButtonCatalogTests(unittest.TestCase):
    def test_seed_catalog_has_unique_button_ids(self):
        button_ids = [button["id"] for button in COMPANION_BUTTONS]
        self.assertEqual(len(button_ids), len(set(button_ids)))

    def test_v3_migration_adds_new_buttons_without_restoring_deleted_ones(self):
        prayer = deepcopy(next(
            button for button in COMPANION_BUTTONS if button["id"] == "native-cam1-preset-prayer"
        ))
        prayer["native_action"]["preset"] = 6
        stored = {
            "categories": deepcopy(DEFAULT_CATEGORIES),
            "buttons": [prayer],
            "native_catalog_version": 2,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))

        migrated_ids = {button["id"] for button in migrated["buttons"]}
        migrated_prayer = next(button for button in migrated["buttons"] if button["id"] == prayer["id"])
        self.assertEqual(migrated["native_catalog_version"], 6)
        self.assertEqual(migrated_prayer["native_action"]["preset"], 8)
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

        self.assertEqual(migrated["native_catalog_version"], 6)
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
        self.assertEqual(migrated["native_catalog_version"], 6)
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
        self.assertEqual(migrated["native_catalog_version"], 6)
        self.assertEqual(by_id["beamer-freeze"]["native_action"], {"type": "beamer-command", "command": "freeze"})
        self.assertEqual(by_id["beamer-power-off"]["native_action"]["command"], "power-off")
        self.assertEqual(by_id["beamer-power-on"]["native_action"]["command"], "power-on")

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