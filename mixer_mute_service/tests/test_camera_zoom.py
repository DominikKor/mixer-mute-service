"""Hold-to-zoom endpoint of the camera section (VISCA is mocked, no camera is
contacted): start, stop, unknown camera, invalid direction, unreachable camera."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class CameraZoomRouteTests(unittest.TestCase):
    def test_zoom_start_stop_and_errors(self):
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
                from app.native_controls import NativeControlError

                with TestClient(app) as client, patch("app.native_controls._visca_tcp") as visca:
                    zoom_in = client.post("/cameras/cam1/zoom", json={"direction": "in"})
                    self.assertEqual(zoom_in.status_code, 200)
                    self.assertEqual(zoom_in.json()["direction"], "in")
                    stop = client.post("/cameras/cam1/zoom", json={"direction": "stop"})
                    self.assertEqual(stop.status_code, 200)
                    zoom_out = client.post("/cameras/cam2/zoom", json={"direction": "out"})
                    self.assertEqual(zoom_out.status_code, 200)
                    self.assertEqual([call.args for call in visca.call_args_list], [
                        ("192.168.5.21", 5678, bytes((0x81, 0x01, 0x04, 0x07, 0x20, 0xFF))),
                        ("192.168.5.21", 5678, bytes((0x81, 0x01, 0x04, 0x07, 0x00, 0xFF))),
                        ("192.168.5.22", 5678, bytes((0x81, 0x01, 0x04, 0x07, 0x30, 0xFF))),
                    ])

                    visca.reset_mock()
                    self.assertEqual(client.post("/cameras/cam9/zoom", json={"direction": "in"}).status_code, 404)
                    self.assertEqual(client.post("/cameras/cam1/zoom", json={"direction": "left"}).status_code, 422)
                    visca.assert_not_called()

                    visca.side_effect = NativeControlError("VISCA camera 192.168.5.21 unreachable")
                    down = client.post("/cameras/cam1/zoom", json={"direction": "in"})
                    self.assertEqual(down.status_code, 503)
                    self.assertIn("unreachable", down.json()["reason"])


if __name__ == "__main__":
    unittest.main()
