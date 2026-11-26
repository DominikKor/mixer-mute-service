"""Stage lights one by one and light presets (app/native_controls.py,
app/lights.py, app/api/lights.py): "off" keeps a light's brightness, each
light has its own warm/cold colour, presets save on/off, brightness and
colour per light, keep their order, show which one is active, and older
state files and scenes are converted. Devices are fakes (tests/test_lights.py).
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

import app.native_controls as native_mod
from app.lights import LightSceneStore, _clean_scene, scene_is_active
from app.native_controls import NativeControlError, NativeControlService
from tests.helpers import app_client
from tests.test_lights import FakeDevices

ZONES = list(native_mod.ARTNET_ZONES)


def channels(frame, zone):
    z = native_mod.ARTNET_ZONES[zone]
    return [frame[z["white"] - 1], frame[z["warm"] - 1], frame[z["cold"] - 1]]


class FixtureTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "state.json"
        self.frames = []
        patcher = patch.object(native_mod, "_send_artnet_frame", lambda host, universe, values: self.frames.append(list(values)))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.controls = NativeControlService(self.state_path)
        self.controls._levels["fade_ms"] = 0

    async def test_off_keeps_the_level_and_on_brings_it_back(self):
        await self.controls.set_stage({"left": 200})
        await self.controls.set_stage({"left": {"on": False}})
        state = self.controls.stage_state()
        self.assertEqual(state["zones"]["left"], 0)                    # dark on stage
        self.assertEqual(state["fixtures"]["left"]["level"], 200)      # the slider keeps its place
        self.assertEqual(channels(self.frames[-1], "left")[0], 0)
        await self.controls.set_stage({"left": {"on": True}})
        self.assertEqual(channels(self.frames[-1], "left")[0], 200)

    async def test_number_zero_switches_off_and_keeps_the_level(self):
        await self.controls.set_stage({"left": 150})
        await self.controls.set_stage({"left": 0})                     # older callers
        self.assertEqual(self.controls.stage_state()["fixtures"]["left"], {"on": False, "level": 150, "warm": 255, "cold": 50})

    async def test_moving_an_off_light_only_stores_the_level(self):
        await self.controls.set_stage({"left": 100})
        await self.controls.set_stage({"left": {"on": False}})
        await self.controls.set_stage({"left": {"level": 60}}, fade=False)
        self.assertEqual(channels(self.frames[-1], "left")[0], 0)      # stays dark
        await self.controls.set_stage({"left": {"on": True}})
        self.assertEqual(channels(self.frames[-1], "left")[0], 60)

    async def test_switching_on_without_a_level_uses_the_start_brightness(self):
        self.controls._levels["start"] = 180
        await self.controls.set_stage({"right": {"on": True}})
        self.assertEqual(self.controls.stage_state()["fixtures"]["right"]["level"], 180)

    async def test_each_light_has_its_own_colour(self):
        await self.controls.set_stage({z: 255 for z in ZONES})
        await self.controls.set_stage({"left": {"warm": 255, "cold": 0}, "right": {"warm": 30, "cold": 255}})
        frame = self.frames[-1]
        self.assertEqual(channels(frame, "left"), [255, 255, 0])
        self.assertEqual(channels(frame, "right"), [255, 30, 255])
        self.assertEqual(channels(frame, "center-left"), [255, 255, 50])  # untouched: Companion's colour
        state = self.controls.stage_state()
        self.assertEqual((state["warm"], state["cold"]), (None, None))
        await self.controls.set_stage(warm=200, cold=100)              # all lights at once
        self.assertEqual({(f["warm"], f["cold"]) for f in self.controls.stage_state()["fixtures"].values()}, {(200, 100)})
        self.assertEqual(self.controls.stage_state()["warm"], 200)

    async def test_invalid_changes_are_refused(self):
        for zones in ({"left": {"on": 1}}, {"left": {"level": 256}}, {"left": {"blue": 3}}, {"left": {}},
                      {"left": -1}, {"left": True}, {"nope": 3}):
            with self.assertRaises(NativeControlError, msg=zones):
                await self.controls.set_stage(zones)
        self.assertEqual(self.frames, [])

    async def test_failed_fade_keeps_the_old_memory(self):
        await self.controls.set_stage({"left": 90})
        with patch.object(native_mod, "_send_artnet_frame", side_effect=OSError("down")):
            with self.assertRaises(OSError):
                await self.controls.set_stage({"left": {"level": 250, "warm": 0}})
        self.assertEqual(self.controls.stage_state()["fixtures"]["left"], {"on": True, "level": 90, "warm": 255, "cold": 50})

    async def test_toggle_key_restores_the_level(self):
        await self.controls.set_stage({"left": 77})
        await self.controls.press({"type": "artnet-zone-toggle", "zone": "left"})
        self.assertEqual(self.controls.stage_state()["zones"]["left"], 0)
        result = await self.controls.press({"type": "artnet-zone-toggle", "zone": "left"})
        self.assertEqual(result["level"], 77)

    async def test_overall_zones_on_and_colour_keys_still_work(self):
        await self.controls.press({"type": "artnet-zones-set", "on": ["left", "right"]})
        self.assertEqual(channels(self.frames[-1], "left"), [255, 255, 50])
        self.assertEqual(channels(self.frames[-1], "center-left")[0], 0)
        await self.controls.set_stage({"left": {"warm": 100}})
        result = await self.controls.press({"type": "artnet-global-adjust", "color": "warm", "delta": 50})
        fixtures = self.controls.stage_state()["fixtures"]
        self.assertEqual((fixtures["left"]["warm"], fixtures["right"]["warm"]), (150, 255))
        # The colours differ now: "level" stays a number (the average).
        self.assertEqual(result["level"], round((150 + 255 * 3) / 4))
        self.assertEqual(result["levels"]["left"], 150)
        await self.controls.press({"type": "artnet-zone-adjust", "zone": "right", "delta": -1})
        self.assertEqual(self.controls.stage_state()["zones"]["right"], 205)

    async def test_overall_switching_off_keeps_the_level(self):
        await self.controls.set_stage({"left": 170, "right": 90})
        await self.controls.press({"type": "artnet-zones-set", "on": ["right"], "off": ["left"]})
        state = self.controls.stage_state()
        self.assertEqual(state["fixtures"]["left"], {"on": False, "level": 170, "warm": 255, "cold": 50})
        self.assertEqual(channels(self.frames[-1], "left")[0], 0)
        self.assertEqual(state["zones"]["right"], 90)                 # was on: keeps its level
        await self.controls.set_stage({"left": {"on": True}})
        self.assertEqual(channels(self.frames[-1], "left")[0], 170)

    async def test_saved_and_reloaded(self):
        await self.controls.set_stage({"left": {"on": True, "level": 40, "warm": 10, "cold": 20}})
        await self.controls.set_stage({"left": {"on": False}})
        again = NativeControlService(self.state_path)
        self.assertEqual(again.stage_state()["fixtures"]["left"], {"on": False, "level": 40, "warm": 10, "cold": 20})


class StateMigrationTests(unittest.TestCase):
    def test_state_from_before_the_lights_were_stored_one_by_one(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            dmx = [0] * 512
            dmx[0:3] = [120, 200, 40]       # left lit
            dmx[12:15] = [0, 200, 40]       # right off
            path.write_text(json.dumps({"dmx": dmx, "stage_active": True,
                                        "levels": {"warm": 200, "cold": 40, "start": 180, "steps": 20, "fade_ms": 500}}),
                            encoding="utf-8")
            controls = NativeControlService(path)
        fixtures = controls.stage_state()["fixtures"]
        self.assertEqual(fixtures["left"], {"on": True, "level": 120, "warm": 200, "cold": 40})
        self.assertEqual(fixtures["right"], {"on": False, "level": 0, "warm": 200, "cold": 40})
        self.assertEqual(controls._levels, {"start": 180, "steps": 20, "fade_ms": 500})
        self.assertTrue(controls.keepalive_status()["active"])

    def test_broken_saved_lights_fall_back_to_the_frame(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "state.json"
            dmx = [0] * 512
            dmx[4:7] = [99, 180, 70]        # center-left: level and its own colour in the frame
            path.write_text(json.dumps({"dmx": dmx, "fixtures": {
                "left": {"on": "yes"}, "center-left": "x",
                "right": {"on": True, "level": 50, "warm": 1, "cold": 2}}}), encoding="utf-8")
            fixtures = NativeControlService(path).stage_state()["fixtures"]
        self.assertEqual(fixtures["center-left"], {"on": True, "level": 99, "warm": 180, "cold": 70})
        # Never written colour channels (0/0): Companion's colour, not black.
        self.assertEqual(fixtures["left"], {"on": False, "level": 0, "warm": 255, "cold": 50})
        self.assertEqual(fixtures["right"], {"on": True, "level": 50, "warm": 1, "cold": 2})


class CorruptFrameTests(unittest.IsolatedAsyncioTestCase):
    """A saved frame that is invalid while the lights are valid: nothing was
    being sent, so every light loads off (keeping its level); the next
    command for one light sends all of them as the page shows them."""

    async def test_lights_load_off_and_the_next_command_renders_all(self):
        frames = []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(native_mod, "_send_artnet_frame", lambda h, u, v: frames.append(list(v))):
            path = Path(folder) / "state.json"
            lit = {"on": True, "level": 200, "warm": 255, "cold": 50}
            path.write_text(json.dumps({"dmx": [7] * 10, "stage_active": True,
                                        "fixtures": {z: dict(lit) for z in ZONES},
                                        "levels": {"fade_ms": 0}}), encoding="utf-8")
            with self.assertLogs("mixer.native_controls", "WARNING"):
                controls = NativeControlService(path)
            state = controls.stage_state()
            self.assertEqual(state["zones"], {z: 0 for z in ZONES})          # the page shows them off
            self.assertEqual({f["level"] for f in state["fixtures"].values()}, {200})
            self.assertFalse(controls.keepalive_status()["active"])
            await controls.set_stage({"left": {"on": True}})
        frame = frames[-1]
        self.assertEqual(channels(frame, "left"), [200, 255, 50])
        for zone in ("center-left", "center-right", "right"):
            self.assertEqual(channels(frame, zone), [0, 255, 50])           # rendered, sent as shown
        self.assertEqual(controls.stage_state()["zones"]["right"], 0)

    async def test_one_light_key_sends_every_light(self):
        frames = []
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(native_mod, "_send_artnet_frame", lambda h, u, v: frames.append(list(v))):
            path = Path(folder) / "state.json"
            path.write_text(json.dumps({"fixtures": {"right": {"on": False, "level": 0, "warm": 9, "cold": 8}},
                                        "levels": {"fade_ms": 0}}), encoding="utf-8")
            controls = NativeControlService(path)
            await controls.press({"type": "artnet-zone-toggle", "zone": "left"})
        self.assertEqual(channels(frames[-1], "right"), [0, 9, 8])


class SceneFormatTests(unittest.TestCase):
    def test_legacy_scenes_become_lights(self):
        scene = _clean_scene({"name": "A", "stage": {"zones": {"left": 90, "right": 0}, "warm": 255, "cold": 50}})
        self.assertEqual(scene["stage"]["fixtures"], {
            "left": {"on": True, "level": 90, "warm": 255, "cold": 50},
            "right": {"on": False, "level": None, "warm": 255, "cold": 50},   # off, keeps its level
        })
        colour = _clean_scene({"name": "B", "stage": {"zones": {}, "warm": 10, "cold": 20}})
        self.assertEqual(set(colour["stage"]["fixtures"]), set(ZONES))
        self.assertEqual(colour["stage"]["fixtures"]["left"], {"on": None, "level": None, "warm": 10, "cold": 20})
        no_colour = _clean_scene({"name": "C", "stage": {"zones": {"left": 5}, "warm": None, "cold": None}})
        self.assertEqual(no_colour["stage"]["fixtures"]["left"], {"on": True, "level": 5, "warm": None, "cold": None})

    def test_new_format_is_checked(self):
        for fixtures in ({"left": {"on": "x"}}, {"left": {"level": 300}}, {"left": {}}, {"nope": {"on": True}},
                         {"left": {"on": True, "hue": 3}}):
            with self.assertRaises(Exception, msg=fixtures):
                _clean_scene({"name": "X", "stage": {"fixtures": fixtures}})

    def test_active_ignores_what_an_off_light_keeps(self):
        scene = _clean_scene({"name": "A", "stage": {"fixtures": {
            "left": {"on": True, "level": 100, "warm": 255, "cold": 50},
            "right": {"on": False, "level": 30, "warm": 1, "cold": 2}}}, "circuits": {"light-tech": True}})
        stage = {"fixtures": {
            "left": {"on": True, "level": 100, "warm": 255, "cold": 50},
            "right": {"on": False, "level": 200, "warm": 9, "cold": 9}}}
        self.assertTrue(scene_is_active(scene, stage, {"light-tech": True}))
        # Unknown (no answer): not known to match, so not active (no flapping mark).
        self.assertFalse(scene_is_active(scene, stage, {"light-tech": None}))
        self.assertFalse(scene_is_active(scene, stage, {}))
        self.assertFalse(scene_is_active(scene, stage, {"light-tech": False}))
        stage["fixtures"]["left"]["cold"] = 51
        self.assertFalse(scene_is_active(scene, stage, {}))


class PresetApiTests(unittest.TestCase):
    def setUp(self):
        self.devices = FakeDevices()
        for p in (mock.patch.object(native_mod, "_send_artnet_frame", self.devices.artnet),
                  mock.patch.object(native_mod, "_shelly_rpc", self.devices.rpc),
                  mock.patch("app.lights.CIRCUIT_CACHE_SECONDS", 0)):
            p.start()
            self.addCleanup(p.stop)
        self.ctx = app_client()
        self.client, self.folder = self.ctx.__enter__()
        self.addCleanup(self.ctx.__exit__, None, None, None)
        self.client.put("/lights/settings", json={"fade_ms": 0})

    def save(self, name, zones=ZONES, circuits=(), **extra):
        r = self.client.post("/lights/scenes", json={"name": name, "lights": {"zones": list(zones), "circuits": list(circuits)}, **extra})
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["scene"]

    def test_preset_saves_each_light_and_applies_it(self):
        self.client.post("/lights/stage", json={"zones": {
            "left": {"on": True, "level": 200, "warm": 255, "cold": 0},
            "center-left": {"on": False, "level": 90},
            "right": {"on": True, "level": 120, "warm": 40, "cold": 255}}})
        scene = self.save("Predigt", zones=["left", "center-left", "right"])
        self.assertEqual(scene["stage"]["fixtures"]["center-left"], {"on": False, "level": 90, "warm": 255, "cold": 50})
        self.client.post("/lights/stage", json={"zones": {z: {"on": True, "level": 10, "warm": 0, "cold": 0} for z in ZONES}})
        self.assertEqual(self.client.post(f"/lights/scenes/{scene['id']}/apply").status_code, 200)
        frame = self.devices.frames[-1]
        self.assertEqual(channels(frame, "left"), [200, 255, 0])
        self.assertEqual(channels(frame, "center-left"), [0, 255, 50])
        self.assertEqual(channels(frame, "right"), [120, 40, 255])
        self.assertEqual(channels(frame, "center-right"), [10, 0, 0])   # not in the preset
        data = self.client.get("/lights").json()
        self.assertEqual(data["stage"]["fixtures"]["center-left"]["level"], 90)
        self.assertEqual(data["last_scene"], scene["id"])
        self.assertEqual([s["active"] for s in data["scenes"]], [True])
        # A light changed by hand: the preset is not active any more.
        self.client.post("/lights/stage", json={"zones": {"left": {"level": 150}}, "fade": False})
        self.assertEqual([s["active"] for s in self.client.get("/lights").json()["scenes"]], [False])

    def test_update_from_current_light_keeps_id_and_place(self):
        a = self.save("A")
        b = self.save("B")
        self.client.post("/lights/stage", json={"zones": {"left": {"on": True, "level": 33}}})
        r = self.client.post("/lights/scenes", json={"id": a["id"], "name": "A neu", "lights": {"zones": ["left"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["scene"]["id"], a["id"])
        scenes = self.client.get("/lights/scenes").json()["scenes"]
        self.assertEqual([(s["id"], s["name"]) for s in scenes], [(a["id"], "A neu"), (b["id"], "B")])
        self.assertEqual(scenes[0]["stage"]["fixtures"], {"left": {"on": True, "level": 33, "warm": 255, "cold": 50}})
        self.assertEqual(self.client.post("/lights/scenes", json={"id": a["id"], "name": "b", "lights": {"zones": ["left"]}}).status_code, 409)
        self.assertEqual(self.client.post("/lights/scenes", json={"id": "nope", "name": "Z", "lights": {"zones": ["left"]}}).status_code, 404)

    def test_invalid_stage_values_are_400_and_send_nothing(self):
        frames = len(self.devices.frames)
        for zones in ({"left": {}}, {"left": {"on": "x"}}, {"left": {"level": 300}}, {"left": {"hue": 1}},
                      {"nope": 1}, {"left": -1}):
            r = self.client.post("/lights/stage", json={"zones": zones})
            self.assertEqual(r.status_code, 400, (zones, r.text))
        self.assertEqual(self.client.post("/lights/stage", json={"warm": 999}).status_code, 400)
        self.assertEqual(len(self.devices.frames), frames)
        # A send that fails is 502, not 400.
        with mock.patch.object(native_mod, "_send_artnet_frame", side_effect=native_mod.NativeControlError("node gone")):
            self.assertEqual(self.client.post("/lights/stage", json={"zones": {"left": 9}}).status_code, 502)

    def test_overwrite_keeps_a_circuit_that_does_not_answer(self):
        self.devices.shelly["light-front"] = True
        scene = self.save("Raum", zones=["left"], circuits=["light-front", "light-tech"])
        self.assertEqual(scene["circuits"], {"light-front": True, "light-tech": True})
        self.devices.offline.add("light-front")
        self.devices.shelly["light-tech"] = False
        r = self.client.post("/lights/scenes", json={"id": scene["id"], "name": "Raum",
                                                     "lights": {"zones": ["left"], "circuits": ["light-front", "light-tech"]}})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["scene"]["circuits"], {"light-front": True, "light-tech": False})
        self.assertEqual((r.json()["kept_circuits"], r.json()["unknown_circuits"]), (["light-front"], []))
        # A new preset cannot keep anything: the silent circuit is left out.
        r = self.client.post("/lights/scenes", json={"name": "Neu", "lights": {"zones": ["left"], "circuits": ["light-front"]}})
        self.assertEqual((r.json()["unknown_circuits"], r.json()["kept_circuits"]), (["light-front"], []))

    def test_order(self):
        ids = [self.save(name)["id"] for name in ("Zeta", "Alpha", "Mitte")]
        self.assertEqual([s["name"] for s in self.client.get("/lights/scenes").json()["scenes"]], ["Zeta", "Alpha", "Mitte"])
        r = self.client.post("/lights/scenes/order", json={"ids": [ids[2], ids[0], ids[1]]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([s["name"] for s in self.client.get("/lights").json()["scenes"]], ["Mitte", "Zeta", "Alpha"])
        stored = json.loads((self.folder / "light_scenes.json").read_text(encoding="utf-8"))
        self.assertEqual([s["name"] for s in stored], ["Mitte", "Zeta", "Alpha"])
        for bad in ([ids[0]], ids + ids[:1], [*ids[:2], "x"], "x", None):
            self.assertEqual(self.client.post("/lights/scenes/order", json={"ids": bad}).status_code, 400, bad)

    def test_legacy_scene_file_is_ordered_by_name_once(self):
        path = self.folder / "legacy.json"
        path.write_text(json.dumps([
            {"id": "z", "name": "Zeta", "stage": {"zones": {"left": 0}, "warm": None, "cold": None}, "circuits": {}},
            {"id": "a", "name": "alpha", "stage": {"zones": {"left": 9}, "warm": None, "cold": None}, "circuits": {}},
        ]), encoding="utf-8")
        store = LightSceneStore(path)
        self.assertEqual([s["id"] for s in asyncio.run(store.list())], ["a", "z"])
        self.assertTrue(all("fixtures" in s["stage"] for s in json.loads(path.read_text(encoding="utf-8"))))

    def test_off_light_in_preset_keeps_its_own_level_when_none_saved(self):
        # A legacy "0" (off) does not overwrite the light's kept level.
        path = self.folder / "light_scenes.json"
        self.client.post("/lights/stage", json={"zones": {"left": 140}})
        scene = _clean_scene({"name": "Alt aus", "stage": {"zones": {"left": 0}, "warm": None, "cold": None}})
        asyncio.run(self.client.app.state.light_scenes.save(scene))
        self.assertTrue(path.exists())
        self.client.post(f"/lights/scenes/{scene['id']}/apply")
        left = self.client.get("/lights").json()["stage"]["fixtures"]["left"]
        self.assertEqual((left["on"], left["level"]), (False, 140))


if __name__ == "__main__":
    unittest.main()
