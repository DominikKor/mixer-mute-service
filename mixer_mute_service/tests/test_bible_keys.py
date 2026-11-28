"""The Bibel keys (Companion page 7, "Bibel Cam1/Cam2 Beamer"): toggle the
Bible text ("BibleShow") in the camera's OBS scene, hide the projector while
it shows, put that camera on air and the other one in the preview; their
live state comes from GET /obs/status. OBS and the projector are faked."""

import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.companion_buttons import (
    COMPANION_BUTTONS, COMPANION_BUTTONS_BY_ID, DEFAULT_CATEGORIES, OVERLAY_SOURCES, CompanionButtonStore,
)
from app.native_controls import NativeControlService, obs_status_blocking


class FakeObs:
    def __init__(self, studio=True):
        self.studio = studio
        self.program, self.preview = "Cam 2", "Cam 1"
        self.bible = {"Cam 1": False, "Cam 2": False}
        self.log = []

    def request(self, host, port, request_type, data=None):
        (answer,) = self.batch(host, port, [(request_type, data)])
        if answer is None:
            from app.native_controls import NativeControlError
            raise NativeControlError(f"OBS rejected {request_type}")
        return answer

    def batch(self, host, port, requests):
        answers = []
        for request_type, data in requests:
            data = data or {}
            self.log.append((request_type, data.get("sceneName")))
            if request_type == "GetSceneItemList":
                scene = data["sceneName"]
                items = [{"sceneItemId": 1, "sourceName": scene.replace(" ", ""), "sceneItemEnabled": True}]
                if scene in self.bible:
                    items.append({"sceneItemId": 7, "sourceName": "BibleShow", "sceneItemEnabled": self.bible[scene]})
                answers.append({"sceneItems": items})
            elif request_type == "SetSceneItemEnabled":
                self.bible[data["sceneName"]] = data["sceneItemEnabled"]
                answers.append({})
            elif request_type == "SetCurrentProgramScene":
                self.program = data["sceneName"]
                answers.append({})
            elif request_type == "SetCurrentPreviewScene":
                if self.studio:
                    self.preview = data["sceneName"]
                answers.append({} if self.studio else None)  # OBS refuses outside studio mode
            elif request_type == "GetCurrentProgramScene":
                answers.append({"currentProgramSceneName": self.program})
            elif request_type == "GetStudioModeEnabled":
                answers.append({"studioModeEnabled": self.studio})
            elif request_type == "GetCurrentPreviewScene":
                answers.append({"currentPreviewSceneName": self.preview} if self.studio else None)
            elif request_type == "GetCurrentSceneTransition":
                answers.append({"transitionName": "Fade", "transitionDuration": 400})
            else:
                raise AssertionError(request_type)
        return answers


class BibleKeyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.controls = NativeControlService(Path(folder.name) / "state.json")

    async def _press(self, obs, key):
        with patch("app.native_controls._obs_request", side_effect=obs.request), \
                patch("app.native_controls._obs_batch", side_effect=obs.batch), \
                patch.object(self.controls, "_toggle_beamer", new_callable=AsyncMock) as beamer:
            result = await self.controls.press(COMPANION_BUTTONS_BY_ID[key]["native_action"])
        return result, beamer.await_count

    async def test_first_press_shows_second_hides_as_in_companion(self):
        obs = FakeObs()
        await self.controls.set_beamer_state(False)
        shown, toggles = await self._press(obs, "native-obs-bible-cam1")
        self.assertTrue(shown["shown"])
        self.assertEqual((obs.bible["Cam 1"], obs.program, obs.preview), (True, "Cam 1", "Cam 2"))
        self.assertTrue(await self.controls.get_beamer_state(), "projector hidden while the text shows")
        # Companion's order: text, projector, program, then the preview.
        sets = [entry for entry in obs.log if entry[0].startswith("Set")]
        self.assertEqual(sets, [("SetSceneItemEnabled", "Cam 1"), ("SetCurrentProgramScene", "Cam 1"),
                                ("SetCurrentPreviewScene", "Cam 2")])
        hidden, _ = await self._press(obs, "native-obs-bible-cam1")
        self.assertFalse(hidden["shown"])
        self.assertFalse(obs.bible["Cam 1"])
        self.assertFalse(await self.controls.get_beamer_state(), "projector back with the text gone")
        self.assertEqual(obs.program, "Cam 1")

    async def test_cam2_key_and_no_studio_mode(self):
        obs = FakeObs(studio=False)
        await self.controls.set_beamer_state(False)
        result, _ = await self._press(obs, "native-obs-bible-cam2")
        self.assertTrue(result["ok"], "the refused preview does not fail the key")
        self.assertEqual((obs.bible["Cam 2"], obs.program), (True, "Cam 2"))

    def test_status_reports_whether_the_text_shows(self):
        obs = FakeObs()
        obs.bible["Cam 2"] = True
        with patch("app.native_controls._obs_batch", side_effect=obs.batch):
            status = obs_status_blocking(OVERLAY_SOURCES)
            missing = obs_status_blocking([("Titel", "BibleShow")])
        self.assertEqual(OVERLAY_SOURCES, [("Cam 1", "BibleShow"), ("Cam 2", "BibleShow")])
        self.assertEqual(status["overlays"], [{"scene": "Cam 1", "source": "BibleShow", "shown": False},
                                              {"scene": "Cam 2", "source": "BibleShow", "shown": True}])
        self.assertEqual(status["program"], "Cam 2")
        self.assertEqual(missing["overlays"], [{"scene": "Titel", "source": "BibleShow", "shown": None}])


class CatalogV11Tests(unittest.TestCase):
    def test_stored_bible_keys_get_the_preview_and_keep_label_and_category(self):
        categories = deepcopy(DEFAULT_CATEGORIES) + [{"id": "category-gd", "name": "Gottesdienst", "visible": True}]
        old = deepcopy(COMPANION_BUTTONS_BY_ID["native-obs-bible-cam1"])
        old["native_action"].pop("preview_after")
        old.update(label="Bibeltext anzeigen", category_id="category-gd")
        own = {"id": "user-1", "label": "Bibel 2", "category_id": "obs", "kind": "companion",
               "target_button_id": "native-obs-bible-cam2"}
        stored = {"categories": categories, "buttons": [old, own], "native_catalog_version": 10}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))
        by_id = {b["id"]: b for b in migrated["buttons"]}
        self.assertEqual(migrated["native_catalog_version"], 12)
        self.assertEqual(by_id["native-obs-bible-cam1"]["native_action"]["preview_after"], "Cam 2")
        self.assertEqual((by_id["native-obs-bible-cam1"]["label"], by_id["native-obs-bible-cam1"]["category_id"]),
                         ("Bibeltext anzeigen", "category-gd"))
        self.assertEqual(by_id["user-1"], own)
        self.assertNotIn("native-obs-bible-cam2", by_id, "a key the user deleted does not come back")
        self.assertIn("category-gd", {c["id"] for c in migrated["categories"]})


if __name__ == "__main__":
    unittest.main()
