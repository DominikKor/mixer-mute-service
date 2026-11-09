"""Licht section (app/lights.py): stage values, room light, scenes, program
rows with a light scene, and the light/mixer keys leaving Steuerung (v8).

The devices are fakes: Art-Net frames and Shelly calls are recorded, nothing
goes onto the network.
"""

import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

import app.native_controls as native_mod
from app.companion_buttons import (
    CATALOG_ONLY_IDS, COMPANION_BUTTONS, DEFAULT_CATEGORIES, CompanionButtonStore,
)
from tests.helpers import app_client


class FakeDevices:
    def __init__(self):
        self.frames = []
        self.shelly = {target: False for target in native_mod.SHELLY_HOSTS}
        self.shelly["light-tech"] = True
        self.offline = set()
        self.host_to_target = {host: target for target, host in native_mod.SHELLY_HOSTS.items()}

    def artnet(self, host, universe, values):
        self.frames.append(list(values))

    def rpc(self, host, method, params):
        target = self.host_to_target[host]
        if target in self.offline:
            raise native_mod.NativeControlError(f"Shelly {host} unreachable")
        if method == "Switch.GetStatus":
            return {"id": 0, "output": self.shelly[target]}
        if method == "Switch.Set":
            self.shelly[target] = params["on"]
            return {"was_on": not params["on"]}
        raise AssertionError(method)

    def zone_white(self, zone):
        return self.frames[-1][native_mod.ARTNET_ZONES[zone]["white"] - 1]


class LightsApiTests(unittest.TestCase):
    def setUp(self):
        self.devices = FakeDevices()
        patches = [
            mock.patch.object(native_mod, "_send_artnet_frame", self.devices.artnet),
            mock.patch.object(native_mod, "_shelly_rpc", self.devices.rpc),
            mock.patch("app.lights.CIRCUIT_CACHE_SECONDS", 0),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.ctx = app_client()
        self.client, self.folder = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.put("/lights/settings", json={"fade_ms": 0})

    def test_fade_and_jump(self):
        self.client.put("/lights/settings", json={"fade_ms": 400})
        self.client.post("/lights/stage", json={"zones": {"left": 200}})
        faded = len(self.devices.frames)
        self.client.post("/lights/stage", json={"zones": {"left": 100}, "fade": False})
        self.assertGreater(faded, 5)
        self.assertEqual(len(self.devices.frames) - faded, 1)
        self.assertEqual(self.devices.zone_white("left"), 100)

    def test_state_lists_stage_circuits_and_scenes(self):
        data = self.client.get("/lights").json()
        self.assertEqual(set(data["stage"]["zones"]), set(native_mod.ARTNET_ZONES))
        self.assertEqual({c["id"]: c["on"] for c in data["circuits"]}["light-tech"], True)
        self.assertEqual(data["scenes"], [])
        self.devices.offline.add("light-left")
        data = self.client.get("/lights").json()
        self.assertIsNone({c["id"]: c["on"] for c in data["circuits"]}["light-left"])

    def test_stage_set_absolute_and_validated(self):
        r = self.client.post("/lights/stage", json={"zones": {"left": 200, "right": 0}, "warm": 180})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.devices.zone_white("left"), 200)
        self.assertEqual(self.devices.frames[-1][native_mod.ARTNET_ZONES["left"]["warm"] - 1], 180)
        self.assertEqual(r.json()["stage"]["warm"], 180)
        for bad in ({"zones": {"left": 256}}, {"zones": {"nope": 1}}, {"warm": -1}, {"zones": {"left": True}}):
            self.assertEqual(self.client.post("/lights/stage", json=bad).status_code, 400, bad)
        self.assertEqual(self.client.put("/lights/settings", json={"steps": 1}).status_code, 400)

    def test_room_light_switched_and_failures_listed(self):
        r = self.client.post("/lights/circuits", json={"states": {"light-left": True, "light-front": True}})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(self.devices.shelly["light-left"] and self.devices.shelly["light-front"])
        self.devices.offline.add("light-right")
        r = self.client.post("/lights/circuits", json={"states": {t: False for t in native_mod.SHELLY_HOSTS}})
        self.assertEqual(r.status_code, 502)
        self.assertEqual(list(r.json()["errors"]), ["light-right"])
        self.assertFalse(self.devices.shelly["light-left"])  # the others were still switched
        self.assertEqual(self.client.post("/lights/circuits", json={"states": {"x": True}}).status_code, 400)

    def test_scene_saved_from_current_light_and_applied(self):
        self.client.post("/lights/stage", json={"zones": {"left": 120, "center-left": 0, "center-right": 0, "right": 120}})
        self.devices.offline.add("light-stage")  # unknown state: not part of the scene
        r = self.client.post("/lights/scenes", json={"name": "Predigt"})
        self.assertEqual(r.status_code, 200)
        scene = r.json()["scene"]
        self.assertEqual(r.json()["unknown_circuits"], ["light-stage"])
        self.assertNotIn("light-stage", scene["circuits"])
        self.assertTrue(scene["circuits"]["light-tech"])
        self.assertEqual(self.client.post("/lights/scenes", json={"name": "predigt"}).status_code, 409)
        self.assertEqual(self.client.post("/lights/scenes", json={"name": " "}).status_code, 400)

        self.client.post("/lights/stage", json={"zones": {z: 255 for z in native_mod.ARTNET_ZONES}})
        self.client.post("/lights/circuits", json={"states": {"light-tech": False}})
        r = self.client.post(f"/lights/scenes/{scene['id']}/apply")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.devices.zone_white("left"), 120)
        self.assertEqual(self.devices.zone_white("center-left"), 0)
        self.assertTrue(self.devices.shelly["light-tech"])

        self.devices.offline.add("light-tech")
        r = self.client.post(f"/lights/scenes/{scene['id']}/apply")
        self.assertEqual(r.status_code, 502)
        self.assertIn("light-tech", r.json()["errors"])
        self.assertEqual(self.client.post("/lights/scenes/nope/apply").status_code, 404)

        self.assertEqual([s["name"] for s in self.client.get("/lights/scenes").json()["scenes"]], ["Predigt"])
        stored = json.loads((self.folder / "light_scenes.json").read_text(encoding="utf-8"))
        self.assertEqual([s["name"] for s in stored], ["Predigt"])
        self.assertEqual(self.client.delete(f"/lights/scenes/{scene['id']}").status_code, 200)
        self.assertEqual(self.client.get("/lights").json()["scenes"], [])

    def test_program_row_applies_its_light_scene(self):
        self.client.post("/lights/stage", json={"zones": {z: 80 for z in native_mod.ARTNET_ZONES}})
        scene = self.client.post("/lights/scenes", json={"name": "Gebet"}).json()["scene"]
        row = {"time": "10:00", "title": "Gebet", "group": "", "light": "nope"}
        self.assertEqual(self.client.post("/program/items", json=row).status_code, 400)
        item = self.client.post("/program/items", json={**row, "light": scene["id"]}).json()["item"]
        self.assertEqual(item["light"], scene["id"])
        self.client.post("/lights/stage", json={"zones": {z: 255 for z in native_mod.ARTNET_ZONES}})
        r = self.client.post(f"/program/items/{item['id']}/play")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["light"]["scene"], "Gebet")
        self.assertEqual(self.devices.zone_white("right"), 80)
        # Scene deleted later: the row refuses to start instead of half running.
        self.client.delete(f"/lights/scenes/{scene['id']}")
        r = self.client.post(f"/program/items/{item['id']}/play")
        self.assertEqual((r.status_code, r.json()["reason"]), (409, "unknown light scene"))

    def test_light_and_mixer_keys_still_run_by_id(self):
        data = self.client.get("/companion/buttons").json()
        ids = {b["id"] for b in data["buttons"]}
        self.assertFalse(CATALOG_ONLY_IDS & ids)
        self.assertNotIn("light", {c["id"] for c in data["categories"]})
        self.assertEqual({b["id"] for b in data["catalog_buttons"]}, CATALOG_ONLY_IDS)
        r = self.client.post("/companion/buttons/native-shelly-all-on/press")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(all(self.devices.shelly.values()))
        row = {"time": "10:00", "title": "Licht an", "group": "", "playback": "native-shelly-all-off"}
        self.assertEqual(self.client.post("/program/items", json=row).status_code, 200)


class CatalogV8Tests(unittest.TestCase):
    def test_v8_migration_removes_light_and_mixer_keys(self):
        legacy = deepcopy(DEFAULT_CATEGORIES) + [
            {"id": "light", "name": "Licht", "visible": True}, {"id": "mixer", "name": "Mixer", "visible": True}]
        buttons = deepcopy([b for b in COMPANION_BUTTONS if b.get("category_id") != "cameras"])
        own = {"id": "user-1", "label": "Mein Mute", "category_id": "mixer", "kind": "group",
               "group": "Band", "operation": "mute"}
        macro_before = deepcopy(next(b for b in buttons if b["id"] == "native-overall-predigt"))
        stored = {"categories": legacy, "buttons": buttons + [own], "native_catalog_version": 7}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            store = CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(list(Path(folder).glob("*.corrupt*")), [])
            snapshot = asyncio.run(store.snapshot())
        ids = {b["id"] for b in migrated["buttons"]}
        self.assertEqual(migrated["native_catalog_version"], 8)
        self.assertFalse(CATALOG_ONLY_IDS & ids)
        self.assertIn("user-1", ids)
        categories = {c["id"] for c in migrated["categories"]}
        self.assertNotIn("light", categories)  # empty now
        self.assertIn("mixer", categories)  # still holds the user's own key
        self.assertEqual(next(b for b in migrated["buttons"] if b["id"] == "native-overall-predigt"), macro_before)
        self.assertEqual(len(snapshot["buttons"]), len(migrated["buttons"]))

    def test_fresh_store_has_no_light_or_mixer_keys(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "buttons.json"
            CompanionButtonStore(path, COMPANION_BUTTONS)
            stored = json.loads(path.read_text(encoding="utf-8"))
        self.assertFalse(CATALOG_ONLY_IDS & {b["id"] for b in stored["buttons"]})
        self.assertFalse({"light", "mixer"} & {c["id"] for c in stored["categories"]})


if __name__ == "__main__":
    unittest.main()
