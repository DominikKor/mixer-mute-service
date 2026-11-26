"""When the program moves on, the desktop profiles the rows before started
stop (app/profile_stops.py): a video window closes, music fades out; what
the new row starts itself, and whatever the operator started by hand, keeps
running; an agent that is off never holds the new row up. The agent's side
(POST /close) is tested at the end.

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
                                      "fade": {"db": 0.0, "seconds": 4.0}, "only_if": True}),
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

    def test_what_the_operator_started_by_hand_keeps_running(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            self.assertTrue(client.post("/desktop/musik/audio/play").json()["ok"])
            self.assertTrue(client.post("/desktop/spotlight/open").json()["ok"])
            made = client.post("/shortcuts", json={"title": "QR", "desktop": ["qr"]}).json()["shortcut"]
            self.assertTrue(client.post(f"/shortcuts/{made['id']}/play").json()["ok"], "a shortcut key is by hand too")
            item = client.post("/program/items", json=row("Predigt")).json()["item"]
            self.play(client, item["id"])
            time.sleep(0.2)
            self.assertEqual(agents.stops(), [])
            self.assertEqual(client.app.state.shown_profiles.shown(), {})

    def test_the_operator_takes_over_what_the_program_started(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            first = client.post("/program/items", json=row("Spotlight", desktop=["spotlight", "musik", "qr"])).json()["item"]
            second = client.post("/program/items", json=row("Predigt")).json()["item"]
            self.play(client, first["id"])
            self.assertEqual(set(client.app.state.shown_profiles.shown()), {"spotlight", "musik", "qr"})
            # The operator presses the video's and the music's Desktop keys.
            client.post("/desktop/spotlight/open")
            client.post("/desktop/musik/audio/play")
            self.play(client, second["id"])
            self.assertEqual(agents.wait_for(1), [(VIDEO_PC, "/close", {"id": "qr"})],
                             "only what is still the program's stops")
            time.sleep(0.1)
            self.assertEqual(len(agents.stops()), 1)

    def test_file_audio_of_the_next_row_on_the_same_pc_is_not_stopped(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            first = client.post("/program/items", json=row("Vorspiel", desktop=["musik"])).json()["item"]
            jingle = f"file:{MUSIC_PC}|C:\\Musik\\jingle.mp3"
            second = client.post("/program/items", json=row("Jingle", playback=jingle)).json()["item"]
            self.play(client, first["id"])
            self.play(client, second["id"])
            time.sleep(0.2)
            self.assertEqual(agents.stops(), [], "the jingle replaces the music; a stop would end the jingle")
            plays = [(pc, p["id"]) for pc, path, p, _t in agents.calls if path == "/audio" and p["action"] == "play"]
            self.assertEqual(plays, [(MUSIC_PC, "musik"), (MUSIC_PC, "program-file")])

    def test_a_profile_still_running_is_left_alone_by_the_next_row(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            first = client.post("/program/items", json=row("Spotlight", desktop=["musik", "spotlight"])).json()["item"]
            second = client.post("/program/items", json=row("Ansage", desktop=["musik", "spotlight", "qr"])).json()["item"]
            self.play(client, first["id"])
            agents.calls.clear()
            answer = self.play(client, second["id"])
            self.assertEqual([(path, p["id"]) for _pc, path, p, _t in agents.calls], [("/display", "qr")],
                             "no new fade-in for the music, no reopened video")
            kept = [a for a in answer["linked_actions"] if a.get("kept_running")]
            self.assertEqual([a["profile"] for a in kept], ["musik", "spotlight"])
            # Played again on purpose: the same row starts them anew.
            agents.calls.clear()
            self.play(client, second["id"])
            self.assertEqual(sorted(p["id"] for _pc, _path, p, _t in agents.calls), ["musik", "qr", "spotlight"])

    def test_a_refused_new_track_keeps_the_old_one_noted(self):
        agents = FakeAgents()

        def post(url, token, payload, timeout):
            if payload.get("id") == "lied":
                agents.calls.append((url, "/audio", payload, timeout))
                raise urllib.error.HTTPError(url, 503, "no audio device", None, None)
            return agents.post(url, token, payload, timeout)

        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", post):
            first = client.post("/program/items", json=row("Vorspiel", desktop=["musik"])).json()["item"]
            second = client.post("/program/items", json=row("Lied", desktop=["lied"])).json()["item"]
            third = client.post("/program/items", json=row("Predigt")).json()["item"]
            self.play(client, first["id"])
            client.post(f"/program/items/{second['id']}/play")  # the new track is refused
            self.assertEqual(client.app.state.shown_profiles.shown(), {"musik": (MUSIC_PC, "audio")})
            self.play(client, third["id"])
            self.assertEqual([(pc, p["id"]) for pc, _path, p in agents.wait_for(1)], [(MUSIC_PC, "musik")])

    def test_a_late_failed_stop_does_not_take_back_a_start_by_hand(self):
        agents = FakeAgents()

        def post(url, token, payload, timeout):
            if url.endswith("/close"):  # the video PC hangs on the close
                agents.calls.append((url, "/close", payload, timeout))
                time.sleep(0.5)
                raise urllib.error.URLError("timed out")
            return agents.post(url, token, payload, timeout)

        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", post):
            first = client.post("/program/items", json=row("Spotlight", desktop=["spotlight"])).json()["item"]
            second = client.post("/program/items", json=row("Predigt")).json()["item"]
            self.play(client, first["id"])
            self.play(client, second["id"])
            time.sleep(0.1)  # the close is on its way ...
            self.assertTrue(client.post("/desktop/spotlight/open").json()["ok"], "... the operator shows it by hand")
            time.sleep(0.6)
            self.assertEqual(client.app.state.shown_profiles.shown(), {},
                             "the failed stop does not note it again: it is the operator's now")
            opens = [i for i, call in enumerate(agents.calls) if call[1] in ("/close", "/display") and call[2]["id"] == "spotlight"]
            self.assertEqual([agents.calls[i][1] for i in opens], ["/display", "/close", "/display"],
                             "the start by hand waited for the close on its way")

    def test_a_slow_agent_at_the_start_does_not_lose_the_profile(self):
        agents = FakeAgents(dead={VIDEO_PC})
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            first = client.post("/program/items", json=row("Spotlight", desktop=["spotlight"])).json()["item"]
            second = client.post("/program/items", json=row("Predigt")).json()["item"]
            client.post(f"/program/items/{first['id']}/play")  # no answer in time: it may have started
            self.assertIn("spotlight", client.app.state.shown_profiles.shown())
            agents.dead = set()
            self.play(client, second["id"])
            self.assertEqual(agents.wait_for(1), [(VIDEO_PC, "/close", {"id": "spotlight"})])

    def test_a_refused_start_is_not_noted(self):
        def post(url, token, payload, timeout):
            raise urllib.error.HTTPError(url, 503, "Configured target is not available", None, None)

        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", post):
            first = client.post("/program/items", json=row("Spotlight", desktop=["spotlight"])).json()["item"]
            client.post(f"/program/items/{first['id']}/play")
            self.assertEqual(client.app.state.shown_profiles.shown(), {})

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
            self.assertIn("spotlight", client.app.state.shown_profiles.shown(),
                          "kept: the next row tries again once the PC answers")
            # Noted again (e.g. shown by hand just before it went off), then the
            # program moves on: the PC marked down is skipped, not waited for.
            client.app.state.shown_profiles._shown["spotlight"] = (VIDEO_PC, "file", "earlier")
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
        agent._unknown_windows.clear()

    def test_only_the_started_process_windows_are_remembered(self):
        owners = {100: 7, 200: 9}
        self.assertTrue(agent._remember_if_own("spotlight", 100, 7, owners.get))
        self.assertFalse(agent._remember_if_own("spotlight", 200, 7, owners.get),
                         "another app's window (OBS?) is never closed later")
        self.assertFalse(agent._remember_if_own("video", 200, 0, owners.get),
                         "handed to an app that was already running: unknown")
        self.assertEqual(agent._opened_windows, {"spotlight": {(100, 7)}})
        agent._remember_unknown("video")
        with self.assertLogs(agent.logger, "WARNING") as logs:
            self.assertEqual(agent._close_profile_windows("video", owners.get, lambda h: True), 0)
        self.assertIn("not known", "\n".join(logs.output))

    def test_remembered_windows_that_are_gone_are_pruned(self):
        agent._remember_window("old", 300, 5, {300: 5}.get)
        agent._remember_window("new", 400, 6, {400: 6}.get)  # 300 closed since
        self.assertEqual(agent._opened_windows, {"new": {(400, 6)}})

    def test_close_hides_only_that_profiles_image(self):
        class Root:
            hidden = 0

            def withdraw(self):
                Root.hidden += 1

        root = Root()
        agent._shown_image = "qr"
        agent.handle_request(root, None, None, {"kind": "close", "id": "logo"})
        self.assertEqual(Root.hidden, 0, "a newer image stays")
        agent.handle_request(root, None, None, {"kind": "close", "id": "qr"})
        self.assertEqual((Root.hidden, agent._shown_image), (1, None))
        agent.handle_request(root, None, None, {"kind": "close", "id": "qr"})
        self.assertEqual(Root.hidden, 1, "already hidden")

    def test_a_targeted_audio_stop_spares_a_newer_track(self):
        class Playback:
            profile_id = "lied"

        with patch.object(agent, "_playback", Playback()), patch.object(agent, "_stop_audio_locked") as stop:
            self.assertFalse(agent._stop_audio(None, only_profile="musik"))
            stop.assert_not_called()
            agent._stop_audio(None, only_profile="lied")
            stop.assert_called_once()

    def test_closes_only_the_profiles_own_windows_still_there(self):
        owners = {100: 7, 101: 7, 102: 7, 200: 9}
        agent._remember_window("spotlight", 100, 7, owners.get)
        agent._remember_window("spotlight", 101, 7, owners.get)
        agent._remember_window("spotlight", 102, 7, owners.get)
        agent._remember_window("other", 200, 9, owners.get)
        owners.update({101: 0, 102: 8})  # 101 closed by hand since, 102's handle now another program's
        posted = []
        closed = agent._close_profile_windows("spotlight", owners.get, lambda hwnd: posted.append(hwnd) or True)
        self.assertEqual((closed, posted), (1, [100]))
        self.assertEqual(agent._close_profile_windows("spotlight", owners.get, posted.append), 0, "forgotten once closed")
        self.assertIn("other", agent._opened_windows)


if __name__ == "__main__":
    unittest.main()
