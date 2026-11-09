"""HTTP behavior for the manually tracked Beamer hide state."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch



class BeamerStateRouteTests(unittest.TestCase):
    def test_manual_state_and_hide_unhide_commands(self):
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
                    controls = client.app.state.native_controls
                    with patch.object(controls, "_toggle_beamer", new_callable=AsyncMock) as toggle:
                        self.assertIsNone(client.get("/beamer/state").json()["hidden"])
                        blocked = client.post("/companion/buttons/beamer-hide/press")
                        self.assertEqual(blocked.status_code, 503)
                        toggle.assert_not_awaited()

                        declared = client.post("/beamer/state", json={"hidden": True})
                        self.assertEqual(declared.status_code, 200)
                        self.assertTrue(declared.json()["hidden"])
                        toggle.assert_not_awaited()

                        already_hidden = client.post("/companion/buttons/beamer-hide/press")
                        self.assertEqual(already_hidden.status_code, 200)
                        self.assertFalse(already_hidden.json()["changed"])
                        toggle.assert_not_awaited()

                        unhide = client.post("/companion/buttons/beamer-unhide/press")
                        self.assertEqual(unhide.status_code, 200)
                        self.assertTrue(unhide.json()["changed"])
                        toggle.assert_awaited_once()
                        self.assertFalse(client.get("/beamer/state").json()["hidden"])

                    persisted = json.loads((Path(directory) / "native_control_state.json").read_text(encoding="utf-8"))
                    self.assertFalse(persisted["beamer_hidden"])


if __name__ == "__main__":
    unittest.main()