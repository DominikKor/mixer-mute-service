"""VISCA presets by number in the camera section: recall any number 1 to 16
(also one without a name), save the current position with number and name,
and the question before a named number is overwritten. VISCA is mocked, no
camera is contacted."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import app.native_controls as native_mod
from app.companion_buttons import (
    CAMERA_PRESET_MAX, CAMERA_PRESET_MIN, COMPANION_BUTTONS, CompanionButtonStore,
)
from app.native_controls import NativeControlError, NativeControlService
from tests.helpers import app_client

CAM1 = ("192.168.5.21", 5678)
CAM2 = ("192.168.5.22", 5678)


def recall(preset):
    return bytes((0x81, 0x01, 0x04, 0x3F, 0x02, preset, 0xFF))


def store(preset):
    return bytes((0x81, 0x01, 0x04, 0x3F, 0x01, preset, 0xFF))


class ViscaPresetCommandTests(unittest.TestCase):
    def test_same_range_everywhere(self):
        self.assertEqual((native_mod.VISCA_PRESET_MIN, native_mod.VISCA_PRESET_MAX),
                         (CAMERA_PRESET_MIN, CAMERA_PRESET_MAX))
        self.assertEqual((CAMERA_PRESET_MIN, CAMERA_PRESET_MAX), (1, 16))

    def test_store_and_recall_payloads(self):
        service = NativeControlService.__new__(NativeControlService)
        with mock.patch.object(native_mod, "_visca_tcp") as visca:
            asyncio.run(service.store_visca_preset("cam2", 16))
            asyncio.run(service.press({"type": "visca-preset", "camera": "cam1", "preset": 1}))
        self.assertEqual([c.args for c in visca.call_args_list], [(*CAM2, store(16)), (*CAM1, recall(1))])

    def test_invalid_numbers_send_nothing(self):
        service = NativeControlService.__new__(NativeControlService)
        with mock.patch.object(native_mod, "_visca_tcp") as visca:
            for camera, preset in (("cam1", 0), ("cam1", 17), ("cam1", True), ("cam1", "3"),
                                   ("cam1", 3.0), ("cam9", 3), (None, 3)):
                with self.assertRaises(NativeControlError):
                    asyncio.run(service.store_visca_preset(camera, preset))
                with self.assertRaises(NativeControlError):
                    asyncio.run(service.press({"type": "visca-preset", "camera": camera, "preset": preset}))
        visca.assert_not_called()


class SavedPresetStoreTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.path = Path(folder.name) / "companion_buttons.json"

    def store(self):
        return CompanionButtonStore(self.path, COMPANION_BUTTONS)

    def test_saved_and_catalog_presets(self):
        s = self.store()
        saved = asyncio.run(s.save_camera_preset("cam1", 9, "CAM1  Kanzel "))
        self.assertEqual(saved["id"], "native-cam1-preset-9")
        self.assertEqual(saved["label"], "CAM1 Kanzel")
        self.assertEqual(saved["native_action"], {"type": "visca-preset", "camera": "cam1", "preset": 9})
        # A number with a catalog key renames that key (same ID as before).
        renamed = asyncio.run(s.save_camera_preset("cam1", 7, "CAM1 Altar"))
        self.assertEqual(renamed["id"], "native-cam1-preset-logo")
        self.assertEqual(asyncio.run(s.camera_preset("cam1", 7))["label"], "CAM1 Altar")
        self.assertIsNone(asyncio.run(s.camera_preset("cam1", 10)))
        self.assertIsNone(asyncio.run(s.camera_preset("cam2", 9)))

        reloaded = {b["id"]: b for b in asyncio.run(self.store().camera_buttons())}
        self.assertEqual(reloaded["native-cam1-preset-9"]["label"], "CAM1 Kanzel")
        self.assertEqual(reloaded["native-cam1-preset-logo"]["label"], "CAM1 Altar")
        self.assertTrue(reloaded["native-cam1-preset-9"]["saved"])

    def test_bad_input(self):
        s = self.store()
        for camera, preset, label in (("cam9", 9, "CAM9 X"), ("cam1", 0, "CAM1 X"), ("cam1", 17, "CAM1 X"),
                                      ("cam1", True, "CAM1 X"), ("cam1", 9, "   "), ("cam1", 9, "x" * 65)):
            with self.assertRaises(ValueError):
                asyncio.run(s.save_camera_preset(camera, preset, label))
        self.assertFalse(any(b.get("saved") for b in asyncio.run(s.camera_buttons())))

    def test_rename_saved_preset_needs_a_name(self):
        s = self.store()
        asyncio.run(s.save_camera_preset("cam2", 12, "CAM2 Chor"))
        self.assertEqual(asyncio.run(s.set_camera_label("native-cam2-preset-12", "CAM2 Band"))["label"], "CAM2 Band")
        with self.assertRaises(ValueError):
            asyncio.run(s.set_camera_label("native-cam2-preset-12", ""))
        with self.assertRaises(KeyError):
            asyncio.run(s.set_camera_label("native-cam2-preset-13", "CAM2 X"))

    def test_invalid_stored_entries_are_dropped(self):
        self.store()  # writes the catalog
        data = json.loads(self.path.read_text())
        data["saved_camera_presets"] = {
            "native-cam1-preset-9": "CAM1 Kanzel",
            "native-cam1-preset-1": "CAM1 shadows the catalog",
            "native-cam1-preset-17": "CAM1 out of range",
            "native-cam1-preset-0": "CAM1 zero",
            "native-cam1-preset-09": "CAM1 leading zero",
            "native-cam9-preset-3": "CAM9 unknown camera",
            "native-cam2-preset-6": "  ",
            "native-cam2-preset-10": 5,
        }
        self.path.write_text(json.dumps(data))
        ids = {b["id"] for b in asyncio.run(self.store().camera_buttons()) if b.get("saved")}
        self.assertEqual(ids, {"native-cam1-preset-9"})


class CameraPresetRouteTests(unittest.TestCase):
    def setUp(self):
        visca = mock.patch.object(native_mod, "_visca_tcp")
        self.visca = visca.start()
        self.addCleanup(visca.stop)
        self.ctx = app_client()
        self.client, self.folder = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def sent(self):
        calls = [c.args for c in self.visca.call_args_list]
        self.visca.reset_mock()
        return calls

    def labels(self):
        buttons = self.client.get("/companion/buttons").json()["camera_buttons"]
        return {b["id"]: b["label"] for b in buttons}

    def test_recall_by_number(self):
        r = self.client.post("/cameras/cam1/preset/recall", json={"preset": 11})   # no name in the app
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((r.json()["camera"], r.json()["preset"]), ("cam1", 11))
        r = self.client.post("/cameras/cam2/preset/recall", json={"preset": 1})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sent(), [(*CAM1, recall(11)), (*CAM2, recall(1))])

        for preset in (0, 17, -1, "3", True, 2.5, None):
            self.assertEqual(self.client.post("/cameras/cam1/preset/recall", json={"preset": preset}).status_code, 422)
        self.assertEqual(self.client.post("/cameras/cam9/preset/recall", json={"preset": 3}).status_code, 404)
        self.assertEqual(self.sent(), [])

        self.visca.side_effect = NativeControlError("VISCA camera 192.168.5.21 unreachable")
        r = self.client.post("/cameras/cam1/preset/recall", json={"preset": 3})
        self.assertEqual(r.status_code, 503)
        self.assertIn("unreachable", r.json()["reason"])

    def test_save_new_number(self):
        r = self.client.post("/cameras/cam1/presets", json={"preset": 9, "name": " Kanzel "})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(r.json()["overwritten"])
        self.assertEqual(r.json()["button"]["id"], "native-cam1-preset-9")
        self.assertEqual(self.sent(), [(*CAM1, store(9))])
        self.assertEqual(self.labels()["native-cam1-preset-9"], "CAM1 Kanzel")

        # The saved preset runs like every preset key and in program rows.
        self.assertEqual(self.client.post("/companion/buttons/native-cam1-preset-9/press").status_code, 200)
        self.assertEqual(self.sent(), [(*CAM1, recall(9))])
        row = self.client.post("/program/items", json={
            "time": "10:00", "title": "Predigt", "person": "", "group": "",
            "playback": "native-cam1-preset-9", "cam1_preset": 9,
        })
        self.assertEqual(row.status_code, 200, row.text)
        # Not a key of another camera.
        self.assertEqual(self.client.post("/companion/buttons/native-cam2-preset-9/press").status_code, 404)

    def test_overwrite_is_asked_first(self):
        self.client.post("/cameras/cam2/presets", json={"preset": 6, "name": "Chor"})
        self.sent()
        r = self.client.post("/cameras/cam2/presets", json={"preset": 6, "name": "Band"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual((r.json()["exists"], r.json()["preset"], r.json()["name"]), (True, 6, "Chor"))
        self.assertEqual(self.sent(), [])                     # the camera keeps its position
        self.assertEqual(self.labels()["native-cam2-preset-6"], "CAM2 Chor")

        r = self.client.post("/cameras/cam2/presets", json={"preset": 6, "name": "Band", "overwrite": True})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertTrue(r.json()["overwritten"])
        self.assertEqual(self.sent(), [(*CAM2, store(6))])
        self.assertEqual(self.labels()["native-cam2-preset-6"], "CAM2 Band")

    def test_catalog_number_is_asked_and_renamed(self):
        r = self.client.post("/cameras/cam1/presets", json={"preset": 3, "name": "Flügel"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["name"], "Klavier")
        r = self.client.post("/cameras/cam1/presets", json={"preset": 3, "name": "Flügel", "overwrite": True})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.sent(), [(*CAM1, store(3))])
        self.assertEqual(self.labels()["native-cam1-preset-3"], "CAM1 Flügel")
        # Still one key for number 3.
        threes = [b for b in self.client.get("/companion/buttons").json()["camera_buttons"]
                  if b["native_action"] == {"type": "visca-preset", "camera": "cam1", "preset": 3}]
        self.assertEqual(len(threes), 1)

    def test_refused_saves_change_nothing(self):
        before = self.labels()
        for body, status in (({"preset": 9, "name": "  "}, 400), ({"preset": 0, "name": "X"}, 422),
                             ({"preset": 17, "name": "X"}, 422), ({"preset": "9", "name": "X"}, 422),
                             ({"preset": 9, "name": "x" * 59}, 422), ({"preset": 9}, 422)):
            self.assertEqual(self.client.post("/cameras/cam1/presets", json=body).status_code, status, body)
        self.assertEqual(self.client.post("/cameras/cam9/presets", json={"preset": 9, "name": "X"}).status_code, 404)
        self.assertEqual(self.sent(), [])

        # The camera did not take it: the name is not stored either.
        self.visca.side_effect = NativeControlError("VISCA camera 192.168.5.21 unreachable")
        r = self.client.post("/cameras/cam1/presets", json={"preset": 9, "name": "Kanzel"})
        self.assertEqual(r.status_code, 503)
        self.assertEqual(self.labels(), before)

    def test_rename_saved_preset(self):
        self.client.post("/cameras/cam1/presets", json={"preset": 14, "name": "Taufe"})
        r = self.client.put("/camera-presets/native-cam1-preset-14/label", json={"name": "CAM1 Taufbecken"})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.labels()["native-cam1-preset-14"], "CAM1 Taufbecken")
        self.assertEqual(self.client.put("/camera-presets/native-cam1-preset-14/label", json={"name": ""}).status_code, 400)
        self.assertEqual(self.client.put("/camera-presets/native-cam1-preset-15/label", json={"name": "X"}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
