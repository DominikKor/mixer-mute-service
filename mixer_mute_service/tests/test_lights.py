"""Licht section (app/lights.py): stage values, room light, scenes, program
rows with a light scene, and the light/mixer keys leaving Steuerung (v8).

The devices are fakes: Art-Net frames and Shelly calls are recorded, nothing
goes onto the network.
"""

import asyncio
import json
import tempfile
import threading
import time
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
        # target -> Event: its status question reads the state, then waits
        # for the event (a slow or hanging Shelly); slow_set: its switching.
        self.slow = {}
        self.slow_set = {}
        self.host_to_target = {host: target for target, host in native_mod.SHELLY_HOSTS.items()}

    def artnet(self, host, universe, values):
        self.frames.append(list(values))

    def rpc(self, host, method, params):
        target = self.host_to_target[host]
        if target in self.offline:
            raise native_mod.NativeControlError(f"Shelly {host} unreachable")
        if method == "Switch.GetStatus":
            output = self.shelly[target]
            if target in self.slow:
                self.slow[target].wait(5)
            return {"id": 0, "output": output}
        if method == "Switch.Set":
            if target in self.slow_set:
                self.slow_set[target].wait(5)
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

    def test_artnet_status_route(self):
        node = {"short_name": "Stage", "long_name": "", "report": "",
                "outputs": [{"sending_dmx": True, "merging": True, "merge_mode": "HTP"}]}
        with mock.patch("app.api.lights.artnet_status", return_value=node):
            data = self.client.get("/lights/artnet").json()
        self.assertTrue(data["ok"])
        self.assertTrue(data["merging"])
        self.assertEqual(data["host"], native_mod.ARTNET_HOST)
        # Silent until the first stage command, then the frame is kept alive.
        self.assertEqual(data["sending"], {"active": False, "running": True, "error": None})
        self.client.post("/lights/stage", json={"zones": {"left": 30}})
        with mock.patch("app.api.lights.artnet_status", return_value=node):
            self.assertTrue(self.client.get("/lights/artnet").json()["sending"]["active"])
        with mock.patch("app.api.lights.artnet_status", return_value=None):
            self.assertEqual(self.client.get("/lights/artnet").status_code, 504)
        with mock.patch("app.api.lights.artnet_status", side_effect=native_mod.NativeControlError("port taken")):
            r = self.client.get("/lights/artnet")
            self.assertEqual(r.status_code, 502)
            self.assertIn("port taken", r.json()["reason"])

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

    def test_switch_answers_with_the_confirmed_states(self):
        self.client.get("/lights")
        r = self.client.post("/lights/circuits", json={"states": {"light-left": True}})
        circuits = r.json()["circuits"]
        self.assertTrue(circuits["light-left"])
        self.assertTrue(circuits["light-tech"])  # known from before
        self.assertEqual(set(circuits), set(native_mod.SHELLY_HOSTS))
        self.devices.offline.add("light-front")
        r = self.client.post("/lights/circuits", json={"states": {"light-front": True, "light-right": True}})
        self.assertEqual(r.status_code, 502)
        self.assertTrue(r.json()["circuits"]["light-right"])
        self.assertFalse(r.json()["circuits"]["light-front"])  # still the last known state

    def _hang(self, target, switching=False):
        event = threading.Event()
        (self.devices.slow_set if switching else self.devices.slow)[target] = event
        self.addCleanup(event.set)
        return event

    def _wait_for_query(self, target):
        cache = self.client.app.state.light_circuits
        end = time.monotonic() + 3
        while not cache._pending[target].done() and time.monotonic() < end:
            time.sleep(0.01)
        self.assertTrue(cache._pending[target].done())

    def test_a_hanging_shelly_does_not_hold_up_the_page(self):
        self.client.get("/lights")
        release = self._hang("light-front")
        self.devices.shelly["light-front"] = True
        self.devices.shelly["light-left"] = True
        started = time.monotonic()
        data = self.client.get("/lights").json()
        self.assertLess(time.monotonic() - started, 1.5)
        states = {c["id"]: c["on"] for c in data["circuits"]}
        self.assertTrue(states["light-left"])      # the others answered
        self.assertFalse(states["light-front"])    # not yet: its last state
        release.set()
        self._wait_for_query("light-front")
        states = {c["id"]: c["on"] for c in self.client.get("/lights").json()["circuits"]}
        self.assertTrue(states["light-front"])

    def test_a_circuit_without_answer_is_not_waited_for(self):
        self.devices.offline.add("light-front")
        self.client.get("/lights")  # light-front: no answer, unknown
        self.devices.offline.discard("light-front")
        release = self._hang("light-front")
        with mock.patch("app.lights.CIRCUIT_WAIT_SECONDS", 5):
            started = time.monotonic()
            states = {c["id"]: c["on"] for c in self.client.get("/lights").json()["circuits"]}
            self.assertLess(time.monotonic() - started, 1.5)
        self.assertIsNone(states["light-front"])
        release.set()
        self._wait_for_query("light-front")
        self.assertFalse(self.client.app.state.light_circuits.snapshot()["light-front"])

    def test_one_question_per_shelly_at_a_time(self):
        self.client.get("/lights")
        release = self._hang("light-front")
        calls = []
        rpc = self.devices.rpc
        with mock.patch.object(native_mod, "_shelly_rpc", lambda h, m, p: calls.append(h) or rpc(h, m, p)):
            for _ in range(3):
                self.client.get("/lights")
            release.set()
            self._wait_for_query("light-front")
        self.assertEqual(calls.count(native_mod.SHELLY_HOSTS["light-front"]), 1)

    def test_an_answer_from_before_a_switch_is_dropped(self):
        self.client.get("/lights")
        release = self._hang("light-front")
        self.client.get("/lights")  # asks light-front: it reads "off", then hangs
        r = self.client.post("/lights/circuits", json={"states": {"light-front": True}})
        self.assertEqual(r.status_code, 200)
        release.set()
        self._wait_for_query("light-front")
        self.assertTrue(self.client.app.state.light_circuits.snapshot()["light-front"])

    def test_scene_stage_does_not_wait_for_a_hanging_shelly(self):
        self.client.post("/lights/stage", json={"zones": {"left": 0}})
        self.client.get("/lights")
        r = self.client.post("/lights/scenes", json={
            "name": "Vorne", "lights": {"zones": ["left"], "circuits": ["light-front"]}})
        scene_id = r.json()["scene"]["id"]
        self.client.post("/lights/stage", json={"zones": {"left": 200}})
        frames = len(self.devices.frames)
        release = self._hang("light-front", switching=True)
        done = threading.Thread(target=self.client.post, args=(f"/lights/scenes/{scene_id}/apply",))
        done.start()
        end = time.monotonic() + 3
        while len(self.devices.frames) == frames and time.monotonic() < end:
            time.sleep(0.01)
        self.assertGreater(len(self.devices.frames), frames)  # the stage moved while the Shelly hangs
        release.set()
        done.join(5)
        self.assertEqual(self.devices.zone_white("left"), 0)

    def test_all_on_key_switches_every_answering_circuit(self):
        self.devices.offline.add("light-front")  # before light-tech and light-stage
        r = self.client.post("/companion/buttons/native-shelly-all-on/press")
        self.assertNotEqual(r.status_code, 200)
        self.assertIn("light-front", r.json()["reason"])
        self.assertTrue(all(on for target, on in self.devices.shelly.items() if target != "light-front"))

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

    def test_scene_holds_only_the_chosen_lights(self):
        self.client.post("/lights/stage", json={"zones": {"left": 90, "center-left": 30, "center-right": 30, "right": 90},
                                                "warm": 200, "cold": 40})
        self.devices.shelly["light-front"] = True
        r = self.client.post("/lights/scenes", json={
            "name": "Kanzel", "lights": {"zones": ["left"], "color": False, "circuits": ["light-front"]}})
        self.assertEqual(r.status_code, 200, r.text)
        scene = r.json()["scene"]
        self.assertEqual(scene["stage"], {"zones": {"left": 90}, "warm": None, "cold": None})
        self.assertEqual(scene["circuits"], {"light-front": True})

        # Everything else changes; applying sets only the chosen lights.
        self.client.post("/lights/stage", json={"zones": {z: 255 for z in native_mod.ARTNET_ZONES}, "warm": 10, "cold": 250})
        self.client.post("/lights/circuits", json={"states": {"light-front": False, "light-tech": False}})
        self.assertEqual(self.client.post(f"/lights/scenes/{scene['id']}/apply").status_code, 200)
        self.assertEqual(self.devices.zone_white("left"), 90)
        self.assertEqual(self.devices.zone_white("right"), 255)
        stage = self.client.get("/lights").json()["stage"]
        self.assertEqual((stage["warm"], stage["cold"]), (10, 250))
        self.assertTrue(self.devices.shelly["light-front"])
        self.assertFalse(self.devices.shelly["light-tech"])

        # Only the room light: the stage is not touched at all.
        room = self.client.post("/lights/scenes", json={
            "name": "Raum", "lights": {"circuits": ["light-tech"]}}).json()["scene"]
        self.assertEqual(room["stage"], {"zones": {}, "warm": None, "cold": None})
        frames = len(self.devices.frames)
        self.assertEqual(self.client.post(f"/lights/scenes/{room['id']}/apply").status_code, 200)
        self.assertEqual(len(self.devices.frames), frames)

        # The colour alone: warm and cold, the zones keep their level.
        color = self.client.post("/lights/scenes", json={"name": "Warm", "lights": {"color": True}}).json()["scene"]
        self.assertEqual(color["stage"], {"zones": {}, "warm": 10, "cold": 250})

        for bad in ({}, {"zones": ["nope"]}, {"circuits": ["x"]}, {"color": "ja"}, {"zones": "left"}, []):
            r = self.client.post("/lights/scenes", json={"name": "Falsch", "lights": bad})
            self.assertEqual(r.status_code, 400, bad)
        # A chosen circuit that does not answer is left out; alone it is no scene.
        self.devices.offline.add("light-stage")
        r = self.client.post("/lights/scenes", json={"name": "Stumm", "lights": {"circuits": ["light-stage"]}})
        self.assertEqual(r.status_code, 400)
        self.assertEqual(r.json()["unknown_circuits"], ["light-stage"])
        r = self.client.post("/lights/scenes", json={"name": "Teil", "lights": {"zones": ["right"], "circuits": ["light-stage"]}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["unknown_circuits"], ["light-stage"])
        self.assertEqual(r.json()["scene"]["circuits"], {})

    def test_scenes_saved_before_the_choice_still_load(self):
        from app.lights import LightSceneStore, SceneError, _clean_scene
        old = {"id": "a", "name": "Alt", "stage": {"zones": {z: 50 for z in native_mod.ARTNET_ZONES}, "warm": 1, "cold": 2},
               "circuits": {"light-tech": True}}
        path = self.folder / "old_scenes.json"
        path.write_text(json.dumps([old]), encoding="utf-8")
        scenes = asyncio.run(LightSceneStore(path).list())
        self.assertEqual(scenes, [old])
        for stage in ({"zones": {}, "warm": 1, "cold": None}, {"zones": {}, "warm": None, "cold": None}):
            with self.assertRaises(SceneError):
                _clean_scene({"name": "x", "stage": stage, "circuits": {}})

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
