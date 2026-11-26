"""The Overall keys (app/companion_buttons.py, app/actions.py) against the
Companion config they replace (Companion 4.3, pages 2 and 7).

Companion per Overall key: the four stage zones to a fixed pattern (it
presses a zone's toggle only when the zone is not as wanted yet), then the
camera that is NOT live drives to its preset, 4 s later it goes live, then
POST /groups/<group>/unmute to this service. Devices are faked here."""

import asyncio
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

from app import actions
from app.companion_buttons import COMPANION_BUTTONS, COMPANION_BUTTONS_BY_ID, DEFAULT_CATEGORIES, CompanionButtonStore
from app.camera_program import CameraProgram
from app.native_controls import NativeControlError

# Companion: zones on (the others off), Cam1 preset, Cam2 preset, mute group,
# extra key. Zones: left (Links), center-left (Klavier), center-right (Logo), right.
COMPANION_OVERALL = {
    "gemeinsam-singen": (["center-left"], 5, 5, "GemeinsamSingen", None),
    "predigt": (["left", "right"], 1, 1, "Predigt", None),
    "musik-klavier": (["center-left"], 3, 3, "Klavier", None),
    "pult-klavier": (["left", "center-left", "right"], 2, 2, "PultKlavier", None),
    "pult-playback": (["left", "right"], 2, 2, "PultPlayback", None),
    "handmik-klavier": (["left", "center-left", "right"], 5, 2, "HandmikKlavier", None),
    "handmik-playback": (["left", "right"], 5, 2, "HandmikPlayback", None),
    "hintergrundmusik": ([], 5, 5, "Hintergrundmusik", None),
    "gebet": (["left", "right"], 8, 8, "Gebet", None),
    "qr": (["right"], 5, 5, "Hintergrundmusik", None),
    "mission-video": ([], None, None, "Missionsvideo", "native-obs-pc2-live"),
    "instrumental": (["center-left"], 3, 5, "Instrumental", None),
    "ende": (["center-right"], None, None, "Klavier", "native-obs-end"),
}
ZONES = ["left", "center-left", "center-right", "right"]


class CatalogMatchesCompanionTests(unittest.TestCase):
    def test_every_companion_overall_key_is_in_the_catalog(self):
        overall = {b["id"].removeprefix("native-overall-") for b in COMPANION_BUTTONS if b["category_id"] == "overall"}
        self.assertEqual(overall, set(COMPANION_OVERALL))

    def test_zones_cameras_group_and_keys_as_in_companion(self):
        for key, (zones_on, cam1, cam2, group, extra) in COMPANION_OVERALL.items():
            with self.subTest(key=key):
                action = COMPANION_BUTTONS_BY_ID[f"native-overall-{key}"]["native_action"]
                self.assertEqual(action["type"], "group-macro")
                self.assertEqual(action["group"], group)
                self.assertEqual(sorted(action["stage"]["on"]), sorted(zones_on))
                self.assertEqual(sorted(action["stage"]["off"]), sorted(z for z in ZONES if z not in zones_on))
                if cam1 is None:
                    self.assertNotIn("cameras", action)
                else:
                    self.assertEqual(action["cameras"], {"cam1": cam1, "cam2": cam2})
                    self.assertEqual(action["camera_wait_seconds"], 4.0)
                self.assertEqual(action["buttons"], [extra] if extra else [])

    def test_room_light_keys_leave_the_technik_circuit_alone(self):
        for key in ("native-shelly-all-on", "native-shelly-all-off"):
            self.assertNotIn("light-tech", COMPANION_BUTTONS_BY_ID[key]["native_action"]["targets"])
        self.assertEqual(sorted(COMPANION_BUTTONS_BY_ID["native-shelly-center-on"]["native_action"]["targets"]),
                         ["light-center-left", "light-center-right", "light-front"])


class FakeCameraProgram:
    def __init__(self, answer=None):
        self.calls = []
        self.cancelled = 0
        self.answer = answer or {"ok": True, "message": "Cam 2 fährt Preset 5 an, Umschalten in 4 s"}
        self.status = {"seq": 0}

    async def run(self, presets, wait, title="", scenes=None, switch_if_none_live=None, token=None):
        self.calls.append((presets, wait, switch_if_none_live))
        return dict(self.answer)

    def cancel(self):
        self.cancelled += 1
        return self.cancelled


class Rig:
    def __init__(self, group_ok=True, light_error=None, failing=(), camera_answer=None, light_gate=None):
        self.events = []
        self.camera = FakeCameraProgram(camera_answer)
        self.warn_scenes = set()
        self.light_gate = light_gate

        async def group_set(group, muted):
            self.events.append(("group", group, muted))
            return {"ok": True} if group_ok else {"ok": False, "reason": "unsafe group operation"}

        async def press(action):
            if action["type"] == "artnet-zones-set":
                self.events.append(("light", sorted(action["on"]), sorted(action["off"])))
                if self.light_gate is not None:
                    await self.light_gate.wait()
                    self.events.append(("light done",))
                if light_error:
                    raise NativeControlError(light_error)
                return {"ok": True}
            self.events.append(("key", action.get("scene")))
            if action.get("scene") in failing:
                raise NativeControlError("OBS WebSocket unavailable")
            if action.get("scene") in self.warn_scenes:
                return {"ok": True, "warning": f"OBS-Quelle fehlt in Szene „{action['scene']}“"}
            return {"ok": True}

        self.request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(
            controller=SimpleNamespace(group_set=group_set),
            native_controls=SimpleNamespace(press=press),
            camera_program=self.camera,
        )))

    async def press(self, key):
        button_id = f"native-overall-{key}"
        return await actions.press_native(self.request, button_id, COMPANION_BUTTONS_BY_ID[button_id]["native_action"])


class OverallRunTests(unittest.IsolatedAsyncioTestCase):
    async def test_group_light_and_off_air_camera(self):
        rig = Rig()
        result, status = await rig.press("gemeinsam-singen")
        self.assertEqual(status, 200, result)
        self.assertTrue(result["ok"])
        self.assertEqual(rig.events[0], ("group", "GemeinsamSingen", False))
        self.assertIn(("light", ["center-left"], ["center-right", "left", "right"]), rig.events)
        # Only one call: CameraProgram moves the camera that is off air and
        # cuts to it after the wait (no cut to a moving camera).
        self.assertEqual(rig.camera.calls, [({"cam1": 5, "cam2": 5}, 4.0, "cam1")])
        self.assertIn("Umschalten in 4 s", result["camera"]["message"])

    async def test_blocked_group_changes_nothing(self):
        rig = Rig(group_ok=False)
        result, status = await rig.press("predigt")
        self.assertEqual(status, 409)
        self.assertFalse(result["ok"])
        self.assertEqual(rig.events, [("group", "Predigt", False)])
        self.assertEqual(rig.camera.calls, [])

    async def test_light_fades_while_the_cameras_run(self):
        gate = asyncio.Event()
        rig = Rig(light_gate=gate)
        press = asyncio.create_task(rig.press("predigt"))
        for _ in range(5):
            await asyncio.sleep(0)
        self.assertEqual(len(rig.camera.calls), 1, "cameras do not wait for the light fade")
        self.assertFalse(press.done(), "the answer waits for the light")
        gate.set()
        result, status = await press
        self.assertEqual(status, 200, result)
        self.assertEqual(rig.events[-1], ("light done",))

    async def test_camera_failure_does_not_stop_the_light(self):
        rig = Rig(camera_answer={"ok": False, "reason": "OBS nicht erreichbar, keine Kamera bewegt"})
        result, status = await rig.press("predigt")
        self.assertEqual(status, 502)
        self.assertFalse(result["ok"])
        self.assertIn("cameras: OBS nicht erreichbar", result["reason"])
        self.assertIn(("light", ["left", "right"], ["center-left", "center-right"]), rig.events)

    async def test_light_failure_does_not_stop_the_keys(self):
        rig = Rig(light_error="Art-Net send failed")
        result, status = await rig.press("ende")
        self.assertEqual(status, 502)
        self.assertIn("light: Art-Net send failed", result["reason"])
        self.assertIn(("key", "Ende (neu)"), rig.events)

    async def test_blocked_group_drops_a_waiting_cut_but_moves_nothing(self):
        rig = Rig(group_ok=False)
        await rig.press("gebet")
        self.assertEqual(rig.camera.cancelled, 1)
        self.assertEqual(rig.camera.calls, [])

    async def test_key_overall_cancels_a_waiting_camera_cut(self):
        rig = Rig()
        result, status = await rig.press("mission-video")
        self.assertEqual(status, 200, result)
        self.assertEqual(rig.camera.cancelled, 1)
        self.assertEqual(rig.camera.calls, [])
        self.assertIn(("key", "PC2"), rig.events)
        self.assertIn(("light", [], sorted(ZONES)), rig.events)

    async def test_manual_obs_key_cancels_a_waiting_camera_cut(self):
        rig = Rig()
        for button_id in ("native-obs-picture", "native-obs-cam1-live", "native-obs-bible-cam2"):
            await actions.press_native(rig.request, button_id, COMPANION_BUTTONS_BY_ID[button_id]["native_action"])
        self.assertEqual(rig.camera.cancelled, 3)
        await actions.press_native(rig.request, "native-obs-cam1-toggle",
                                   COMPANION_BUTTONS_BY_ID["native-obs-cam1-toggle"]["native_action"])
        await actions.press_native(rig.request, "beamer-hide", COMPANION_BUTTONS_BY_ID["beamer-hide"]["native_action"])
        self.assertEqual(rig.camera.cancelled, 3, "keys that do not change the program leave it alone")

    async def test_warning_of_its_obs_key_reaches_the_page(self):
        rig = Rig()
        rig.warn_scenes.add("PC2")
        result, status = await rig.press("mission-video")
        self.assertEqual(status, 200, result)
        self.assertIn("PC2", result["warning"])

    async def test_failing_key_is_reported(self):
        rig = Rig(failing={"Ende (neu)"})
        result, status = await rig.press("ende")
        self.assertEqual(status, 502)
        self.assertIn("Ende: OBS WebSocket unavailable", result["reason"])
        self.assertIn(("light", ["center-right"], ["center-left", "left", "right"]), rig.events)

    async def test_without_camera_program_the_rest_still_runs(self):
        rig = Rig()
        rig.request.app.state.camera_program = None
        result, status = await rig.press("gebet")
        self.assertEqual(status, 502)
        self.assertIn("camera presets unavailable", result["reason"])
        self.assertIn(("light", ["left", "right"], ["center-left", "center-right"]), rig.events)


class PressOrderTests(unittest.IsolatedAsyncioTestCase):
    """With the real CameraProgram: the key pressed last wins, even if an
    Overall pressed before it only gets to its cameras later (setting the
    mute group takes a moment)."""

    def _rig(self, live):
        rig = Rig()
        self.moves = []
        self.cut_gate = asyncio.Event()
        self.group_gates = {}

        on_air = set(live)

        async def read_live():
            await asyncio.sleep(0)
            return set(on_air)

        async def recall(cam, preset):
            self.moves.append(("recall", cam, preset))

        async def switch(cam):
            self.moves.append(("switch", cam))
            on_air.clear()
            on_air.add(cam)

        async def transition():
            return 0.0

        async def sleep(seconds):
            await self.cut_gate.wait()

        async def group_set(group, muted):
            gate = self.group_gates.get(group)
            if gate is not None:
                await gate.wait()
            return {"ok": True}

        rig.camera = CameraProgram(read_live, recall, switch, transition, sleep)
        rig.request.app.state.camera_program = rig.camera
        rig.request.app.state.controller = SimpleNamespace(group_set=group_set)
        return rig

    async def _spin(self):
        for _ in range(60):
            await asyncio.sleep(0)

    async def test_obs_key_while_the_mutes_are_set_stops_the_cameras(self):
        rig = self._rig(live=[])
        self.group_gates["Predigt"] = asyncio.Event()
        overall = asyncio.create_task(rig.press("predigt"))
        await self._spin()
        await actions.press_native(rig.request, "native-obs-picture",
                                   COMPANION_BUTTONS_BY_ID["native-obs-picture"]["native_action"])
        self.group_gates["Predigt"].set()
        result, status = await overall
        self.cut_gate.set()
        await self._spin()
        self.assertEqual(status, 200, result)
        self.assertEqual(self.moves, [], "Bild was pressed last: no camera moves, no cut")

    async def test_later_overall_wins_even_if_its_mutes_are_faster(self):
        rig = self._rig(live=["cam1"])
        self.group_gates["Predigt"] = asyncio.Event()
        first = asyncio.create_task(rig.press("predigt"))   # presets 1, mutes take long
        await self._spin()
        await rig.press("gebet")                            # presets 8
        self.group_gates["Predigt"].set()
        await first
        self.cut_gate.set()
        await self._spin()
        self.assertEqual(self.moves, [("recall", "cam2", 8), ("switch", "cam2"), ("recall", "cam1", 8)])


class CatalogV9MigrationTests(unittest.TestCase):
    def _migrate(self, buttons, version):
        stored = {"categories": deepcopy(DEFAULT_CATEGORIES), "buttons": buttons, "native_catalog_version": version}
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "buttons.json"
            path.write_text(json.dumps(stored), encoding="utf-8")
            CompanionButtonStore(path, COMPANION_BUTTONS)
            migrated = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(list(Path(folder).glob("*.corrupt*")), [])
        return migrated

    def test_v8_macros_get_the_companion_pattern_and_keep_a_renamed_group(self):
        old = {"id": "native-overall-predigt", "label": "Predigt!", "kind": "native", "category_id": "overall",
               "native_action": {"type": "group-macro", "group": "Kanzel",
                                 "light_zones_on": ZONES, "buttons": ["native-cam1-preset-1", "native-obs-cam1-live"]}}
        migrated = self._migrate([old], 8)
        by_id = {b["id"]: b for b in migrated["buttons"]}
        self.assertEqual(migrated["native_catalog_version"], 10)
        action = by_id["native-overall-predigt"]["native_action"]
        self.assertEqual(action["group"], "Kanzel")
        self.assertEqual(by_id["native-overall-predigt"]["label"], "Predigt!")
        self.assertNotIn("light_zones_on", action)
        self.assertEqual(action["cameras"], {"cam1": 1, "cam2": 1})
        for added in ("native-overall-ende", "native-obs-bible-cam1", "native-obs-droidcam-live", "native-obs-picture"):
            self.assertIn(added, by_id)

    def test_current_catalog_does_not_bring_back_deleted_keys(self):
        migrated = self._migrate([], 10)
        self.assertEqual(migrated["buttons"], [])


if __name__ == "__main__":
    unittest.main()
