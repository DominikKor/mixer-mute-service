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
            store = CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))

        migrated_ids = {button["id"] for button in migrated["buttons"]}
        migrated_prayer = next(button for button in migrated["buttons"] if button["id"] == prayer["id"])
        self.assertEqual(migrated["native_catalog_version"], 3)
        self.assertEqual(migrated_prayer["native_action"]["preset"], 8)
        self.assertIn("native-obs-cam1-toggle", migrated_ids)
        self.assertNotIn("native-cam1-preset-1", migrated_ids)
        self.assertEqual(len(migrated_ids), len(migrated["buttons"]))


if __name__ == "__main__":
    unittest.main()