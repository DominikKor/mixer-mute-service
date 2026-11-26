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
        async def slow_fade(target, fade=True):
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

    async def test_zones_set_switches_the_pattern_and_keeps_lit_zones(self):
        # The lights' state (the frame is made from it).
        self.controls._fixtures["right"].update(on=True, level=90)          # already lit
        self.controls._fixtures["center-left"].update(on=True, level=200)   # lit, to be switched off
        self.controls._fixtures["center-right"].update(on=True, level=70)   # not named: stays
        with patch.object(self.controls, "_fade_to", new_callable=AsyncMock):
            for _ in range(2):  # pressing an Overall twice must not toggle
                await self.controls.press({"type": "artnet-zones-set",
                                           "on": ["left", "right"], "off": ["center-left"]})

        self.assertEqual(self.controls._dmx[0], 255)   # left: off -> start brightness
        self.assertEqual(self.controls._dmx[12], 90)   # right: keeps its level
        self.assertEqual(self.controls._dmx[4], 0)     # center-left: off
        self.assertEqual(self.controls._fixtures["center-left"]["level"], 200)  # keeps its level
        self.assertEqual(self.controls._dmx[8], 70)    # center-right: untouched

    async def test_zones_set_rejects_bad_patterns(self):
        for action in ({"on": [], "off": []}, {"on": ["left"], "off": ["left"]},
                       {"on": ["stage"]}, {"on": "left"}):
            with self.subTest(action=action), self.assertRaises(NativeControlError):
                await self.controls.press({"type": "artnet-zones-set", **action})

    async def test_shelly_group_tries_every_circuit_and_names_the_silent_ones(self):
        called = []

        def rpc(host, method, params):
            called.append(host)
            if host == "192.168.188.23":  # Vorne does not answer
                raise NativeControlError(f"Shelly {host} unreachable")
            return {}

        with patch("app.native_controls._shelly_rpc", side_effect=rpc):
            with self.assertRaises(NativeControlError) as caught:
                await self.controls.press({"type": "shelly-set-group", "on": True,
                                           "targets": ["light-left", "light-front", "light-stage"]})
        self.assertEqual(sorted(called), ["192.168.188.23", "192.168.188.24", "192.168.188.25"],
                         "the circuits after the silent one are still switched")
        self.assertIn("light-front", str(caught.exception))

    async def test_overlay_toggle_shows_text_hides_beamer_and_goes_live(self):
        requests = []
        enabled = {"value": False}

        def obs(host, port, request_type, data=None):
            requests.append((request_type, data))
            if request_type == "GetSceneItemList":
                return {"sceneItems": [{"sourceName": "BibleShow", "sceneItemId": 7,
                                        "sceneItemEnabled": enabled["value"]}]}
            if request_type == "SetSceneItemEnabled":
                enabled["value"] = data["sceneItemEnabled"]
            return {}

        action = {"type": "obs-overlay-toggle", "scene": "Cam 1", "source": "BibleShow",
                  "hide_beamer_while_shown": True}
        await self.controls.set_beamer_state(False)
        with patch("app.native_controls._obs_request", side_effect=obs), \
                patch.object(self.controls, "_toggle_beamer", new_callable=AsyncMock) as beamer:
            shown = await self.controls.press(action)
            hidden = await self.controls.press(action)
        self.assertEqual((shown["shown"], hidden["shown"]), (True, False))
        self.assertEqual(beamer.await_count, 2)
        self.assertFalse(await self.controls.get_beamer_state())
        self.assertEqual([r for r, _ in requests if r == "SetCurrentProgramScene"], ["SetCurrentProgramScene"] * 2)
        self.assertEqual(requests[2], ("SetCurrentProgramScene", {"sceneName": "Cam 1"}))

    async def test_live_key_switches_even_if_the_scene_lacks_the_source(self):
        requests = []

        def obs(host, port, request_type, data=None):
            requests.append((request_type, data))
            if request_type == "GetSceneItemList":
                return {"sceneItems": [{"sourceName": "Capture_PC2", "sceneItemId": 3, "sceneItemEnabled": True}]}
            return {}

        with patch("app.native_controls._obs_request", side_effect=obs), \
                self.assertLogs("mixer.native_controls", "WARNING"):
            result = await self.controls.press({"type": "obs-live", "scene": "Cam 1", "source": "Cam1"})
            with self.assertRaises(NativeControlError):  # a toggle key still needs its source
                await self.controls.press({"type": "obs-source-toggle", "scene": "Preview Scene", "source": "Cam1_klein"})
        self.assertTrue(result["ok"])
        self.assertIn("Cam1", result["warning"])
        self.assertIn(("SetCurrentProgramScene", {"sceneName": "Cam 1"}), requests)
        self.assertNotIn("SetSceneItemEnabled", [r for r, _ in requests])

    async def test_overlay_toggle_still_goes_live_when_the_beamer_state_is_unknown(self):
        def obs(host, port, request_type, data=None):
            if request_type == "GetSceneItemList":
                return {"sceneItems": [{"sourceName": "BibleShow", "sceneItemId": 7, "sceneItemEnabled": False}]}
            return {}

        with patch("app.native_controls._obs_request", side_effect=obs) as calls:
            with self.assertRaises(NativeControlError) as caught:
                await self.controls.press({"type": "obs-overlay-toggle", "scene": "Cam 2",
                                           "source": "BibleShow", "hide_beamer_while_shown": True})
        self.assertIn("beamer", str(caught.exception))
        self.assertEqual(calls.call_args.args[2:], ("SetCurrentProgramScene", {"sceneName": "Cam 2"}))

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

    async def test_unanswered_beamer_toggle_makes_state_unknown(self):
        await self.controls.set_beamer_state(True)
        with patch.object(self.controls, "_toggle_beamer", new_callable=AsyncMock,
                          side_effect=NativeControlError("no answer", maybe_sent=True)):
            with self.assertRaises(NativeControlError):
                await self.controls.press({"type": "beamer-set", "hidden": False})
        self.assertIsNone(await self.controls.get_beamer_state())
        self.assertIsNone(NativeControlService(self.state_path)._beamer_hidden)

    async def test_beamer_timeout_counts_as_maybe_sent(self):
        import urllib.error
        from app.native_controls import _post_beamer_form
        with patch("app.native_controls.urlopen", side_effect=urllib.error.URLError(TimeoutError("timed out"))):
            with self.assertRaises(NativeControlError) as caught:
                _post_beamer_form("avmute", "AV Mute")
        self.assertTrue(caught.exception.maybe_sent)
        with patch("app.native_controls.urlopen", side_effect=urllib.error.URLError(ConnectionRefusedError())):
            with self.assertRaises(NativeControlError) as caught:
                _post_beamer_form("avmute", "AV Mute")
        self.assertFalse(caught.exception.maybe_sent)

    async def test_raw_avmute_keeps_state_in_step(self):
        await self.controls.set_beamer_state(False)
        with patch("app.native_controls._post_beamer_form") as post:
            await self.controls.press({"type": "beamer-command", "command": "avmute"})
        post.assert_called_once()
        self.assertTrue(await self.controls.get_beamer_state())

    async def test_light_fade_does_not_hold_up_the_beamer(self):
        await self.controls.set_beamer_state(True)
        release = asyncio.Event()

        async def slow_fade(target, fade=True):
            await release.wait()

        with patch.object(self.controls, "_fade_to", side_effect=slow_fade), \
                patch.object(self.controls, "_toggle_beamer", new_callable=AsyncMock):
            fade = asyncio.create_task(self.controls.press({"type": "artnet-zone-toggle", "zone": "left"}))
            await asyncio.sleep(0)
            result = await asyncio.wait_for(self.controls.press({"type": "beamer-set", "hidden": False}), 1)
            release.set()
            await fade
        self.assertTrue(result["changed"])

    async def test_failed_beamer_toggle_does_not_change_state(self):
        await self.controls.set_beamer_state(True)
        with patch.object(self.controls, "_toggle_beamer", new_callable=AsyncMock,
                          side_effect=NativeControlError("script failed")):
            with self.assertRaisesRegex(NativeControlError, "script failed"):
                await self.controls.press({"type": "beamer-set", "hidden": False})

        self.assertTrue(await self.controls.get_beamer_state())
        self.assertTrue(NativeControlService(self.state_path)._beamer_hidden)

    async def test_beamer_toggle_and_direct_commands_use_projector_form_endpoint(self):
        with patch("app.native_controls._post_beamer_form") as post:
            await self.controls.set_beamer_state(True)
            await self.controls.press({"type": "beamer-set", "hidden": False})
            await self.controls.press({"type": "beamer-command", "command": "power-off"})
            await self.controls.press({"type": "beamer-command", "command": "power-on"})
            await self.controls.press({"type": "beamer-command", "command": "freeze"})

        self.assertEqual(post.call_args_list, [
            unittest.mock.call("avmute", "AV Mute"),
            unittest.mock.call("btn_powoff", "Power Off"),
            unittest.mock.call("btn_powon", "Power On"),
            unittest.mock.call("freeze", "Freeze"),
        ])

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


class ShellyRpcTests(unittest.TestCase):
    """_shelly_rpc against a local HTTP server that answers like a Gen2+
    Shelly (loopback only)."""

    def setUp(self):
        import http.server
        import threading

        test = self
        self.delay = 0.0
        self.answer = (200, {"was_on": False})
        self.requests = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                test.requests.append((self.path, json.loads(self.rfile.read(length) or b"{}")))
                import time
                time.sleep(test.delay)
                status, body = test.answer
                data = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.host = f"127.0.0.1:{self.server.server_address[1]}"

    def test_answer_and_request(self):
        from app.native_controls import _shelly_rpc
        self.assertEqual(_shelly_rpc(self.host, "Switch.Set", {"id": 0, "on": True}), {"was_on": False})
        self.assertEqual(self.requests, [("/rpc/Switch.Set", {"id": 0, "on": True})])

    def test_rejected_call(self):
        from app.native_controls import _shelly_rpc
        self.answer = (400, {"code": -103, "message": "Invalid argument"})
        with self.assertRaisesRegex(NativeControlError, "rejected"):
            _shelly_rpc(self.host, "Switch.Set", {"id": 7, "on": True})
        self.answer = (200, {"error": {"code": -103}})
        with self.assertRaisesRegex(NativeControlError, "rejected"):
            _shelly_rpc(self.host, "Switch.Set", {"id": 0, "on": True})

    def test_slow_answer_counts_up_to_the_answer_timeout(self):
        from app.native_controls import _shelly_rpc
        self.delay = 0.3
        with patch("app.native_controls.SHELLY_CONNECT_TIMEOUT", 0.1), patch("app.native_controls.SHELLY_TIMEOUT", 1.0):
            self.assertEqual(_shelly_rpc(self.host, "Switch.GetStatus", {"id": 0}), {"was_on": False})
        with patch("app.native_controls.SHELLY_TIMEOUT", 0.1):
            with self.assertRaisesRegex(NativeControlError, "unreachable"):
                _shelly_rpc(self.host, "Switch.GetStatus", {"id": 0})

    def test_dead_shelly_is_given_up_at_the_connect_timeout(self):
        import socket
        from app import native_controls

        timeouts = []

        class DeadConnection:
            def __init__(self, host, timeout):
                timeouts.append(timeout)
                self.sock = None

            def connect(self):
                raise socket.timeout("timed out")

            def close(self):
                pass

        with patch.object(native_controls.http.client, "HTTPConnection", DeadConnection):
            with self.assertRaisesRegex(NativeControlError, "unreachable"):
                native_controls._shelly_rpc("192.168.188.23", "Switch.GetStatus", {"id": 0})
        self.assertEqual(timeouts, [native_controls.SHELLY_CONNECT_TIMEOUT])
        self.assertLessEqual(native_controls.SHELLY_CONNECT_TIMEOUT, 1.0)

    def _flaky_connection(self, failures, timeouts):
        """HTTPConnection whose first ``failures`` connects time out."""
        import socket
        from app import native_controls
        real = native_controls.http.client.HTTPConnection
        attempts = []

        class Flaky(real):
            def __init__(self, host, timeout):
                timeouts.append(timeout)
                super().__init__(host, timeout=timeout)

            def connect(self):
                attempts.append(self.host)
                if len(attempts) <= failures:
                    raise socket.timeout("timed out")
                super().connect()

        return patch.object(native_controls.http.client, "HTTPConnection", Flaky), attempts

    def test_switching_tries_a_lost_connect_once_more(self):
        from app import native_controls
        timeouts = []
        flaky, attempts = self._flaky_connection(1, timeouts)
        with flaky:
            self.assertEqual(native_controls._shelly_rpc(self.host, "Switch.Set", {"id": 0, "on": True}),
                             {"was_on": False})
        self.assertEqual(len(attempts), 2)
        self.assertEqual(timeouts, [native_controls.SHELLY_CONNECT_TIMEOUT] * 2)
        self.assertEqual(self.requests, [("/rpc/Switch.Set", {"id": 0, "on": True})])  # sent once

    def test_switching_gives_up_after_the_second_connect(self):
        from app import native_controls
        flaky, attempts = self._flaky_connection(5, [])
        with flaky:
            with self.assertRaisesRegex(NativeControlError, "unreachable"):
                native_controls._shelly_rpc(self.host, "Switch.Set", {"id": 0, "on": True})
        self.assertEqual(len(attempts), native_controls.SHELLY_SWITCH_CONNECT_ATTEMPTS)
        self.assertLessEqual(native_controls.SHELLY_SWITCH_CONNECT_ATTEMPTS * native_controls.SHELLY_CONNECT_TIMEOUT, 2.0)
        self.assertEqual(self.requests, [])

    def test_status_question_has_one_connect(self):
        from app import native_controls
        flaky, attempts = self._flaky_connection(1, [])
        with flaky:
            with self.assertRaisesRegex(NativeControlError, "unreachable"):
                native_controls._shelly_rpc(self.host, "Switch.GetStatus", {"id": 0})
        self.assertEqual(len(attempts), 1)

    def test_no_retry_once_the_command_was_sent(self):
        from app import native_controls
        self.delay = 0.3
        flaky, attempts = self._flaky_connection(0, [])
        with flaky, patch("app.native_controls.SHELLY_TIMEOUT", 0.1):
            with self.assertRaisesRegex(NativeControlError, "unreachable"):
                native_controls._shelly_rpc(self.host, "Switch.Set", {"id": 0, "on": True})
        self.assertEqual(len(attempts), 1)
        self.assertEqual(len(self.requests), 1)

if __name__ == "__main__":
    unittest.main()