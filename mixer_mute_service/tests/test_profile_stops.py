"""When the program moves on, the desktop profiles shown or played before
stop (app/profile_stops.py): a video window closes, music fades out; what
the new row starts itself keeps running; an agent that is off never holds
the new row up. The agent's side (POST /close) is tested at the end.

The display agents are faked (app.display_agent._post records the calls)."""

import time
import unittest
import urllib.error
from unittest.mock import patch

from tests.helpers import app_client, require_tk

tkinter = require_tk()
if tkinter is not None:
    from desktop_agent import show_image_agent as agent

VIDEO_PC, MUSIC_PC = "192.168.188.11", "192.168.188.20"
PROFILES = (
    "desktop_display:\n  enabled: true\n  profiles:\n"
    f"    spotlight:\n      title: Mission Spotlight\n      kind: file\n      path: 'C:\\Videos\\spot.mp4'\n      target_ip: {VIDEO_PC}\n"
    f"    qr:\n      title: QR\n      kind: image\n      path: 'C:\\Bilder\\qr.png'\n      target_ip: {VIDEO_PC}\n"
    f"    obs:\n      title: OBS\n      kind: program\n      path: 'C:\\obs\\obs64.exe'\n      target_ip: {VIDEO_PC}\n"
    f"    musik:\n      title: Hintergrundmusik\n      kind: audio\n      path: 'C:\\Musik\\bg.mp3'\n      target_ip: {MUSIC_PC}\n"
    "      fade: true\n      fade_seconds: 4\n"
    f"    lied:\n      title: Lied\n      kind: audio\n      path: 'C:\\Musik\\lied.mp3'\n      target_ip: {MUSIC_PC}\n"
)
ENV = {"DESKTOP_AGENT_TOKEN": "test-token"}


def row(title, **extra):
    return {"time": "10:00", "title": title, "person": "", "group": "", **extra}


class FakeAgents:
    """Answers like the agents; records (pc, path, payload, timeout)."""

    def __init__(self, dead=(), slow=0.0):
        self.calls = []
        self.dead = set(dead)
        self.slow = slow

    def post(self, url, _token, payload, timeout):
        pc, path = url.removeprefix("http://").split(":", 1)
        path = path.split("/", 1)[1]
        self.calls.append((pc, "/" + path, payload, timeout))
        if pc in self.dead:
            time.sleep(min(self.slow, timeout))
            raise urllib.error.URLError("timed out")
        return {"ok": True}

    def stops(self):
        return [(pc, path, payload) for pc, path, payload, _t in self.calls
                if path == "/close" or (path == "/audio" and payload["action"] == "stop")]

    def wait_for(self, count, seconds=3.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end and len(self.stops()) < count:
            time.sleep(0.02)
        return self.stops()


class AdvanceTests(unittest.TestCase):
    def play(self, client, item_id):
        answer = client.post(f"/program/items/{item_id}/play").json()
        self.assertTrue(answer["ok"], answer)
        return answer

    def test_the_next_row_closes_the_video_and_fades_the_music(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            first = client.post("/program/items", json=row("Spotlight", desktop=["spotlight", "musik", "obs"])).json()["item"]
            second = client.post("/program/items", json=row("Predigt", desktop=["qr"])).json()["item"]
            self.play(client, first["id"])
            self.assertEqual(agents.stops(), [])
            self.play(client, second["id"])
            stops = agents.wait_for(2)
            self.assertCountEqual(stops, [
                (VIDEO_PC, "/close", {"id": "spotlight"}),
                (MUSIC_PC, "/audio", {"id": "musik", "action": "stop", "path": None,
                                      "fade": {"db": 0.0, "seconds": 4.0}}),
            ])
            self.assertNotIn("obs", str(stops), "a program (e.g. OBS) is never closed")
            timeouts = {path: t for pc, path, _p, t in agents.calls if path in ("/close",)}
            self.assertLessEqual(timeouts["/close"], 2.0, "stops use short timeouts")
            self.assertEqual(client.app.state.shown_profiles.shown(), {"qr": (VIDEO_PC, "image")})

    def test_a_profile_the_next_row_starts_again_keeps_running(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            first = client.post("/program/items", json=row("Begrüßung", desktop=["musik", "spotlight"])).json()["item"]
            second = client.post("/program/items", json=row("Ansage", desktop=["musik"])).json()["item"]
            self.play(client, first["id"])
            self.play(client, second["id"])
            self.assertEqual(agents.wait_for(1), [(VIDEO_PC, "/close", {"id": "spotlight"})])
            time.sleep(0.1)
            self.assertEqual(len(agents.stops()), 1, "the music is not stopped")

    def test_new_music_on_the_same_pc_replaces_the_old_without_a_stop(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            first = client.post("/program/items", json=row("Vorspiel", desktop=["musik"])).json()["item"]
            second = client.post("/program/items", json=row("Lied", desktop=["lied"])).json()["item"]
            self.play(client, first["id"])
            self.play(client, second["id"])
            time.sleep(0.2)
            self.assertEqual(agents.stops(), [], "a stop sent alongside could end the new track")
            self.assertEqual(client.app.state.shown_profiles.shown(), {"lied": (MUSIC_PC, "audio")})

    def test_music_started_by_hand_fades_too(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            self.assertTrue(client.post("/desktop/musik/audio/play").json()["ok"])
            item = client.post("/program/items", json=row("Predigt", desktop=["qr"])).json()["item"]
            self.play(client, item["id"])
            self.assertEqual([s[:2] for s in agents.wait_for(1)], [(MUSIC_PC, "/audio")])

    def test_music_stopped_by_hand_is_not_stopped_again(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            client.post("/desktop/musik/audio/play")
            client.post("/desktop/musik/audio/stop")
            agents.calls.clear()
            item = client.post("/program/items", json=row("Predigt", desktop=["qr"])).json()["item"]
            self.play(client, item["id"])
            time.sleep(0.2)
            self.assertEqual(agents.stops(), [])

    def test_nothing_running_nothing_stopped(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            item = client.post("/program/items", json=row("Predigt", desktop=["qr"])).json()["item"]
            self.play(client, item["id"])
            time.sleep(0.2)
            self.assertEqual(agents.stops(), [])
            self.assertEqual([path for _pc, path, _p, _t in agents.calls], ["/display"])

    def test_an_agent_that_is_off_never_delays_the_next_row(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            first = client.post("/program/items", json=row("Spotlight", desktop=["spotlight"])).json()["item"]
            second = client.post("/program/items", json=row("Ansage", desktop=["musik"])).json()["item"]
            first_again = client.post("/program/items", json=row("Ansage 2", desktop=["musik"])).json()["item"]
            self.play(client, first["id"])
            agents.dead, agents.slow = {VIDEO_PC}, 1.0  # the video PC was switched off
            started = time.monotonic()
            answer = self.play(client, second["id"])
            self.assertLess(time.monotonic() - started, 0.8, "the new row does not wait for the stop")
            self.assertEqual([a["profile"] for a in answer["linked_actions"]], ["musik"])
            agents.wait_for(1)
            time.sleep(1.2)  # the stop timed out: the PC is marked down
            self.assertTrue(client.app.state.shown_profiles.is_down(VIDEO_PC))
            # Noted again (e.g. shown by hand just before it went off), then the
            # program moves on: the PC marked down is skipped, not waited for.
            client.app.state.shown_profiles.started("spotlight", VIDEO_PC, "file")
            calls = len(agents.calls)
            self.play(client, first_again["id"])
            time.sleep(0.2)
            self.assertEqual([c for c in agents.calls[calls:] if c[1] == "/close"], [])

    def test_old_agents_without_close_are_logged(self):
        agents = FakeAgents()

        def post(url, token, payload, timeout):
            if url.endswith("/close"):
                agents.calls.append((url, "/close", payload, timeout))
                raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
            return agents.post(url, token, payload, timeout)

        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", post):
            first = client.post("/program/items", json=row("Spotlight", desktop=["spotlight"])).json()["item"]
            second = client.post("/program/items", json=row("Predigt")).json()["item"]
            self.play(client, first["id"])
            with self.assertLogs("mixer.profile_stops", "WARNING") as logs:
                self.play(client, second["id"])
                agents.wait_for(1)
                time.sleep(0.1)
            self.assertIn("older than POST /close", "\n".join(logs.output))
            self.assertFalse(client.app.state.shown_profiles.is_down(VIDEO_PC))


@unittest.skipIf(tkinter is None, "the desktop agent needs tkinter")
class AgentCloseTests(unittest.TestCase):
    def setUp(self):
        agent._opened_windows.clear()

    def test_closes_only_the_profiles_own_windows_still_there(self):
        agent._remember_window("spotlight", 100, 7)
        agent._remember_window("spotlight", 101, 7)   # closed by hand since: gone
        agent._remember_window("spotlight", 102, 7)   # handle reused by another program
        agent._remember_window("other", 200, 9)
        owners = {100: 7, 101: 0, 102: 8, 200: 9}
        posted = []
        closed = agent._close_profile_windows("spotlight", owners.get, lambda hwnd: posted.append(hwnd) or True)
        self.assertEqual((closed, posted), (1, [100]))
        self.assertEqual(agent._close_profile_windows("spotlight", owners.get, posted.append), 0, "forgotten once closed")
        self.assertIn("other", agent._opened_windows)


if __name__ == "__main__":
    unittest.main()
