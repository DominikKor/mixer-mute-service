"""Update key, display agent step: the service finds the agent of the PC whose
browser asked (127.0.0.1 for its own PC), compares its version with the agent
files it ships, pushes them through the agent's self-update and waits until
the new version answers. Agents from before the self-update need the .cmd."""

import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


class FakeAgent:
    """display_agent._post stand-in: one agent per address."""

    def __init__(self):
        self.agents = {}   # ip -> version, or None for an agent without /info
        self.calls = []

    def __call__(self, url, token, payload, timeout):
        ip, path = url[len("http://"):].split(":", 1)[0], "/" + url.split("/", 3)[3]
        self.calls.append((ip, path, payload))
        if ip not in self.agents:
            raise urllib.error.URLError("no route")
        if self.agents[ip] is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        if path == "/info":
            return {"ok": True, "version": self.agents[ip]}
        if path == "/update":
            self.agents[ip] = payload["version"]  # comes back with it after the restart
            return {"ok": True, "version": payload["version"], "restarting": True}
        raise AssertionError(path)


class AgentUpdateServiceTests(unittest.TestCase):
    def test_check_and_update_the_agent_of_this_pc(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                "server:\n  host: 127.0.0.1\n  port: 8765\n"
                "desktop_display:\n  enabled: true\n",
                encoding="utf-8",
            )
            fake = FakeAgent()
            env = {"MIXER_CONFIG": str(config_path), "DESKTOP_AGENT_TOKEN": "t", "MIXER_BUILD_ID": "v9"}
            with patch.dict(os.environ, env), patch("app.display_agent._post", fake), \
                    patch("app.agent_update.RESTART_WAIT_SECONDS", 4):
                from fastapi.testclient import TestClient
                from app.main import app

                # The service's own PC: its browser comes from 127.0.0.1.
                with TestClient(app, client=("127.0.0.1", 50000)) as here:
                    s = here.get("/update/agent").json()
                    self.assertEqual((s["present"], s["pc"], s["latest"]), (False, "127.0.0.1", "v9"))

                    fake.agents["127.0.0.1"] = "v8"
                    s = here.get("/update/agent").json()
                    self.assertEqual((s["present"], s["version"], s["current"], s["self_update"]),
                                     (True, "v8", False, True))
                    r = here.post("/update/agent")
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual((r.json()["updated"], r.json()["version"]), (True, "v9"))
                    sent = next(p for ip, path, p in fake.calls if path == "/update")
                    self.assertEqual(sent["version"], "v9")
                    self.assertIn("show_image_agent.py", sent["files"])
                    self.assertIn("restart-agent.ps1", sent["files"])
                    self.assertNotIn("version.txt", sent["files"])

                    # Already current: nothing is sent.
                    n = len(fake.calls)
                    r = here.post("/update/agent")
                    self.assertEqual(r.json()["updated"], False)
                    self.assertFalse(any(path == "/update" for _, path, _ in fake.calls[n:]))

                # The Anzeige PC: an agent from before the self-update.
                with TestClient(app, client=("192.168.188.11", 50000)) as there:
                    fake.agents["192.168.188.11"] = None
                    s = there.get("/update/agent").json()
                    self.assertEqual((s["present"], s["self_update"]), (True, False))
                    r = there.post("/update/agent")
                    self.assertEqual(r.status_code, 409)
                    self.assertIn("too old", r.json()["reason"])

                # An iPad: no agent there, the step is skipped.
                with TestClient(app, client=("192.168.188.50", 50000)) as ipad:
                    self.assertFalse(ipad.get("/update/agent").json()["present"])
                    self.assertEqual(ipad.post("/update/agent").status_code, 404)

                # An agent that never comes back with the new version.
                with TestClient(app, client=("192.168.188.12", 50000)) as slow:
                    fake.agents["192.168.188.12"] = "v7"
                    with patch.object(FakeAgent, "__call__", lambda self, url, token, payload, timeout: (
                            {"ok": True, "version": "v7"} if url.endswith("/info") else {"ok": True})):
                        r = slow.post("/update/agent")
                    self.assertEqual(r.status_code, 504)


if __name__ == "__main__":
    unittest.main()
