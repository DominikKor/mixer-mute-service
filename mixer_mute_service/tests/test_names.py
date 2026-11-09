"""Renaming (app/names.py, app/api/names.py): stage zones, room light circuits
and "Alles ausschalten" devices get the user's names on the service; light
scenes, program templates and Steuerung categories can be renamed. No device
is contacted (Art-Net and Shelly are fakes)."""

import json
import unittest
from unittest import mock

import app.native_controls as native_mod
from tests.helpers import app_client

DESKTOP = (
    "desktop_display:\n  enabled: true\n  token_env: NAMES_TEST_TOKEN\n  profiles:\n"
    "    qr:\n      title: Gemeindegaben-QR\n      kind: image\n      path: C:/Bilder/qr.png\n"
    "      target_ip: 192.168.188.11\n"
)


class RenameTests(unittest.TestCase):
    def setUp(self):
        patches = [
            mock.patch.object(native_mod, "_send_artnet_frame", lambda *a: None),
            mock.patch.object(native_mod, "_shelly_rpc", lambda host, method, params: {"output": False}),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ctx = app_client(DESKTOP, env={"NAMES_TEST_TOKEN": "t"})
        self.client, self.folder = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)

    def test_light_names_on_the_service(self):
        r = self.client.put("/names/light-zone/left", json={"name": "  Kanzel   links "})
        self.assertEqual((r.status_code, r.json()["name"]), (200, "Kanzel links"))
        self.client.put("/names/light-circuit/light-tech", json={"name": "Technikpult"})
        data = self.client.get("/lights").json()
        self.assertEqual(data["zone_labels"]["left"], "Kanzel links")
        self.assertEqual(data["zone_defaults"]["left"], "Links")
        tech = next(c for c in data["circuits"] if c["id"] == "light-tech")
        self.assertEqual((tech["label"], tech["default_label"]), ("Technikpult", "Technik"))
        # Empty: back to the built-in name.
        self.client.put("/names/light-zone/left", json={"name": ""})
        self.assertEqual(self.client.get("/lights").json()["zone_labels"]["left"], "Links")
        stored = json.loads((self.folder / "display_names.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["light-circuit"], {"light-tech": "Technikpult"})

    def test_bad_names_and_targets_refused(self):
        self.assertEqual(self.client.put("/names/light-zone/nope", json={"name": "x"}).status_code, 404)
        self.assertEqual(self.client.put("/names/other/left", json={"name": "x"}).status_code, 404)
        self.assertEqual(self.client.put("/names/power-target/999.1.1.1", json={"name": "x"}).status_code, 404)
        self.assertEqual(self.client.put("/names/light-zone/left", json={"name": "x" * 41}).status_code, 400)
        self.assertEqual(self.client.put("/names/light-zone/left", json={"name": 5}).status_code, 400)

    def test_power_off_devices_renamed(self):
        self.client.put("/names/power-target/192.168.188.11", json={"name": "Anzeige-PC"})
        self.client.put("/names/power-target/beamer", json={"name": "Projektor Saal"})
        data = self.client.get("/power/off-config").json()
        pc = data["available_targets"][0]
        self.assertEqual((pc["label"], pc["default_label"]), ("Anzeige-PC", "Gemeindegaben-QR"))
        self.assertEqual(data["beamer_label"], "Projektor Saal")

    def test_scene_template_and_category_renamed(self):
        a = self.client.post("/lights/scenes", json={"name": "Predigt"}).json()["scene"]
        self.client.post("/lights/scenes", json={"name": "Gebet"})
        self.assertEqual(self.client.put(f"/lights/scenes/{a['id']}", json={"name": "gebet"}).status_code, 409)
        self.assertEqual(self.client.put(f"/lights/scenes/{a['id']}", json={"name": " "}).status_code, 400)
        self.assertEqual(self.client.put("/lights/scenes/nope", json={"name": "X"}).status_code, 404)
        r = self.client.put(f"/lights/scenes/{a['id']}", json={"name": "Lobpreis"})
        self.assertEqual((r.status_code, r.json()["scene"]["id"]), (200, a["id"]))
        self.assertIn("Lobpreis", [s["name"] for s in self.client.get("/lights/scenes").json()["scenes"]])

        self.client.post("/program/items", json={"time": "10:00", "title": "A", "group": ""})
        t1 = self.client.post("/program/templates", json={"name": "Gottesdienst", "slots": []}).json()["template"]
        self.client.post("/program/templates", json={"name": "Taufe", "slots": []})
        self.assertEqual(self.client.put(f"/program/templates/{t1['id']}", json={"name": "taufe"}).status_code, 409)
        r = self.client.put(f"/program/templates/{t1['id']}", json={"name": "Abendmahl"})
        self.assertEqual((r.status_code, r.json()["template"]["name"]), (200, "Abendmahl"))
        self.assertEqual(self.client.put("/program/templates/nope", json={"name": "X"}).status_code, 404)

        r = self.client.post("/companion/categories/obs/rename", json={"name": "Stream"})
        self.assertEqual((r.status_code, r.json()["category"]["name"]), (200, "Stream"))
        self.assertEqual(self.client.post("/companion/categories/overall/rename", json={"name": "stream"}).status_code, 409)
        self.assertEqual(self.client.post("/companion/categories/nope/rename", json={"name": "X"}).status_code, 404)
        names = {c["id"]: c["name"] for c in self.client.get("/companion/buttons").json()["categories"]}
        self.assertEqual(names["obs"], "Stream")


    def test_camera_preset_renamed(self):
        r = self.client.put("/camera-presets/native-cam1-preset-1/label", json={"name": "CAM1  Kanzel"})
        self.assertEqual((r.status_code, r.json()["button"]["label"]), (200, "CAM1 Kanzel"))
        labels = {b["id"]: b["label"] for b in self.client.get("/companion/buttons").json()["camera_buttons"]}
        self.assertEqual(labels["native-cam1-preset-1"], "CAM1 Kanzel")
        stored = json.loads((self.folder / "companion_buttons.json").read_text(encoding="utf-8"))
        self.assertEqual(stored["camera_labels"]["native-cam1-preset-1"], "CAM1 Kanzel")
        # Empty: the catalog's label again.
        r = self.client.put("/camera-presets/native-cam1-preset-1/label", json={"name": ""})
        self.assertEqual(r.json()["button"]["label"], "CAM1 Pult nah")
        self.assertNotIn("native-cam1-preset-1", json.loads(
            (self.folder / "companion_buttons.json").read_text(encoding="utf-8"))["camera_labels"])
        self.assertEqual(self.client.put("/camera-presets/beamer-freeze/label", json={"name": "x"}).status_code, 404)
        self.assertEqual(self.client.put("/camera-presets/native-cam1-preset-1/label", json={"name": "x" * 65}).status_code, 400)

if __name__ == "__main__":
    unittest.main()
