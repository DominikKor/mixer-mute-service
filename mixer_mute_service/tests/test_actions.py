"""The display agent client (app/display_agent.py) and the action runner
(app/actions.py): the token only goes to LAN addresses, programs never start
from a network share, audio profiles play from program rows, and program rows
refuse combinations that would fight over the mutes."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from app import display_agent
from app.display_agent import AgentError
from tests.helpers import app_client

AGENT_ENV = {"DESKTOP_AGENT_TOKEN": "test-agent-token"}
DESKTOP = (
    "desktop_display:\n  enabled: true\n  profiles:\n"
    "    music:\n      title: Musik\n      kind: audio\n      path: C:/Musik/a.mp3\n"
    "      target_ip: 192.168.188.11\n"
    "groups:\n  Chor:\n    channels: [1, 2]\n"
)


class Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, url, token, payload, timeout):
        self.calls.append((url, token, payload))
        return {"ok": True}


class AllowedTargetTests(unittest.TestCase):
    def test_lan_and_loopback_only(self):
        for ip in ("192.168.188.11", "10.0.0.5", "172.16.3.4", "127.0.0.1"):
            self.assertTrue(display_agent.allowed_target(ip), ip)
        for ip in ("8.8.8.8", "0.0.0.0", "224.0.0.1", "255.255.255.255", "pc11", "", "::1"):
            self.assertFalse(display_agent.allowed_target(ip), ip)


class CallTests(unittest.TestCase):
    def call(self, cfg, ip):
        return asyncio.run(display_agent.call(cfg, ip, "/display", {"id": "x"}))

    def test_token_never_leaves_the_lan(self):
        cfg = SimpleNamespace(enabled=True, token_env="DESKTOP_AGENT_TOKEN")
        recorder = Recorder()
        with patch.dict("os.environ", AGENT_ENV), patch("app.display_agent._post", recorder):
            with self.assertRaises(AgentError) as caught:
                self.call(cfg, "8.8.8.8")
            self.assertEqual(caught.exception.status, 400)
            self.assertEqual(recorder.calls, [])
            self.assertEqual(self.call(cfg, "192.168.188.11"), {"ok": True})
            self.assertEqual(recorder.calls[0][:2], ("http://192.168.188.11:8766/display", "test-agent-token"))

    def test_not_configured(self):
        recorder = Recorder()
        with patch.dict("os.environ", AGENT_ENV), patch("app.display_agent._post", recorder):
            for cfg in (SimpleNamespace(enabled=False, token_env="DESKTOP_AGENT_TOKEN"),
                        SimpleNamespace(enabled=True, token_env="NO_SUCH_TOKEN_VARIABLE")):
                with self.assertRaises(AgentError) as caught:
                    self.call(cfg, "192.168.188.11")
                self.assertEqual(caught.exception.status, 503)
        self.assertEqual(recorder.calls, [])


class RouteTests(unittest.TestCase):
    def test_settings_refuse_internet_targets(self):
        with app_client(DESKTOP, env=AGENT_ENV) as (client, _):
            r = client.put("/songs/settings", json={"target_ip": "8.8.8.8"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("LAN", r.json()["reason"])
            profile = {"id": "x", "title": "X", "kind": "image", "path": "C:/x.png"}
            r = client.post("/desktop/profiles", json={**profile, "target_ip": "8.8.8.8"})
            self.assertEqual(r.status_code, 400)
            r = client.post("/desktop/profiles", json={
                **profile, "kind": "program", "path": "\\\\server\\share\\x.exe", "target_ip": "192.168.188.11"})
            self.assertEqual(r.status_code, 400)
            self.assertIn("network share", r.json()["reason"])

    def test_audio_profile_plays_from_a_program_row(self):
        recorder = Recorder()
        with app_client(DESKTOP, env=AGENT_ENV) as (client, _), patch("app.display_agent._post", recorder):
            r = client.post("/program/items", json={"time": "10:00", "title": "Musik", "playback": "desktop:music"})
            self.assertEqual(r.status_code, 200)
            r = client.post(f"/program/items/{r.json()['item']['id']}/play")
            self.assertEqual(r.status_code, 200, r.json())
            url, _, payload = recorder.calls[-1]
            self.assertEqual(url, "http://192.168.188.11:8766/audio")
            self.assertEqual(payload["action"], "play")
            # The page's own button still uses the audio endpoint, not /open.
            self.assertEqual(client.post("/desktop/music/open").status_code, 400)

    def test_row_with_group_refuses_keys_that_switch_mutes(self):
        with app_client(DESKTOP, env=AGENT_ENV) as (client, _):
            buttons = client.get("/companion/buttons").json()
            category = buttons["categories"][0]["id"]
            key = client.post("/companion/buttons", json={
                "label": "Chor an", "category_id": category, "kind": "group", "group": "Chor", "operation": "unmute",
            }).json()["button"]["id"]
            r = client.post("/program/items", json={"time": "10:00", "title": "Lied", "group": "Chor", "playback": key})
            self.assertEqual(r.status_code, 400)
            self.assertIn("switches mutes", r.json()["reason"])
            r = client.post("/program/items", json={"time": "10:00", "title": "Lied", "group": "", "playback": key})
            self.assertEqual(r.status_code, 200)

    def test_weiter_with_expected_row(self):
        with app_client(DESKTOP, env=AGENT_ENV) as (client, _):
            first = client.post("/program/items", json={"time": "10:00", "title": "A"}).json()["item"]["id"]
            second = client.post("/program/items", json={"time": "10:05", "title": "B"}).json()["item"]["id"]
            r = client.post("/program/next", json={"expected_next_id": first})
            self.assertEqual((r.status_code, r.json()["program_item_id"]), (200, first))
            # A double tap sends the same expected row again: refused, B not skipped.
            r = client.post("/program/next", json={"expected_next_id": first})
            self.assertEqual((r.status_code, r.json()["reason"], r.json()["next_id"]), (409, "program moved on", second))
            self.assertEqual(client.post("/program/next").json()["program_item_id"], second)


if __name__ == "__main__":
    unittest.main()
