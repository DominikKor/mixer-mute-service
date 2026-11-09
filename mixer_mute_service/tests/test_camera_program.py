"""Camera presets of program rows (app/camera_program.py): a preset is only
ever recalled on a camera that is off air.

OBS and the cameras are faked: the live state is a script of answers, the
recalls and switches are recorded."""

import asyncio
import tempfile
import unittest
from pathlib import Path

from app.camera_program import CameraProgram, _live_cameras_blocking, camera_of
from app.native_controls import NativeControlError
from app.program_items import ProgramItemError, ProgramItemStore

from tests.helpers import app_client


def fake_obs(program_scene, scenes, groups=None):
    """An OBS answering GetCurrentProgramScene and the item lists."""
    def obs(request_type, data=None):
        if request_type == "GetCurrentProgramScene":
            return {"currentProgramSceneName": program_scene}
        if request_type == "GetSceneItemList":
            return {"sceneItems": scenes.get(data["sceneName"], [])}
        if request_type == "GetGroupSceneItemList":
            return {"sceneItems": (groups or {}).get(data["sceneName"], [])}
        raise AssertionError(request_type)
    return obs


def item(name, enabled=True, scene=False, group=False):
    return {"sourceName": name, "sceneItemEnabled": enabled, "isGroup": group,
            "sourceType": "OBS_SOURCE_TYPE_SCENE" if scene else "OBS_SOURCE_TYPE_INPUT"}


class LiveDetectionTests(unittest.TestCase):
    def test_camera_names(self):
        for name, cam in (("Cam1", "cam1"), ("Cam 2", "cam2"), ("Cam2_klein", "cam2"), ("CAM-1 NDI", "cam1"),
                          ("cam_2", "cam2"), ("Cam12", None), ("Camera", None), ("PC2", None), (None, None)):
            self.assertEqual(camera_of(name), cam, name)

    def test_camera_scene_means_that_camera_only(self):
        # The "Cam N Live" keys switch sources in the current scene, so the
        # other camera's source may be enabled in it too.
        obs = fake_obs("Cam 1", {"Cam 1": [item("Cam1"), item("Cam2")]})
        self.assertEqual(_live_cameras_blocking(obs), {"cam1"})

    def test_sources_and_nested_scenes_of_other_scenes(self):
        scenes = {
            "Preview Scene": [item("Cam1_klein"), item("Cam2_klein", enabled=False), item("Beamer")],
            "Gottesdienst": [item("Titel"), item("Cam 2", scene=True)],
            "Rahmen": [item("Bauchbinde", group=True)],
            "Willkommen": [item("Logo"), item("Cam1", enabled=False)],
        }
        groups = {"Bauchbinde": [item("Cam1")]}
        self.assertEqual(_live_cameras_blocking(fake_obs("Preview Scene", scenes)), {"cam1"})
        self.assertEqual(_live_cameras_blocking(fake_obs("Gottesdienst", scenes)), {"cam2"})
        self.assertEqual(_live_cameras_blocking(fake_obs("Rahmen", scenes, groups)), {"cam1"})
        self.assertEqual(_live_cameras_blocking(fake_obs("Willkommen", scenes)), set())

    def test_scene_loops_end(self):
        scenes = {"A": [item("B", scene=True)], "B": [item("A", scene=True), item("Cam2")]}
        self.assertEqual(_live_cameras_blocking(fake_obs("A", scenes)), {"cam2"})


class Rig:
    """A CameraProgram on scripted OBS answers; records what was done."""

    def __init__(self, live_answers, gate=None):
        self.live_answers = list(live_answers)
        self.events = []
        self.gate = gate  # an Event the wait blocks on, if given

        async def read_live():
            answer = self.live_answers.pop(0)
            if isinstance(answer, Exception):
                raise answer
            self.events.append(("read", tuple(sorted(answer))))
            return set(answer)

        async def recall(cam, preset):
            self.events.append(("recall", cam, preset))

        async def switch(cam):
            self.events.append(("switch", cam))

        async def transition():
            return 0.3

        async def sleep(seconds):
            self.events.append(("sleep", seconds))
            if self.gate is not None:
                await self.gate.wait()

        self.program = CameraProgram(read_live, recall, switch, transition, sleep)

    async def settle(self):
        task = self.program._task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def moves(self):
        return [event for event in self.events if event[0] in ("recall", "switch")]


class SequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_off_air_camera_first_then_switch_then_the_other(self):
        rig = Rig([{"cam1"}, {"cam1"}, {"cam2"}])
        result = await rig.program.run({"cam1": 3, "cam2": 5}, 4)
        self.assertTrue(result["ok"])
        self.assertEqual(result["switch_to"], "cam2")
        self.assertEqual(rig.moves(), [("recall", "cam2", 5)], "only the off-air camera moves at once")
        self.assertEqual(rig.program.status["state"], "waiting")
        await rig.settle()
        self.assertEqual(rig.moves(), [("recall", "cam2", 5), ("switch", "cam2"), ("recall", "cam1", 3)])
        self.assertIn(("sleep", 4.0), rig.events)
        self.assertIn(("sleep", 0.8), rig.events, "the old camera moves only after the transition")
        self.assertEqual(rig.program.status["state"], "done")

    async def test_only_the_off_air_preset_switches_and_stops(self):
        rig = Rig([{"cam2"}, {"cam2"}])
        await rig.program.run({"cam1": 2, "cam2": None}, 5)
        await rig.settle()
        self.assertEqual(rig.moves(), [("recall", "cam1", 2), ("switch", "cam1")])

    async def test_preset_for_the_live_camera_alone_is_skipped(self):
        rig = Rig([{"cam1"}])
        result = await rig.program.run({"cam1": 3}, 5)
        await rig.settle()
        self.assertTrue(result["ok"])
        self.assertEqual(result["skipped"], ["cam1"])
        self.assertEqual(rig.moves(), [])

    async def test_no_camera_live_moves_both_and_does_not_switch(self):
        rig = Rig([set()])
        result = await rig.program.run({"cam1": 1, "cam2": 4}, 5)
        await rig.settle()
        self.assertEqual(result["switch_to"], None)
        self.assertEqual(rig.moves(), [("recall", "cam1", 1), ("recall", "cam2", 4)])

    async def test_both_live_moves_nothing(self):
        rig = Rig([{"cam1", "cam2"}])
        result = await rig.program.run({"cam1": 1, "cam2": 4}, 5)
        self.assertTrue(result["ok"])
        self.assertEqual(rig.moves(), [])

    async def test_obs_unreachable_moves_nothing(self):
        rig = Rig([NativeControlError("OBS WebSocket unavailable")])
        result = await rig.program.run({"cam1": 1, "cam2": 4}, 5)
        self.assertFalse(result["ok"])
        self.assertIn("OBS", result["reason"])
        self.assertEqual(rig.moves(), [])
        self.assertEqual(rig.program.status["state"], "failed")

    async def test_obs_switched_by_hand_during_the_wait(self):
        rig = Rig([{"cam1"}, {"cam2"}])
        await rig.program.run({"cam1": 3, "cam2": 5}, 5)
        await rig.settle()
        self.assertEqual(rig.moves(), [("recall", "cam2", 5)], "no switch, and cam1 is not moved")
        self.assertEqual(rig.program.status["state"], "aborted")

    async def test_old_camera_back_on_air_after_the_switch(self):
        rig = Rig([{"cam1"}, {"cam1"}, {"cam1"}])
        await rig.program.run({"cam1": 3, "cam2": 5}, 5)
        await rig.settle()
        self.assertEqual(rig.moves(), [("recall", "cam2", 5), ("switch", "cam2")])
        self.assertEqual(rig.program.status["state"], "aborted")

    async def test_obs_lost_in_the_background_stops_there(self):
        rig = Rig([{"cam1"}, NativeControlError("OBS WebSocket unavailable")])
        await rig.program.run({"cam1": 3, "cam2": 5}, 5)
        await rig.settle()
        self.assertEqual(rig.moves(), [("recall", "cam2", 5)])
        self.assertEqual(rig.program.status["state"], "failed")

    async def test_next_row_cancels_a_waiting_switch(self):
        gate = asyncio.Event()
        rig = Rig([{"cam1"}, {"cam1"}, {"cam1"}], gate=gate)
        await rig.program.run({"cam1": 3, "cam2": 5}, 5)
        first = rig.program._task
        await asyncio.sleep(0)  # the first row is now waiting
        await rig.program.run({"cam2": 6}, 5)
        await asyncio.sleep(0)
        self.assertTrue(first.cancelled())
        gate.set()
        await rig.settle()
        self.assertEqual(rig.moves(), [("recall", "cam2", 5), ("recall", "cam2", 6), ("switch", "cam2")],
                         "only the second row switches, once")

    async def test_wait_is_clamped_and_empty_rows_do_nothing(self):
        rig = Rig([{"cam1"}, {"cam1"}])
        result = await rig.program.run({"cam2": 5}, 999)
        self.assertEqual(result["switch_in_seconds"], 30.0)
        await rig.settle()
        empty = Rig([])
        self.assertEqual((await empty.program.run({"cam1": None, "cam2": 0}, 5))["applied"], [])
        self.assertEqual(empty.events, [], "OBS is not even asked")


def row(**extra):
    return {"time": "10:00", "title": "Predigt", "person": "", "group": "", **extra}


class StoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_presets_and_wait_are_validated(self):
        with tempfile.TemporaryDirectory() as name:
            store = ProgramItemStore(Path(name) / "program.json")
            created = await store.create(row(cam1_preset=3, cam2_preset="5", camera_wait_seconds="4,5"))
            self.assertEqual((created["cam1_preset"], created["cam2_preset"], created["camera_wait_seconds"]), (3, 5, 4.5))
            plain = await store.create(row())
            self.assertEqual((plain["cam1_preset"], plain["cam2_preset"], plain["camera_wait_seconds"]), (None, None, 5.0))
            for bad in ({"cam1_preset": 17}, {"cam2_preset": -1}, {"cam1_preset": "x"}, {"cam1_preset": True},
                        {"camera_wait_seconds": 0.2}, {"camera_wait_seconds": 31}, {"camera_wait_seconds": "lang"}):
                with self.assertRaises(ProgramItemError, msg=bad):
                    await store.create(row(**bad))
            # Rows saved before this feature load with no presets.
            (Path(name) / "old.json").write_text(
                '[{"id": "a", "time": "09:00", "title": "Alt", "person": "", "group": ""}]', encoding="utf-8")
            old = await ProgramItemStore(Path(name) / "old.json").get("a")
            self.assertEqual((old["cam1_preset"], old["cam2_preset"], old["camera_wait_seconds"]), (None, None, 5.0))


class FakeCameraProgram:
    def __init__(self):
        self.calls = []
        self.status = {"seq": 3, "state": "waiting", "message": "Cam 2 fährt Preset 5 an", "at": 1.0}

    async def run(self, presets, wait, title=""):
        self.calls.append((presets, wait, title))
        return {"ok": True, "applied": ["cam2"], "switch_to": "cam2"}


class ApiTests(unittest.TestCase):
    def test_row_with_presets_runs_them_after_its_actions(self):
        with app_client() as (client, _folder):
            fake = FakeCameraProgram()
            client.app.state.camera_program = fake
            created = client.post("/program/items", json=row(cam1_preset=3, cam2_preset=5, camera_wait_seconds=6))
            self.assertEqual(created.status_code, 200, created.text)
            item_id = created.json()["item"]["id"]
            self.assertEqual(client.post("/program/items", json=row(cam1_preset=20)).status_code, 400)
            played = client.post(f"/program/items/{item_id}/play").json()
            self.assertTrue(played["ok"], played)
            self.assertEqual(played["camera"]["switch_to"], "cam2")
            self.assertEqual(fake.calls, [({"cam1": 3, "cam2": 5}, 6.0, "Predigt")])
            self.assertEqual(client.get("/program/status").json()["camera"]["seq"], 3)

            plain = client.post("/program/items", json=row(title="Lied")).json()["item"]["id"]
            played = client.post(f"/program/items/{plain}/play").json()
            self.assertIsNone(played["camera"])
            self.assertEqual(len(fake.calls), 1, "a row without presets does not touch the cameras")


if __name__ == "__main__":
    unittest.main()
