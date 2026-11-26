"""A program row runs best effort per area (app/api/program.py _run_steps):
the mixer, the light, the beamer, each file / key field, the desktop
profiles, the song and the cameras each run or fail on their own. The
mixer's safety stays absolute: a blocked group sends nothing to the mixer.

The mixer is the closed test port (never connected); display agents, the
light and the song are faked."""

import asyncio
import time
import unittest
import urllib.error
from unittest.mock import AsyncMock, patch

from tests.helpers import app_client

VIDEO_PC, MUSIC_PC = "192.168.188.11", "192.168.188.20"
CONFIG = (
    "desktop_display:\n  enabled: true\n  profiles:\n"
    f"    ms:\n      title: Mission Spotlight\n      kind: file\n      path: 'I:\\Mission Spotlight'\n"
    f"      target_ip: {VIDEO_PC}\n      mission_spotlight: true\n"
    f"    qr:\n      title: QR\n      kind: image\n      path: 'C:\\Bilder\\qr.png'\n      target_ip: {VIDEO_PC}\n"
    f"    musik:\n      title: Hintergrundmusik\n      kind: audio\n      path: 'C:\\Musik\\bg.mp3'\n      target_ip: {MUSIC_PC}\n"
    "groups:\n  Predigt:\n    channels: [1]\n"
)
ENV = {"DESKTOP_AGENT_TOKEN": "test-token"}


def row(**extra):
    return {"time": "10:00", "title": "Mission", "person": "", "group": "", **extra}


class Agents:
    """Fake display agents: records (pc, path, payload); a dead PC times out."""

    def __init__(self, dead=(), slow=0.0):
        self.calls, self.dead, self.slow = [], set(dead), slow

    def post(self, url, _token, payload, timeout):
        pc, path = url.removeprefix("http://").split(":", 1)
        path = "/" + path.split("/", 1)[1]
        self.calls.append((pc, path, payload, time.monotonic()))
        if pc in self.dead:
            time.sleep(min(self.slow, timeout))
            raise urllib.error.URLError("timed out")
        return {"ok": True}


def song_ok(shown):
    async def show_slide(_request, song, slide, rev=None):
        shown.append((song, time.monotonic()))
        return {"ok": True, "slide": slide}
    return show_slide


class RowTests(unittest.TestCase):
    def setUp(self):
        self.light = AsyncMock(return_value=({"ok": True}, 200))

    def run_row(self, client, **fields):
        # Saving still checks the references (a light scene "warm" exists).
        with patch.object(client.app.state.light_scenes, "get", AsyncMock(return_value={"id": "warm"})):
            item = client.post("/program/items", json=row(**fields)).json()
        self.assertIn("item", item, item)
        answer = client.post(f"/program/items/{item['item']['id']}/play")
        self.assertEqual(answer.status_code, 200, answer.text)
        return item["item"]["id"], answer.json()

    def test_mixer_offline_everything_else_runs(self):
        agents, shown = Agents(), []
        with app_client(CONFIG, env=ENV) as (client, _folder), \
                patch("app.display_agent._post", agents.post), \
                patch("app.api.program.apply_scene", self.light), \
                patch("app.api.program.program_area_problems", AsyncMock(return_value={})), \
                patch("app.api.program.show_slide", song_ok(shown)), \
                patch.object(client.app.state.client, "send", AsyncMock()) as mixer_send:
            item_id, answer = self.run_row(client, group="Predigt", light="warm", desktop=["ms", "musik"],
                                           song="lied.foil")
            self.assertEqual(answer["areas"]["mixer"], {"ok": False, "reason": "no mixer connection",
                                                         "details": answer["areas"]["mixer"].get("details")})
            mixer_send.assert_not_called()
            self.assertEqual({area: entry["ok"] for area, entry in answer["areas"].items()},
                             {"mixer": False, "light": True, "desktop": True, "song": True})
            self.assertEqual((answer["ok"], answer["partial"]), (False, True))
            self.assertEqual(sorted(p["id"] for _pc, path, p, _t in agents.calls if path in ("/display", "/audio")),
                             ["ms", "musik"], "Mission Spotlight and the music run")
            self.assertEqual([song for song, _t in shown], ["lied.foil"])
            self.light.assert_awaited_once()
            self.assertEqual(client.get("/program/status").json()["current_id"], item_id, "the row counts as played")

    def test_a_failing_light_costs_only_the_light(self):
        agents, shown = Agents(), []
        self.light.return_value = ({"ok": False, "reason": "no answer"}, 502)
        with app_client(CONFIG, env=ENV) as (client, _folder), \
                patch("app.display_agent._post", agents.post), \
                patch("app.api.program.apply_scene", self.light), \
                patch("app.api.program.program_area_problems", AsyncMock(return_value={})), \
                patch("app.api.program.show_slide", song_ok(shown)):
            _item, answer = self.run_row(client, light="warm", desktop=["ms"], song="lied.foil")
            self.assertEqual(answer["areas"], {"light": {"ok": False, "reason": "no answer"},
                                               "desktop": {"ok": True}, "song": {"ok": True}})
            self.assertEqual(answer["reason"], "light: no answer")

    def test_several_failures_at_once(self):
        agents, shown = Agents(dead={VIDEO_PC}), []
        self.light.return_value = ({"ok": False, "reason": "no answer"}, 502)
        jingle = f"file:{MUSIC_PC}|C:\\Musik\\jingle.mp3"
        with app_client(CONFIG, env=ENV) as (client, _folder), \
                patch("app.display_agent._post", agents.post), \
                patch("app.api.program.apply_scene", self.light), \
                patch("app.api.program.program_area_problems", AsyncMock(return_value={})), \
                patch("app.api.program.show_slide", song_ok(shown)):
            _item, answer = self.run_row(client, group="Predigt", light="warm", desktop=["ms", "qr"],
                                         playback=jingle, song="lied.foil")
            failed = {area for area, entry in answer["areas"].items() if not entry["ok"]}
            self.assertEqual(failed, {"mixer", "light", "desktop"})
            self.assertTrue(answer["areas"]["playback"]["ok"] and answer["areas"]["song"]["ok"])
            self.assertIn("Mission Spotlight: display agent unavailable", answer["areas"]["desktop"]["reason"])
            self.assertIn("QR: display agent unavailable", answer["areas"]["desktop"]["reason"],
                          "a failed profile does not stop the next one")
            self.assertTrue(answer["partial"])
            self.assertEqual(answer["reason"].split("; ")[0].split(":")[0] in failed, True)

    def test_a_dead_pc_does_not_hold_up_another_pc(self):
        agents = Agents(dead={VIDEO_PC}, slow=0.6)
        with app_client(CONFIG, env=ENV) as (client, _folder), \
                patch("app.display_agent._post", agents.post), \
                patch("app.api.program.program_area_problems", AsyncMock(return_value={})):
            started = time.monotonic()
            _item, answer = self.run_row(client, desktop=["ms", "musik"])
            music = next(t for pc, path, p, t in agents.calls if path == "/audio")
            self.assertLess(music - started, 0.4, "the music PC does not wait for the video PC")
            self.assertEqual((answer["areas"]["desktop"]["ok"], answer["areas"]["desktop"]["partial"]), (False, True),
                             "the music ran, the video did not")
            self.assertTrue(answer["partial"])

    def test_steps_on_the_same_pc_keep_their_order(self):
        agents, shown = Agents(), []
        slow = Agents()

        def post(url, token, payload, timeout):
            time.sleep(0.2)  # the image takes a moment
            return slow.post(url, token, payload, timeout)

        with app_client(CONFIG, env=ENV) as (client, _folder), \
                patch("app.display_agent._post", post), \
                patch("app.api.program.program_area_problems", AsyncMock(return_value={})), \
                patch("app.api.program.show_slide", song_ok(shown)), \
                patch.object(client.app.state.songs.settings.targets[0], "ip", VIDEO_PC):
            self.run_row(client, desktop=["qr"], song="lied.foil")
            image_done = next(t for pc, path, p, t in slow.calls if path == "/display")  # noted after its 0.2 s
            self.assertGreaterEqual(shown[0][1], image_done, "the song comes after the image on its screen")
        del agents

    def test_a_deleted_reference_skips_only_its_area(self):
        agents = Agents()
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            item = client.post("/program/items", json=row(desktop=["qr"], light=None)).json()["item"]
            # A light scene deleted since the row was saved.
            client.put(f"/program/items/{item['id']}", json=row(desktop=["qr"]))
            with patch("app.api.program.program_area_problems",
                       AsyncMock(return_value={"light": "unknown light scene"})):
                answer = client.post(f"/program/items/{item['id']}/play").json()
            self.assertEqual(answer["areas"]["desktop"], {"ok": True})

    def test_full_success_is_quiet(self):
        agents = Agents()
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            _item, answer = self.run_row(client, desktop=["qr"])
            self.assertEqual((answer["ok"], answer["partial"], "reason" in answer), (True, False, False))
            self.assertEqual(answer["areas"], {"desktop": {"ok": True}})

    def test_a_shortcut_runs_best_effort_too(self):
        agents = Agents(dead={VIDEO_PC})
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            made = client.post("/shortcuts", json={"title": "Mix", "desktop": ["qr", "musik"]}).json()["shortcut"]
            answer = client.post(f"/shortcuts/{made['id']}/play").json()
            self.assertEqual({a: e["ok"] for a, e in answer["areas"].items()}, {"desktop": False})
            self.assertIn("/audio", [path for _pc, path, _p, _t in agents.calls], "the music still started")


if __name__ == "__main__":
    unittest.main()
