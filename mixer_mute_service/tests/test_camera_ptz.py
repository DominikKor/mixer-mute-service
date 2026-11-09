"""Pan/tilt arrow keys of the camera section and the camera keys that left
Steuerung (VISCA is mocked, no camera is contacted)."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.camera_ptz import PanTiltControl, pan_tilt_payload
from app.native_controls import NativeControlError

CAM1 = ("192.168.5.21", 5678)
CAM2 = ("192.168.5.22", 5678)


def drive(direction, pan=0x06, tilt=0x05):
    xx, yy = {"left": (1, 3), "right": (2, 3), "up": (3, 1), "down": (3, 2), "stop": (3, 3)}[direction]
    return bytes((0x81, 0x01, 0x06, 0x01, pan, tilt, xx, yy, 0xFF))


class PanTiltPayloadTests(unittest.TestCase):
    def test_payloads(self):
        for direction in ("left", "right", "up", "down", "stop"):
            self.assertEqual(pan_tilt_payload(direction), drive(direction))


class PanTiltWatchdogTests(unittest.TestCase):
    def run_async(self, coro):
        return asyncio.run(coro)

    def test_watchdog_stops_without_release(self):
        async def scenario():
            control = PanTiltControl(watchdog_seconds=0.05)
            with patch("app.native_controls._visca_tcp") as visca:
                await control.drive("cam1", "left")
                await asyncio.sleep(0.15)
                return [call.args for call in visca.call_args_list]

        self.assertEqual(self.run_async(scenario()), [(*CAM1, drive("left")), (*CAM1, drive("stop"))])

    def test_keepalive_and_release_cancel_watchdog(self):
        async def scenario():
            control = PanTiltControl(watchdog_seconds=0.08)
            with patch("app.native_controls._visca_tcp") as visca:
                await control.drive("cam2", "up")
                for _ in range(3):   # held longer than the watchdog, refreshed in time
                    await asyncio.sleep(0.04)
                    await control.drive("cam2", "up")
                await control.drive("cam2", "stop")
                await asyncio.sleep(0.15)   # no extra stop from the watchdog
                return [call.args for call in visca.call_args_list]

        self.assertEqual(self.run_async(scenario()), [(*CAM2, drive("up"))] * 4 + [(*CAM2, drive("stop"))])

    def test_failed_stop_is_retried_by_watchdog(self):
        async def scenario():
            control = PanTiltControl(watchdog_seconds=0.05)
            with patch("app.native_controls._visca_tcp") as visca:
                await control.drive("cam1", "down")
                visca.side_effect = NativeControlError("VISCA camera unreachable")
                with self.assertRaises(NativeControlError):
                    await control.drive("cam1", "stop")
                visca.side_effect = None
                await asyncio.sleep(0.15)
                return [call.args for call in visca.call_args_list]

        self.assertEqual(self.run_async(scenario()), [
            (*CAM1, drive("down")), (*CAM1, drive("stop")), (*CAM1, drive("stop")),
        ])

    def test_failed_move_is_still_stopped(self):
        async def scenario():
            control = PanTiltControl(watchdog_seconds=0.05)
            with patch("app.native_controls._visca_tcp") as visca:
                visca.side_effect = [NativeControlError("timeout after send"), None]
                with self.assertRaises(NativeControlError):
                    await control.drive("cam1", "right")
                await asyncio.sleep(0.15)
                return [call.args for call in visca.call_args_list]

        self.assertEqual(self.run_async(scenario()), [(*CAM1, drive("right")), (*CAM1, drive("stop"))])

    def test_close_stops_moving_cameras(self):
        async def scenario():
            control = PanTiltControl(watchdog_seconds=10)
            with patch("app.native_controls._visca_tcp") as visca:
                await control.drive("cam2", "left")
                await control.close()
                await asyncio.sleep(0.05)
                return [call.args for call in visca.call_args_list]

        self.assertEqual(self.run_async(scenario()), [(*CAM2, drive("left")), (*CAM2, drive("stop"))])

    def test_rejects_unknown_camera_or_direction(self):
        async def scenario():
            control = PanTiltControl()
            with patch("app.native_controls._visca_tcp") as visca:
                for camera, direction in (("cam9", "left"), ("cam1", "in")):
                    with self.assertRaises(NativeControlError):
                        await control.drive(camera, direction)
                return visca.call_count

        self.assertEqual(self.run_async(scenario()), 0)


class CameraRouteTests(unittest.TestCase):
    def test_pan_tilt_route_and_retired_camera_keys(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                "server:\n  host: 127.0.0.1\n  port: 8765\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MIXER_CONFIG": str(config_path)}):
                from fastapi.testclient import TestClient
                from app.main import app

                with TestClient(app) as client, patch("app.native_controls._visca_tcp") as visca:
                    left = client.post("/cameras/cam1/pan-tilt", json={"direction": "left"})
                    self.assertEqual(left.status_code, 200)
                    self.assertEqual(left.json()["direction"], "left")
                    stop = client.post("/cameras/cam1/pan-tilt", json={"direction": "stop"})
                    self.assertEqual(stop.status_code, 200)
                    up = client.post("/cameras/cam2/pan-tilt", json={"direction": "up"})
                    self.assertEqual(up.status_code, 200)
                    self.assertEqual(client.post("/cameras/cam2/pan-tilt", json={"direction": "stop"}).status_code, 200)
                    self.assertEqual([call.args for call in visca.call_args_list], [
                        (*CAM1, drive("left")), (*CAM1, drive("stop")),
                        (*CAM2, drive("up")), (*CAM2, drive("stop")),
                    ])

                    visca.reset_mock()
                    self.assertEqual(client.post("/cameras/cam9/pan-tilt", json={"direction": "left"}).status_code, 404)
                    self.assertEqual(client.post("/cameras/cam1/pan-tilt", json={"direction": "in"}).status_code, 422)
                    visca.assert_not_called()

                    visca.side_effect = NativeControlError("VISCA camera 192.168.5.21 unreachable")
                    down = client.post("/cameras/cam1/pan-tilt", json={"direction": "down"})
                    self.assertEqual(down.status_code, 503)
                    self.assertIn("unreachable", down.json()["reason"])
                    visca.side_effect = None
                    client.post("/cameras/cam1/pan-tilt", json={"direction": "stop"})

                    # Steuerung has no camera keys; the camera section gets them separately.
                    data = client.get("/companion/buttons").json()
                    self.assertFalse(any(b["id"].startswith(("native-cam1-", "native-cam2-")) for b in data["buttons"]))
                    self.assertNotIn("cameras", {c["id"] for c in data["categories"]})
                    presets = [b for b in data["camera_buttons"] if b["native_action"]["type"] == "visca-preset"]
                    self.assertIn("native-cam1-preset-1", {b["id"] for b in presets})

                    # A camera preset still runs by ID (camera section, program rows).
                    visca.reset_mock()
                    press = client.post("/companion/buttons/native-cam2-preset-3/press")
                    self.assertEqual(press.status_code, 200)
                    self.assertEqual(visca.call_args.args, (*CAM2, bytes((0x81, 0x01, 0x04, 0x3F, 0x02, 3, 0xFF))))
                    self.assertEqual(client.post("/companion/buttons/native-cam9-preset-1/press").status_code, 404)

                    row = client.post("/program/items", json={
                        "time": "10:00", "title": "Predigt", "person": "", "group": "",
                        "playback": "native-cam1-preset-1",
                    })
                    self.assertEqual(row.status_code, 200, row.text)


if __name__ == "__main__":
    unittest.main()
