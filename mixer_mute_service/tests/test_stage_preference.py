"""The Stage switch is remembered in a cookie the server sets (Safari wipes
script-written storage after 7 days without use, but not such a cookie)."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


class StagePreferenceRouteTests(unittest.TestCase):
    def test_stage_cookie_on_off_and_validation(self):
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

                with TestClient(app) as client:
                    on = client.post("/ui/stage", json={"on": True})
                    self.assertEqual(on.status_code, 200)
                    header = on.headers["set-cookie"]
                    self.assertIn("mixer_stage=1", header)
                    self.assertIn("Max-Age=315360000", header)
                    self.assertIn("Path=/", header)
                    self.assertNotIn("HttpOnly", header)  # the page reads it on load

                    off = client.post("/ui/stage", json={"on": False})
                    self.assertIn("mixer_stage=0", off.headers["set-cookie"])

                    self.assertEqual(client.post("/ui/stage", json={"on": "maybe"}).status_code, 422)
                    self.assertEqual(client.post("/ui/stage", json={}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
