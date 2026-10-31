import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.native_controls import NativeControlService


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
        with patch.object(self.controls, "_send_dmx", new_callable=AsyncMock) as send_dmx:
            result = await self.controls.press({"type": "artnet-zone-toggle", "zone": "left"})

        self.assertEqual(result["level"], 255)
        frame = send_dmx.await_args.args[0]
        self.assertEqual(frame[0:3], [255, 255, 50])
        send_dmx.assert_awaited_once()

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