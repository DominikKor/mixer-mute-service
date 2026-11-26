"""Camera presets of program rows (app/camera_program.py): a preset is only
ever recalled on a camera that is off air.

OBS and the cameras are faked: the live state is a script of answers, the
recalls and switches are recorded."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.camera_program import CameraProgram, _live_cameras_blocking, camera_of, obs_scene_names
from app.native_controls import NativeControlError
from app.program_items import ProgramItemError, ProgramItemStore
from app.shortcuts import ShortcutStore

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

    def __init__(self, live_answers, gate=None, read_yields=0):
        self.live_answers = list(live_answers)
        self.events = []
        self.gate = gate  # an Event the wait blocks on, if given

        async def read_live():
            for _ in range(read_yields):  # OBS takes a moment: other presses get in between
                await asyncio.sleep(0)
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

        async def switch_scene(scene):
            self.events.append(("scene", scene))

        self.program = CameraProgram(read_live, recall, switch, transition, sleep, switch_scene)

    async def settle(self):
        task = self.program._task
        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                pass

    def moves(self):
        return [event for event in self.events if event[0] in ("recall", "switch", "scene")]


async def spin(rounds=60):
    for _ in range(rounds):
        await asyncio.sleep(0)


class QuickPressTests(unittest.IsolatedAsyncioTestCase):
    """Two Overall keys pressed at once, or an OBS key while a sequence is
    still reading OBS: only the newest sequence may act, and every cancel
    must reach it (no orphaned background task)."""

    async def test_second_key_wins_while_a_camera_is_live(self):
        gate = asyncio.Event()
        rig = Rig([{"cam1"}] * 3 + [{"cam2"}] * 4, gate=gate, read_yields=3)
        # Cam 1 live: Predigt (presets 1) and at once Gebet (presets 8).
        await asyncio.gather(rig.program.run({"cam1": 1, "cam2": 1}, 4), rig.program.run({"cam1": 8, "cam2": 8}, 4))
        gate.set()
        await spin()
        moves = rig.moves()
        self.assertEqual(moves.count(("switch", "cam2")), 1, moves)
        self.assertIn(("recall", "cam1", 8), moves)
        self.assertNotIn(("recall", "cam1", 1), moves, "Cam 1 must end on Gebet's preset")

    async def test_obs_key_after_two_quick_keys_stops_the_cut(self):
        gate = asyncio.Event()
        rig = Rig([set()] * 6, gate=gate, read_yields=3)
        await asyncio.gather(rig.program.run({"cam1": 5, "cam2": 5}, 4, switch_if_none_live="cam1"),
                             rig.program.run({"cam1": 1, "cam2": 1}, 4, switch_if_none_live="cam1"))
        rig.program.cancel()  # "Bild" pressed
        gate.set()
        await spin()
        self.assertNotIn(("switch", "cam1"), rig.moves())

    async def test_cancel_while_a_sequence_reads_obs(self):
        gate = asyncio.Event()
        rig = Rig([{"cam1"}] * 3, gate=gate, read_yields=3)
        running = asyncio.create_task(rig.program.run({"cam1": 3, "cam2": 5}, 4))
        await asyncio.sleep(0)
        rig.program.cancel()
        result = await running
        gate.set()
        await spin()
        self.assertEqual(rig.moves(), [("recall", "cam2", 5)], "moved off air, but no cut afterwards")
        self.assertEqual(rig.program.status["state"], "aborted")
        self.assertIsNone(result.get("switch_to"))


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

    async def test_no_camera_live_with_fallback_moves_both_then_cuts_to_it(self):
        # The Overall keys: like Companion they always end on a camera.
        rig = Rig([set(), set()])
        result = await rig.program.run({"cam1": 1, "cam2": 4}, 4, switch_if_none_live="cam1")
        self.assertEqual(result["switch_to"], "cam1")
        self.assertEqual(rig.moves(), [("recall", "cam1", 1), ("recall", "cam2", 4)], "the cut waits")
        await rig.settle()
        self.assertEqual(rig.moves(), [("recall", "cam1", 1), ("recall", "cam2", 4), ("switch", "cam1")])
        self.assertIn(("sleep", 4.0), rig.events)
        self.assertEqual(rig.program.status["state"], "done")

    async def test_fallback_cut_skipped_when_obs_changed_meanwhile(self):
        rig = Rig([set(), {"cam2"}])
        await rig.program.run({"cam1": 1, "cam2": 4}, 4, switch_if_none_live="cam1")
        await rig.settle()
        self.assertNotIn(("switch", "cam1"), rig.moves())
        self.assertEqual(rig.program.status["state"], "aborted")

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


class SceneTests(unittest.IsolatedAsyncioTestCase):
    async def test_scene_of_the_moved_camera_replaces_the_switch(self):
        rig = Rig([{"cam1"}, {"cam1"}, {"cam2"}])
        result = await rig.program.run({"cam1": 3, "cam2": 5}, 4, scenes={"cam1": "Kanzel", "cam2": "Predigt"})
        self.assertEqual(rig.moves(), [("recall", "cam2", 5)], "the scene waits for the camera")
        self.assertEqual(result["scene"], "Predigt")
        await rig.settle()
        self.assertEqual(rig.moves(), [("recall", "cam2", 5), ("scene", "Predigt"), ("recall", "cam1", 3)])
        self.assertEqual(rig.program.status["state"], "done")

    async def test_other_cameras_scene_is_not_used(self):
        rig = Rig([{"cam1"}, {"cam1"}])
        await rig.program.run({"cam2": 5}, 4, scenes={"cam1": "Kanzel"})
        await rig.settle()
        self.assertEqual(rig.moves(), [("recall", "cam2", 5), ("switch", "cam2")])

    async def test_scene_without_preset_does_nothing(self):
        rig = Rig([])
        result = await rig.program.run({"cam1": None, "cam2": None}, 5, scenes={"cam1": "Ende (neu)"})
        self.assertTrue(result["ok"])
        self.assertEqual(rig.events, [], "OBS is not even asked")

    async def test_no_camera_live_scene_after_the_wait(self):
        rig = Rig([set(), set()])
        result = await rig.program.run({"cam1": 1, "cam2": 4}, 6, scenes={"cam2": "Gottesdienst"})
        self.assertEqual(result["switch_in_seconds"], 6.0)
        self.assertEqual(rig.moves(), [("recall", "cam1", 1), ("recall", "cam2", 4)])
        await rig.settle()
        self.assertEqual(rig.moves()[-1], ("scene", "Gottesdienst"))
        self.assertIn(("sleep", 6.0), rig.events)

    async def test_no_camera_live_scene_dropped_when_obs_changed(self):
        rig = Rig([set(), {"cam2"}])
        await rig.program.run({"cam1": 1}, 5, scenes={"cam1": "Gottesdienst"})
        await rig.settle()
        self.assertEqual(rig.moves(), [("recall", "cam1", 1)])
        self.assertEqual(rig.program.status["state"], "aborted")

    async def test_live_camera_preset_skipped_scene_too(self):
        rig = Rig([{"cam1"}])
        result = await rig.program.run({"cam1": 3}, 5, scenes={"cam1": "Lied"})
        self.assertTrue(result["ok"])
        self.assertEqual(rig.moves(), [])

    async def test_scene_switch_failing_is_reported(self):
        rig = Rig([{"cam1"}, {"cam1"}])

        async def broken(scene):
            raise NativeControlError("OBS WebSocket unavailable")

        rig.program._switch_scene = broken
        result = await rig.program.run({"cam2": 5}, 5, scenes={"cam2": "Lied"})
        self.assertTrue(result["ok"])
        await rig.settle()
        self.assertEqual(rig.program.status["state"], "failed")

    def test_scene_names_top_first(self):
        def obs(request_type, data=None):
            self.assertEqual(request_type, "GetSceneList")
            return {"scenes": [{"sceneName": "Ende"}, {"sceneName": "Predigt"}, {"sceneName": "Start"}, {}]}
        self.assertEqual(obs_scene_names(obs), ["Start", "Predigt", "Ende"])


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
            self.assertEqual((old["desktop"], old["cam1_scene"], old["shortcuts"]), ([], None, []))

    async def test_desktop_and_scene(self):
        with tempfile.TemporaryDirectory() as name:
            store = ProgramItemStore(Path(name) / "program.json")
            created = await store.create(row(desktop=["qr", "bild-2", "qr"], cam2_scene="  Predigt "))
            self.assertEqual((created["desktop"], created["cam1_scene"], created["cam2_scene"]),
                             (["qr", "bild-2"], None, "Predigt"))
            self.assertEqual((await store.create(row(cam1_scene="")))["cam1_scene"], None)
            for bad in ({"desktop": "qr"}, {"desktop": ["a b"]}, {"desktop": [f"p{i}" for i in range(9)]},
                        {"cam1_scene": "x" * 101}, {"cam2_scene": "a\nb"}, {"cam1_scene": 3},
                        {"shortcuts": ["nope"]}, {"shortcuts": "a" * 32}):
                with self.assertRaises(ProgramItemError, msg=bad):
                    await store.create(row(**bad))

    async def test_shortcut_store(self):
        with tempfile.TemporaryDirectory() as name:
            store = ShortcutStore(Path(name) / "shortcuts.json")
            created = await store.create({"title": "Predigt", "group": "Pult", "cam1_preset": 3, "time": "10:00"})
            self.assertNotIn("time", created)
            self.assertEqual((created["group"], created["cam1_preset"]), ("Pult", 3))
            with self.assertRaises(ProgramItemError):
                await store.create({"title": ""})
            copy = await store.duplicate(created["id"])
            self.assertEqual(copy["title"], "Predigt (Kopie)")
            self.assertNotEqual(copy["id"], created["id"])
            updated = await store.update(copy["id"], {"title": "Lied", "beamer": "hide"})
            self.assertEqual((updated["title"], updated["group"], updated["beamer"]), ("Lied", "", "hide"))
            self.assertEqual(await store.rename_group("Pult", "Kanzel"), 1)
            reloaded = ShortcutStore(Path(name) / "shortcuts.json")
            self.assertEqual([s["title"] for s in await reloaded.list()], ["Predigt", "Lied"])
            self.assertEqual((await reloaded.get(created["id"]))["group"], "Kanzel")
            self.assertTrue(await store.delete(copy["id"]))
            self.assertIsNone(await store.update("missing", {"title": "X"}))


class FakeCameraProgram:
    def __init__(self):
        self.calls = []
        self.scenes = []
        self.status = {"seq": 3, "state": "waiting", "message": "Cam 2 fährt Preset 5 an", "at": 1.0}

    async def run(self, presets, wait, title="", scenes=None, switch_if_none_live=None):
        self.calls.append((presets, wait, title))
        self.scenes.append(scenes)
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

            with_scene = client.post("/program/items", json=row(title="Ende", cam2_preset=4, cam2_scene="Ende (neu)")).json()
            played = client.post(f"/program/items/{with_scene['item']['id']}/play").json()
            self.assertTrue(played["ok"], played)
            self.assertEqual(fake.scenes[-1], {"cam1": None, "cam2": "Ende (neu)"})
            scene_only = client.post("/program/items", json=row(title="Nur Szene", cam1_scene="X")).json()
            calls = len(fake.calls)
            client.post(f"/program/items/{scene_only['item']['id']}/play")
            self.assertEqual(len(fake.calls), calls, "a scene without its preset does not touch the cameras")

    def test_desktop_profiles_run_in_order_and_must_exist(self):
        profiles = ("desktop_display:\n  enabled: true\n  profiles:\n"
                    "    qr:\n      title: QR\n      kind: image\n      path: 'C:\\Bilder\\qr.png'\n"
                    "    obs:\n      title: OBS\n      kind: program\n      path: 'C:\\obs\\obs64.exe'\n")
        with app_client(profiles) as (client, _folder):
            self.assertEqual(client.post("/program/items", json=row(desktop=["nope"])).status_code, 400)
            item_id = client.post("/program/items", json=row(desktop=["obs", "qr"])).json()["item"]["id"]
            ran = []

            async def fake_run_profile(_request, profile_id):
                ran.append(profile_id)
                return {"ok": True}, 200

            with patch("app.api.program.run_profile", fake_run_profile):
                played = client.post(f"/program/items/{item_id}/play").json()
            self.assertTrue(played["ok"], played)
            self.assertEqual(ran, ["obs", "qr"])
            self.assertEqual([a["profile"] for a in played["linked_actions"]], ["obs", "qr"])

    def test_beamer_hide_first_unhide_after_the_content(self):
        profiles = ("desktop_display:\n  enabled: true\n  profiles:\n"
                    "    qr:\n      title: QR\n      kind: image\n      path: 'C:\\Bilder\\qr.png'\n")
        with app_client(profiles) as (client, _folder):
            self.assertNotEqual(client.post("/program/items", json=row(beamer="aus")).status_code, 200)
            plain = client.post("/program/items", json=row()).json()["item"]
            self.assertIsNone(plain["beamer"])
            events = []

            async def fake_press(_request, button_id, action):
                events.append((button_id, action["hidden"]))
                return {"ok": True, "button": button_id}, 200

            async def fake_run_profile(_request, profile_id):
                events.append(("desktop", profile_id))
                return {"ok": True}, 200

            for beamer, expected in (("hide", [("beamer-hide", True), ("desktop", "qr")]),
                                     ("unhide", [("desktop", "qr"), ("beamer-unhide", False)])):
                events.clear()
                item_id = client.post("/program/items", json=row(beamer=beamer, desktop=["qr"])).json()["item"]["id"]
                with patch("app.api.program.press_native", fake_press), \
                        patch("app.api.program.run_profile", fake_run_profile):
                    played = client.post(f"/program/items/{item_id}/play").json()
                self.assertTrue(played["ok"], played)
                self.assertEqual(events, expected, beamer)

    def test_shortcuts_saved_edited_cloned_played_and_assigned(self):
        profiles = ("desktop_display:\n  enabled: true\n  profiles:\n"
                    "    qr:\n      title: QR\n      kind: image\n      path: 'C:\\Bilder\\qr.png'\n")
        with app_client(profiles) as (client, _folder):
            ran = []

            async def fake_run_profile(_request, profile_id):
                ran.append(profile_id)
                return {"ok": True}, 200

            item = client.post("/program/items", json=row(title="Begrüßung", desktop=["qr"])).json()["item"]
            self.assertEqual(client.post("/program/items/missing/shortcut").status_code, 404)
            saved = client.post(f"/program/items/{item['id']}/shortcut").json()["shortcut"]
            self.assertEqual((saved["title"], saved["desktop"]), ("Begrüßung", ["qr"]))
            self.assertEqual(client.put(f"/shortcuts/{saved['id']}", json={"title": "X", "desktop": ["nope"]}).status_code, 400)
            edited = client.put(f"/shortcuts/{saved['id']}", json={"title": "QR zeigen", "desktop": ["qr"]}).json()
            self.assertEqual(edited["shortcut"]["title"], "QR zeigen")
            copy = client.post(f"/shortcuts/{saved['id']}/duplicate").json()["shortcut"]
            self.assertEqual([s["title"] for s in client.get("/shortcuts").json()["shortcuts"]],
                             ["QR zeigen", "QR zeigen (Kopie)"])

            status_before = client.get("/program/status").json()
            with patch("app.api.program.run_profile", fake_run_profile):
                played = client.post(f"/shortcuts/{saved['id']}/play").json()
            self.assertTrue(played["ok"], played)
            self.assertEqual(ran, ["qr"])
            self.assertEqual(client.get("/program/status").json()["current_id"], status_before["current_id"],
                             "a shortcut leaves the program's run order alone")

            self.assertEqual(client.post("/program/items", json=row(shortcuts=["0" * 32])).status_code, 400)
            plain = client.post("/program/items", json=row(title="Lied", shortcuts=[copy["id"], saved["id"]])).json()["item"]
            ran.clear()
            with patch("app.api.program.run_profile", fake_run_profile):
                played = client.post(f"/program/items/{plain['id']}/play").json()
            self.assertTrue(played["ok"], played)
            self.assertEqual(ran, ["qr", "qr"])
            self.assertEqual([s["shortcut_id"] for s in played["shortcuts"]], [copy["id"], saved["id"]])

            deleted = client.delete(f"/shortcuts/{copy['id']}").json()
            self.assertEqual(deleted["program_items"], 1)
            rows = {i["id"]: i for i in client.get("/program/items").json()}
            self.assertEqual(rows[plain["id"]]["shortcuts"], [saved["id"]])


if __name__ == "__main__":
    unittest.main()
