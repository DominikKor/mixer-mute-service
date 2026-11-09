"""Tests for the configurable all-off target selection."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from app.power_control import PowerControlStore


class PowerControlStoreTests(unittest.TestCase):
    def test_selection_persists_and_is_filtered_by_available_agents(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "power-off-config.json"
                store = PowerControlStore(path)
                await store.set(["192.168.188.11", "192.168.188.12"], True,
                                {"192.168.188.11", "192.168.188.12"})
                reloaded = PowerControlStore(path)
                self.assertEqual(await reloaded.get({"192.168.188.11"}), {
                    "targets": ["192.168.188.11"], "beamer": True,
                })

        asyncio.run(run())

    def test_rejects_unconfigured_or_invalid_targets(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                store = PowerControlStore(Path(directory) / "power-off-config.json")
                with self.assertRaisesRegex(ValueError, "not configured"):
                    await store.set(["192.168.188.99"], False, {"192.168.188.11"})
                with self.assertRaisesRegex(ValueError, "invalid agent target"):
                    await store.set(["not-an-ip"], False, {"192.168.188.11"})

        asyncio.run(run())

    def test_power_routes_only_shutdown_configured_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                "server:\n  host: 127.0.0.1\n  port: 8765\n"
                "desktop_display:\n  enabled: true\n  profiles:\n"
                "    pc1:\n      title: PC 1\n      kind: image\n      path: C:/pc1.png\n      target_ip: 192.168.188.11\n"
                "    pc2:\n      title: PC 2\n      kind: program\n      path: C:/pc2.exe\n      target_ip: 192.168.188.12\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {
                "MIXER_CONFIG": str(config_path),
                "DESKTOP_AGENT_TOKEN": "test-agent-token",
            }):
                from fastapi.testclient import TestClient
                from app.main import app

                with TestClient(app) as client:
                    self.assertEqual(
                        set(client.app.state.cfg.desktop_display.profiles), {"pc1", "pc2"}
                    )
                    available = client.get("/power/off-config").json()
                    self.assertEqual([item["ip"] for item in available["available_targets"]], [
                        "192.168.188.11", "192.168.188.12",
                    ])
                    invalid = client.put("/power/off-config", json={
                        "target_ips": ["192.168.188.99"], "beamer": True,
                    })
                    self.assertEqual(invalid.status_code, 400)
                    self.assertEqual(client.post("/power/off").status_code, 400)

                    configured = client.put("/power/off-config", json={
                        "target_ips": ["192.168.188.11"], "beamer": True,
                    })
                    self.assertEqual(configured.status_code, 200)
                    native_controls = client.app.state.native_controls
                    with patch("app.display_agent._post", return_value={
                        "ok": True, "shutdown_scheduled": True,
                    }) as forward, patch.object(native_controls, "press", new_callable=AsyncMock,
                                                return_value={"ok": True, "command": "power-off"}) as press:
                        response = client.post("/power/off")
                        self.assertEqual(response.status_code, 200)
                        self.assertTrue(response.json()["ok"])
                        self.assertEqual(response.json()["results"], [
                            {"target": "beamer", "ok": True, "command": "power-off"},
                            {"target": "192.168.188.11", "ok": True,
                             "shutdown_scheduled": True, "status": 202},
                        ])
                        press.assert_awaited_once_with({"type": "beamer-command", "command": "power-off"})
                        forward.assert_called_with(
                            "http://192.168.188.11:8766/power", "test-agent-token", {"action": "shutdown"}, 3.0
                        )
                        single = client.post("/power/agents/192.168.188.11/shutdown")
                        self.assertEqual(single.status_code, 202)
                        self.assertEqual(forward.call_count, 2)
                        self.assertEqual(
                            client.post("/power/agents/192.168.188.99/shutdown").status_code, 404
                        )


if __name__ == "__main__":
    unittest.main()