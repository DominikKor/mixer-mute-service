import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.native_controls import NativeControlError, NativeControlService


class NativeControlTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "native-state.json"
        self.controls = NativeControlService(self.state_path)

    async def test_shelly_set_uses_rpc_boolean(self):
        with patch("app.native_controls._shelly_rpc") as rpc:
            result = await self.controls.press({
                "type": "shelly-set", "target": "light-left", "on": True,
            })

        self.assertTrue(result["ok"])
        rpc.assert_called_once_with("192.168.188.25", "Switch.Set", {"id": 0, "on": True})

    async def test_artnet_zone_toggle_is_mocked_and_uses_start_level(self):
        with patch.object(self.controls, "_fade_to", new_callable=AsyncMock) as fade:
            result = await self.controls.press({"type": "artnet-zone-toggle", "zone": "left"})

        self.assertEqual(result["level"], 255)
        frame = fade.await_args.args[0]
        self.assertEqual(frame[0:3], [255, 255, 50])
        fade.assert_awaited_once()

    async def test_concurrent_toggles_build_on_each_other(self):
        # Two quick presses: the second must see the first one's result and
        # switch the zone off again, not compute "on" from the old frame.
        async def slow_fade(target):
            await asyncio.sleep(0.01)

        with patch.object(self.controls, "_fade_to", side_effect=slow_fade):
            first, second = await asyncio.gather(
                self.controls.press({"type": "artnet-zone-toggle", "zone": "left"}),
                self.controls.press({"type": "artnet-zone-toggle", "zone": "left"}),
            )

        self.assertEqual(sorted([first["level"], second["level"]]), [0, 255])
        self.assertEqual(self.controls._dmx[0], 0)

    async def test_adjust_uses_dimming_step_setting(self):
        with patch.object(self.controls, "_fade_to", new_callable=AsyncMock):
            await self.controls.press({"type": "artnet-setting-adjust", "setting": "steps", "delta": -30})
            await self.controls.press({"type": "artnet-zone-adjust", "zone": "right", "delta": 50})
            await self.controls.press({"type": "artnet-all-adjust", "delta": 50})

        self.assertEqual(self.controls._levels["steps"], 20)
        self.assertEqual(self.controls._dmx[12], 40)  # right zone white: two steps
        self.assertEqual(self.controls._dmx[0], 20)   # left zone white: one step

    async def test_zones_on_switches_off_zones_on_and_keeps_lit_ones(self):
        self.controls._dmx[12] = 90  # right zone already lit
        with patch.object(self.controls, "_fade_to", new_callable=AsyncMock):
            for _ in range(2):  # pressing a macro twice must not switch off
                await self.controls.press({"type": "artnet-zones-on", "zones": ["left", "right"]})

        self.assertEqual(self.controls._dmx[0], 255)
        self.assertEqual(self.controls._dmx[12], 90)

    async def test_socket_error_becomes_native_control_error(self):
        with patch("app.native_controls._send_artnet_frame", side_effect=OSError("network down")):
            with self.assertRaises(NativeControlError):
                await self.controls.press({"type": "artnet-zone-toggle", "zone": "left"})
        self.assertEqual(self.controls._dmx[0], 0)

    async def test_corrupt_state_file_is_kept(self):
        self.state_path.write_text("{not json", encoding="utf-8")
        controls = NativeControlService(self.state_path)
        self.assertEqual(controls._levels["start"], 255)
        self.assertTrue(self.state_path.with_name(self.state_path.name + ".corrupt").exists())

    async def test_beamer_mode_blocks_until_current_state_is_set(self):
        with patch.object(self.controls, "_toggle_beamer", new_callable=AsyncMock) as toggle:
            with self.assertRaisesRegex(NativeControlError, "state is unknown"):
                await self.controls.press({"type": "beamer-set", "hidden": True})
        toggle.assert_not_awaited()

    async def test_beamer_mode_only_toggles_when_target_differs(self):
        await self.controls.set_beamer_state(True)
        with patch.object(self.controls, "_toggle_beamer", new_callable=AsyncMock) as toggle:
            already_hidden = await self.controls.press({"type": "beamer-set", "hidden": True})
            unhide = await self.controls.press({"type": "beamer-set", "hidden": False})

        self.assertFalse(already_hidden["changed"])
        self.assertTrue(unhide["changed"])
        self.assertFalse(await self.controls.get_beamer_state())
        toggle.assert_awaited_once()
        self.assertFalse(NativeControlService(self.state_path)._beamer_hidden)

    async def test_beamer_state_can_be_declared_and_persisted(self):
        result = await self.controls.set_beamer_state(False)
        self.assertFalse(result["hidden"])
        self.assertFalse(NativeControlService(self.state_path)._beamer_hidden)

    async def test_failed_beamer_toggle_does_not_change_state(self):
        await self.controls.set_beamer_state(True)
        with patch.object(self.controls, "_toggle_beamer", new_callable=AsyncMock,
                          side_effect=NativeControlError("script failed")):
            with self.assertRaisesRegex(NativeControlError, "script failed"):
                await self.controls.press({"type": "beamer-set", "hidden": False})

        self.assertTrue(await self.controls.get_beamer_state())
        self.assertTrue(NativeControlService(self.state_path)._beamer_hidden)

    async def test_artnet_settings_are_bounded_and_persisted(self):
        await self.controls.press({"type": "artnet-start-adjust", "delta": 50})
        await self.controls.press({"type": "artnet-setting-adjust", "setting": "fade_ms", "delta": -5000})

        stored = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["levels"]["start"], 255)
        self.assertEqual(stored["levels"]["fade_ms"], 0)
        self.assertEqual(NativeControlService(self.state_path)._levels["fade_ms"], 0)

    async def test_visca_preset_sends_expected_packet_to_mock(self):
        with patch("app.native_controls._visca_tcp") as visca:
            await self.controls.press({"type": "visca-preset", "camera": "cam2", "preset": 8})

        visca.assert_called_once_with(
            "192.168.5.22", 5678, bytes((0x81, 0x01, 0x04, 0x3F, 0x02, 8, 0xFF))
        )

    async def test_obs_live_enables_source_before_switching_scene(self):
        requests = []

        def fake_obs_request(host, port, request_type, request_data=None):
            requests.append((request_type, request_data or {}))
            if request_type == "GetCurrentProgramScene":
                return {"currentProgramSceneName": "Preview Scene"}
            if request_type == "GetSceneItemList":
                return {"sceneItems": [{"sourceName": "Cam1", "sceneItemId": 12}]}
            return {}

        with patch("app.native_controls._obs_request", side_effect=fake_obs_request):
            await self.controls.press({
                "type": "obs-live", "scene": "Cam 1",
                "source_scene": "Current Scene", "source": "Cam1",
            })

        self.assertEqual(requests[0][0], "GetCurrentProgramScene")
        self.assertIn(("GetSceneItemList", {"sceneName": "Preview Scene"}), requests)
        enabled_index = requests.index(("SetSceneItemEnabled", {
            "sceneName": "Preview Scene", "sceneItemId": 12, "sceneItemEnabled": True,
        }))
        scene_index = requests.index(("SetCurrentProgramScene", {"sceneName": "Cam 1"}))
        self.assertLess(enabled_index, scene_index)


if __name__ == "__main__":
    unittest.main()