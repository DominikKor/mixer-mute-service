"""Catalog v10: the duplicate OBS keys leave Steuerung (old IDs keep working),
the transition time keys, and the OBS status behind the live colours.
OBS is faked."""

import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest.mock import patch

from app import native_controls
from app.companion_buttons import (
    BUTTON_ALIASES, COMPANION_BUTTONS, COMPANION_BUTTONS_BY_ID, DEFAULT_CATEGORIES, CompanionButtonStore,
)
from app.native_controls import NativeControlError, NativeControlService, obs_status_blocking
from app.obs_status import ERROR_TTL_SECONDS, ObsStatus

from tests.helpers import app_client

OLD = {
    "cam2-live-top": ("Cam 2 Live", {"type": "obs-live", "scene": "Cam 2", "source": "Cam2"}),
    "cam2-live-bottom": ("Cam 2 Live", {"type": "obs-live", "scene": "Cam 2", "source": "Cam2"}),
    "pc2-live-top-left": ("PC2 Live", {"type": "obs-live", "scene": "PC2", "source": "Capture_PC2"}),
    "start": ("Start", {"type": "obs-scene", "scene": "Willkommen (neu)"}),
    "beamer-live": ("Beamer Live", {"type": "obs-live", "scene": "BeamerVonVorne", "source": "Beamer"}),
}


def old_key(button_id, label=None, category="obs"):
    default_label, action = OLD[button_id]
    return {"id": button_id, "label": label or default_label, "kind": "native",
            "category_id": category, "native_action": deepcopy(action)}


class DuplicateKeyTests(unittest.TestCase):
    def _migrate(self, buttons, version=9):
        stored = {"categories": deepcopy(DEFAULT_CATEGORIES), "buttons": buttons, "native_catalog_version": version}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(list(Path(folder).glob("*.corrupt*")), [])
        return migrated

    def test_catalog_has_each_obs_key_once(self):
        obs = [b for b in COMPANION_BUTTONS if b["category_id"] == "obs"]
        actions = [json.dumps(b["native_action"], sort_keys=True) for b in obs]
        self.assertEqual(len(actions), len(set(actions)))
        self.assertFalse(set(BUTTON_ALIASES) & set(COMPANION_BUTTONS_BY_ID))
        self.assertIn("beamer-live", COMPANION_BUTTONS_BY_ID, "no native twin: stays")

    def test_old_keys_leave_when_the_native_key_is_there(self):
        native = deepcopy(COMPANION_BUTTONS_BY_ID["native-obs-cam2-live"])
        own = {"id": "user-1", "label": "Zwei", "category_id": "obs", "kind": "companion",
               "target_button_id": "cam2-live-top"}
        migrated = self._migrate([old_key("cam2-live-top"), native, old_key("cam2-live-bottom"),
                                  old_key("beamer-live"), own])
        ids = [b["id"] for b in migrated["buttons"]]
        self.assertEqual(ids[:3], ["native-obs-cam2-live", "beamer-live", "user-1"])
        self.assertEqual(migrated["buttons"][2]["target_button_id"], "native-obs-cam2-live")
        self.assertIn("native-obs-transition-slower", ids)
        self.assertEqual(migrated["native_catalog_version"], 11)

    def test_renamed_or_moved_old_key_stays_as_an_own_key(self):
        categories = deepcopy(DEFAULT_CATEGORIES) + [{"id": "category-gd", "name": "Gottesdienst", "visible": True}]
        stored = {"categories": categories, "native_catalog_version": 9, "buttons": [
            deepcopy(COMPANION_BUTTONS_BY_ID["native-obs-end"]),
            deepcopy(COMPANION_BUTTONS_BY_ID["native-obs-cam2-live"]),
            deepcopy(COMPANION_BUTTONS_BY_ID["native-obs-start"]),
            {**old_key("start"), "id": "ende", "label": "Schluss", "category_id": "category-gd",
             "native_action": {"type": "obs-scene", "scene": "Ende (neu)"}},   # renamed and moved
            old_key("cam2-live-top", label="Kamera hinten"),                     # renamed only
            old_key("cam2-live-bottom", category="beamer"),                      # moved only
            old_key("start"),                                                    # untouched: goes
        ]}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            store = CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))
            own = [b for b in migrated["buttons"] if b["id"].startswith("user-")]
            self.assertEqual([(b["label"], b["category_id"], b["kind"], b["target_button_id"]) for b in own], [
                ("Schluss", "category-gd", "companion", "native-obs-end"),
                ("Kamera hinten", "obs", "companion", "native-obs-cam2-live"),
                ("Cam 2 Live", "beamer", "companion", "native-obs-cam2-live"),
            ])
            ids = {b["id"] for b in migrated["buttons"]}
            self.assertFalse(ids & set(BUTTON_ALIASES), "no old ID left in Steuerung")
            self.assertEqual(len([b for b in migrated["buttons"] if b["label"] == "Start"]), 1)
            # Reloading keeps them (no second migration, no new IDs).
            self.assertEqual(asyncio.run(CompanionButtonStore(path, COMPANION_BUTTONS).snapshot())["buttons"],
                             asyncio.run(store.snapshot())["buttons"])

    def test_an_old_key_takes_the_place_of_a_deleted_native_key(self):
        migrated = self._migrate([old_key("pc2-live-top-left", label="Beamer-PC", category="beamer"),
                                  old_key("start")])
        by_id = {b["id"]: b for b in migrated["buttons"]}
        self.assertNotIn("pc2-live-top-left", by_id)
        self.assertEqual(by_id["native-obs-pc2-live"]["label"], "Beamer-PC")
        self.assertEqual(by_id["native-obs-pc2-live"]["category_id"], "beamer")
        self.assertEqual(by_id["native-obs-pc2-live"]["native_action"],
                         COMPANION_BUTTONS_BY_ID["native-obs-pc2-live"]["native_action"])
        self.assertEqual(by_id["native-obs-start"]["label"], "Start")


class OldIdsStillRunTests(unittest.TestCase):
    def test_program_rows_and_own_keys_with_old_ids_run_the_native_key(self):
        with app_client() as (client, _folder):
            pressed = []

            async def press(action):
                pressed.append(action)
                return {"ok": True}

            with patch.object(client.app.state.native_controls, "press", side_effect=press):
                self.assertEqual(client.post("/companion/buttons/cam2-live-bottom/press").status_code, 200)
                own = client.post("/companion/buttons", json={
                    "label": "Alt", "category_id": "obs", "kind": "companion", "target_button_id": "start"})
                self.assertEqual(own.status_code, 200, own.text)
                self.assertEqual(own.json()["button"]["target_button_id"], "native-obs-start")
                self.assertEqual(client.post("/companion/buttons/ende/press").status_code, 200)
                # The native key deleted from Steuerung: the old ID is gone with it.
                client.post("/companion/buttons/native-obs-end/delete")
                self.assertEqual(client.post("/companion/buttons/ende/press").status_code, 404)
            self.assertEqual(pressed, [COMPANION_BUTTONS_BY_ID["native-obs-cam2-live"]["native_action"],
                                       COMPANION_BUTTONS_BY_ID["native-obs-end"]["native_action"]])
            row = client.post("/program/items", json={"time": "10:00", "title": "Lied", "group": "",
                                                      "playback": "pc2-live-top-right"})
            self.assertEqual(row.status_code, 200, row.text)
            self.assertEqual(client.get("/companion/buttons").json()["button_aliases"]["cam1-live"],
                             "native-obs-cam1-live")


class TransitionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.controls = NativeControlService(Path(folder.name) / "state.json")

    async def _adjust(self, delta, transition):
        sent = []

        def obs(host, port, request_type, data=None):
            if request_type == "GetCurrentSceneTransition":
                return transition
            sent.append((request_type, data))
            return {}

        with patch("app.native_controls._obs_request", side_effect=obs):
            result = await self.controls.press({"type": "obs-transition-adjust", "delta": delta})
        return result, sent

    async def test_steps_and_limits(self):
        result, sent = await self._adjust(50, {"transitionName": "Fade", "transitionDuration": 400})
        self.assertEqual(result["duration_ms"], 450)
        self.assertEqual(sent, [("SetCurrentSceneTransitionDuration", {"transitionDuration": 450})])
        result, _ = await self._adjust(-50, {"transitionName": "Fade", "transitionDuration": 60})
        self.assertEqual(result["duration_ms"], 50, "OBS takes no less than 50 ms")

    async def test_cut_has_no_duration(self):
        with self.assertRaises(NativeControlError):
            await self._adjust(50, {"transitionName": "Cut", "transitionFixed": True, "transitionDuration": None})

    def test_catalog_keys(self):
        for key, delta in (("native-obs-transition-slower", 50), ("native-obs-transition-faster", -50)):
            self.assertEqual(COMPANION_BUTTONS_BY_ID[key]["native_action"], {"type": "obs-transition-adjust", "delta": delta})


class ObsStatusTests(unittest.IsolatedAsyncioTestCase):
    def test_status_over_one_connection(self):
        answers = [{"currentProgramSceneName": "Cam 1"}, {"studioModeEnabled": True},
                   {"currentPreviewSceneName": "Cam 2"},
                   {"transitionName": "Fade", "transitionFixed": False, "transitionDuration": 400}]
        with patch("app.native_controls._obs_batch", return_value=answers) as batch:
            status = obs_status_blocking()
        batch.assert_called_once()
        self.assertEqual(status, {"program": "Cam 1", "preview": "Cam 2", "studio_mode": True,
                                  "transition": {"name": "Fade", "duration_ms": 400}, "overlays": []})

    def test_without_studio_mode_there_is_no_preview(self):
        answers = [{"currentProgramSceneName": "PC2"}, {"studioModeEnabled": False}, None,
                   {"transitionName": "Cut", "transitionFixed": True, "transitionDuration": None}]
        with patch("app.native_controls._obs_batch", return_value=answers):
            status = obs_status_blocking()
        self.assertIsNone(status["preview"])
        self.assertIsNone(status["transition"]["duration_ms"])

    async def test_cache_shared_and_errors_back_off(self):
        now = [100.0]
        calls = []

        def fetch():
            calls.append(now[0])
            if len(calls) == 2:
                raise NativeControlError("OBS WebSocket unavailable")
            return {"program": "Cam 1", "preview": None}

        status = ObsStatus(fetch, clock=lambda: now[0])
        first = await asyncio.gather(*(status.get() for _ in range(5)))
        self.assertEqual(len(calls), 1, "five pages, one OBS query")
        self.assertTrue(all(answer["program"] == "Cam 1" for answer in first))
        now[0] += 1.5
        failed = await status.get()
        self.assertEqual((failed["ok"], failed["program"]), (False, None))
        now[0] += ERROR_TTL_SECONDS - 1
        await status.get()
        self.assertEqual(len(calls), 2, "OBS down: not asked again at once")
        status.invalidate()
        self.assertTrue((await status.get())["ok"])
        self.assertEqual(len(calls), 3)

    def test_endpoint(self):
        with app_client() as (client, _folder):
            client.app.state.obs_status = ObsStatus(lambda: {"program": "Cam 2", "preview": "Cam 1"})
            data = client.get("/obs/status").json()
            self.assertEqual((data["ok"], data["program"], data["preview"]), (True, "Cam 2", "Cam 1"))
            client.app.state.obs_status = ObsStatus(native_controls.obs_status_blocking)
            with patch("app.native_controls._obs_batch", side_effect=NativeControlError("OBS WebSocket unavailable")):
                data = client.get("/obs/status").json()
            self.assertEqual((data["ok"], data["reason"]), (False, "OBS WebSocket unavailable"))


if __name__ == "__main__":
    unittest.main()
