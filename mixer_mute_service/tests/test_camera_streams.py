"""Tests for private RTSP source validation and camera secret handling."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.camera_streams import CameraStreamError, CameraStreamStore, MediaMTXRelay, _relay_config, _validate_source


class FakeProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


class CameraStreamTests(unittest.TestCase):
    def test_rtsp_credentials_are_encoded_only_in_relay_config(self):
        camera = {
            "id": "cam-abc123",
            "name": "Stage",
            "rtsp_url": "rtsp://192.168.5.21:554/live/main?channel=1",
            "username": "cam user",
            "password": "p@ss/word",
        }
        config = _relay_config([camera])
        self.assertEqual(
            config["paths"][camera["id"]]["source"],
            "rtsp://cam%20user:p%40ss%2Fword@192.168.5.21:554/live/main?channel=1",
        )
        self.assertTrue(config["paths"][camera["id"]]["sourceOnDemand"])
        self.assertEqual(config["webrtcAddress"], ":8889")
        self.assertEqual(config["webrtcLocalUDPAddress"], ":8189")

    def test_public_snapshot_never_includes_credentials(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                store = CameraStreamStore(Path(directory) / "cameras.json")
                created = await store.add(
                    "Cam 1", "rtsp://192.168.5.21:554/live", "operator", "secret"
                )
                public = (await store.public_snapshot())[0]
                internal = (await store.relay_snapshot())[0]
                self.assertEqual(created["name"], "Cam 1")
                self.assertTrue(public["has_credentials"])
                self.assertNotIn("username", public)
                self.assertNotIn("password", public)
                self.assertNotIn("rtsp_url", public)
                self.assertEqual(internal["password"], "secret")

        asyncio.run(run())

    def test_rejects_non_private_and_credential_bearing_urls(self):
        for value in (
            "rtsp://8.8.8.8:554/live",
            "rtsp://user:pass@192.168.5.21/live",
            "http://192.168.5.21/live",
            "rtsp://192.168.5.21",
            "rtsp://192.168.5.21:0/live",
        ):
            with self.subTest(value=value), self.assertRaises(CameraStreamError):
                _validate_source(value)

    def test_password_requires_username(self):
        with self.assertRaises(CameraStreamError):
            _validate_source("rtsp://192.168.5.21/live", password="secret")

    def test_relay_restarts_on_config_change_and_stops_cleanly(self):
        async def run():
            with tempfile.TemporaryDirectory() as directory:
                relay = MediaMTXRelay(Path(directory))
                first, second = FakeProcess(), FakeProcess()
                camera = {
                    "id": "cam-abc123",
                    "name": "Stage",
                    "rtsp_url": "rtsp://192.168.5.21:554/live",
                    "username": "operator",
                    "password": "secret",
                }
                with patch.object(MediaMTXRelay, "_binary", return_value="mediamtx"), \
                        patch("app.camera_streams.subprocess.Popen", side_effect=[first, second]) as popen:
                    self.assertTrue((await relay.configure([camera]))["ready"])
                    config_path = Path(directory) / "mediamtx-cameras.yml"
                    config_text = config_path.read_text(encoding="utf-8")
                    self.assertIn("operator:secret@192.168.5.21", config_text)
                    self.assertTrue((Path(directory) / "mediamtx.log").is_file())
                    self.assertTrue((await relay.configure([camera]))["ready"])
                    self.assertTrue(first.terminated)
                    self.assertEqual(popen.call_count, 2)
                    self.assertTrue((await relay.configure([]))["ready"])
                    self.assertTrue(second.terminated)
                    self.assertTrue(relay.ready)
                    self.assertFalse(config_path.exists())
                    self.assertFalse((Path(directory) / "mediamtx.log").exists())

        asyncio.run(run())

    def test_camera_http_crud_keeps_credentials_private(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                "server:\n  host: 127.0.0.1\n  port: 8765\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MIXER_CONFIG": str(config_path)}), \
                    patch("app.camera_streams.MediaMTXRelay._binary", return_value=None):
                from fastapi.testclient import TestClient
                from app.main import app

                with TestClient(app) as client:
                    body = {
                        "name": "Cam 1",
                        "rtsp_url": "rtsp://192.168.5.21:554/live/main",
                        "username": "operator",
                        "password": "secret",
                    }
                    response = client.post("/camera-streams", json=body)
                    self.assertEqual(response.status_code, 200)
                    self.assertFalse(response.json()["relay_ready"])
                    camera_id = response.json()["camera"]["id"]
                    self.assertNotIn("password", response.json()["camera"])

                    listed = client.get("/camera-streams").json()
                    self.assertEqual(listed["cameras"][0]["name"], "Cam 1")
                    self.assertNotIn("password", listed["cameras"][0])
                    self.assertNotIn("username", listed["cameras"][0])
                    self.assertNotIn("rtsp_url", listed["cameras"][0])

                    updated = {**body, "name": "Front", "rtsp_url": "", "password": ""}
                    response = client.put(f"/camera-streams/{camera_id}", json=updated)
                    self.assertEqual(response.status_code, 200)
                    stored = (Path(directory) / "camera_streams.json").read_text(encoding="utf-8")
                    self.assertIn('"password": "secret"', stored)

                    invalid = {**body, "rtsp_url": "rtsp://203.0.113.1:554/live"}
                    self.assertEqual(client.post("/camera-streams", json=invalid).status_code, 400)
                    self.assertEqual(client.delete(f"/camera-streams/{camera_id}").status_code, 200)
                    self.assertEqual(client.get("/camera-streams").json()["cameras"], [])


if __name__ == "__main__":
    unittest.main()