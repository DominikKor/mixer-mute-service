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

from app.row_files import start_id
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
    f"    ms:\n      title: Mission Spotlight Woche\n      kind: file\n      path: 'I:\\Mission Spotlight'\n"
    f"      target_ip: {VIDEO_PC}\n      mission_spotlight: true\n"
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
        """The stops sent, without their (random) start stamps."""
        return [(pc, path, {k: v for k, v in payload.items() if k != "stamp"}) for pc, path, payload, _t in self.calls
                if path == "/close" or (path == "/audio" and payload["action"] == "stop")]

    def starts(self):
        return [(pc, path, payload) for pc, path, payload, _t in self.calls
                if path == "/display" or (path == "/audio" and payload["action"] == "play")]

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
            # The old file field became a step through a music key on that PC, the
            # one without fade (app/row_files.py): tracked as key and file.
            self.assertEqual(plays, [(MUSIC_PC, "musik"), (MUSIC_PC, start_id("lied", "C:\\Musik\\jingle.mp3"))])

    def test_what_the_next_row_starts_again_is_sent_with_keep(self):
        # The agent decides: music that still plays and a video window still
        # open stay as they are; one that ended starts again; images show again.
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            first = client.post("/program/items", json=row("Spotlight", desktop=["musik", "spotlight", "qr"])).json()["item"]
            second = client.post("/program/items", json=row("Ansage", desktop=["musik", "spotlight", "qr"])).json()["item"]
            self.play(client, first["id"])
            agents.calls.clear()
            self.play(client, second["id"])
            sent = {p["id"]: p for _pc, _path, p in agents.starts()}
            self.assertEqual(set(sent), {"musik", "spotlight", "qr"}, "always sent: the agent knows what still runs")
            self.assertTrue(sent["musik"]["keep_if_playing"])
            self.assertTrue(sent["spotlight"]["keep_if_open"])
            self.assertNotIn("keep_if_open", sent["qr"], "an image is simply shown again")
            self.assertEqual(agents.stops(), [])
            # By hand: no keep, the operator means to start it (anew).
            agents.calls.clear()
            client.post("/desktop/musik/audio/play")
            self.assertNotIn("keep_if_playing", agents.starts()[0][2])

    def test_a_window_left_open_by_hand_is_opened_again(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            item = client.post("/program/items", json=row("Spotlight", desktop=["spotlight"])).json()["item"]
            client.post("/desktop/spotlight/open")  # by hand
            agents.calls.clear()
            self.play(client, item["id"])
            sent = agents.starts()[0][2]
            self.assertNotIn("keep_if_open", sent, "not the program's window: the row opens it again")
            # The program's own window from now on: the next row keeps it.
            agents.calls.clear()
            again = client.post("/program/items", json=row("Weiter", desktop=["spotlight"])).json()["item"]
            self.play(client, again["id"])
            self.assertTrue(agents.starts()[0][2]["keep_if_open"])

    def test_a_timed_out_start_is_stopped_by_profile_alone(self):
        for profile, pc in (("spotlight", VIDEO_PC), ("musik", MUSIC_PC)):
            agents = FakeAgents(dead={pc})
            with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
                first = client.post("/program/items", json=row("Erst", desktop=[profile])).json()["item"]
                second = client.post("/program/items", json=row("Predigt")).json()["item"]
                client.post(f"/program/items/{first['id']}/play")  # no answer in time: it may run all the same
                self.assertEqual(set(client.app.state.shown_profiles.shown()), {profile})
                agents.dead = set()
                self.play(client, second["id"])
                agents.wait_for(1)
                stop = next(payload for _pc, path, payload, _t in agents.calls
                            if path == "/close" or payload.get("action") == "stop")
                self.assertNotIn("stamp", stop, f"{profile}: whatever start it was")
                self.assertEqual(stop["id"], profile)

    def test_video_starts_need_no_player_field(self):
        # The agent itself plays every video in mpv when it has mpv.exe, so
        # the service sends a video start as any other: an older agent
        # accepts it unchanged.
        calls = []

        def post(url, token, payload, timeout):
            calls.append(payload)
            return {"ok": True}

        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", post):
            self.assertTrue(client.post("/desktop/ms/open").json()["ok"])
            self.assertNotIn("player", calls[-1])
            self.assertTrue(calls[-1]["path"].startswith("I:\\Mission Spotlight\\"), calls[-1]["path"])

        def old_agent(url, token, payload, timeout):
            calls.append(payload)
            if "stamp" in payload:
                raise urllib.error.HTTPError(url, 400, "Bad Request", None, None)
            return {"ok": True}

        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", old_agent):
            calls.clear()
            with self.assertLogs("mixer.actions", "WARNING") as logs:
                self.assertTrue(client.post("/desktop/ms/open").json()["ok"])
            self.assertEqual(["stamp" in c for c in calls], [True, False], "an older agent: sent again without")
            self.assertIn("rejected stamp; sent again without", "\n".join(logs.output))

    def test_jingle_then_the_music_again(self):
        # Row A music, row B a jingle (file field) on the same PC, row C the
        # music again: C must start it (the jingle replaced it).
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            a = client.post("/program/items", json=row("Vorspiel", desktop=["musik"])).json()["item"]
            b = client.post("/program/items", json=row("Jingle", playback=f"file:{MUSIC_PC}|C:\\Musik\\jingle.mp3")).json()["item"]
            c = client.post("/program/items", json=row("Nachspiel", desktop=["musik"])).json()["item"]
            self.play(client, a["id"])
            self.play(client, b["id"])
            jingle = start_id("lied", "C:\\Musik\\jingle.mp3")
            self.assertEqual(client.app.state.shown_profiles.shown(), {jingle: (MUSIC_PC, "audio")},
                             "the jingle (the program's now) replaced the program's music")
            self.play(client, c["id"])
            time.sleep(0.2)
            plays = [p["id"] for _pc, path, p in agents.starts() if path == "/audio"]
            self.assertEqual(plays, ["musik", jingle, "musik"])
            self.assertEqual(agents.stops(), [])

    def test_an_image_is_shown_again(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            first = client.post("/program/items", json=row("QR", desktop=["qr"])).json()["item"]
            second = client.post("/program/items", json=row("QR noch mal", desktop=["qr"])).json()["item"]
            self.play(client, first["id"])
            self.play(client, second["id"])
            self.assertEqual([p["id"] for _pc, _path, p in agents.starts()], ["qr", "qr"])
            self.assertEqual(client.app.state.shown_profiles.shown(), {"qr": (VIDEO_PC, "image")})

    def test_older_agents_get_the_start_without_the_new_fields(self):
        calls = []

        def post(url, token, payload, timeout):
            calls.append(payload)
            if "stamp" in payload:
                raise urllib.error.HTTPError(url, 400, "Bad Request", None, None)
            return {"ok": True}

        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", post):
            item = client.post("/program/items", json=row("Musik", desktop=["musik"])).json()["item"]
            self.play(client, item["id"])
            self.assertEqual([("stamp" in c, "keep_if_playing" in c) for c in calls], [(True, True), (False, False)])
            self.assertEqual(client.app.state.shown_profiles.shown(), {"musik": (MUSIC_PC, "audio")})

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
                agents.calls.append((VIDEO_PC, "/close", payload, timeout))
                time.sleep(0.5)
                raise urllib.error.URLError("timed out")
            return agents.post(url, token, payload, timeout)

        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", post):
            first = client.post("/program/items", json=row("Spotlight", desktop=["spotlight"])).json()["item"]
            second = client.post("/program/items", json=row("Predigt")).json()["item"]
            self.play(client, first["id"])
            self.play(client, second["id"])
            time.sleep(0.1)  # the close is on its way ...
            started = time.monotonic()
            self.assertTrue(client.post("/desktop/spotlight/open").json()["ok"], "... the operator shows it by hand")
            self.assertLess(time.monotonic() - started, 0.3, "a start never waits for a stop")
            time.sleep(0.6)
            self.assertEqual(client.app.state.shown_profiles.shown(), {},
                             "the failed stop does not note it again: it is the operator's now")
            close = next(p for _pc, path, p, _t in agents.calls if path == "/close")
            by_hand = [p for _pc, path, p in agents.starts() if path == "/display"][-1]
            self.assertNotEqual(close["stamp"], by_hand["stamp"],
                                "the close names the program's start: the agent ignores it for the newer one")

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
            client.app.state.shown_profiles._shown["spotlight"] = (VIDEO_PC, "file", "earlier", True)
            client.app.state.shown_profiles._latest["spotlight"] = "earlier"
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
        agent._opened_stamps.clear()
        agent._cancelled.clear()

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
        agent._shown_image, agent._shown_image_stamp = "qr", "s2"
        agent.handle_request(root, None, None, {"kind": "close", "id": "logo"})
        self.assertEqual(Root.hidden, 0, "a newer image stays")
        agent.handle_request(root, None, None, {"kind": "close", "id": "qr", "stamp": "s1"})
        self.assertEqual(Root.hidden, 0, "a close for an earlier showing of the same image is ignored")
        agent.handle_request(root, None, None, {"kind": "close", "id": "qr", "stamp": "s2"})
        self.assertEqual(Root.hidden, 1)
        agent._shown_image = "qr"
        agent.handle_request(root, None, None, {"kind": "close", "id": "qr"})
        self.assertEqual((Root.hidden, agent._shown_image), (2, None))
        agent.handle_request(root, None, None, {"kind": "close", "id": "qr"})
        self.assertEqual(Root.hidden, 2, "already hidden")

    def test_a_targeted_audio_stop_spares_a_newer_track(self):
        class Playback:
            profile_id = "lied"
            stamp = "s2"

        with patch.object(agent, "_playback", Playback()), patch.object(agent, "_stop_audio_locked") as stop:
            self.assertFalse(agent._stop_audio(None, only_profile="musik"))
            self.assertFalse(agent._stop_audio(None, only_profile="lied", stamp="s1"), "an earlier start of it")
            stop.assert_not_called()
            agent._stop_audio(None, only_profile="lied", stamp="s2")
            stop.assert_called_once()

    def test_keep_if_playing(self):
        class Process:
            code = None

            def poll(self):
                return self.code

        class Playback:
            profile_id = "musik"
            stamp = "s1"
            process = Process()

        playback = Playback()
        with patch.object(agent, "_playback", playback), \
                patch.object(agent, "_audio_target", side_effect=RuntimeError("started")) as start:
            self.assertTrue(agent._play_audio("musik", "C:\\a.mp3", None, stamp="s2", keep_if_playing=True))
            self.assertEqual(playback.stamp, "s2", "kept, with the new start's stamp")
            start.assert_not_called()
            with self.assertRaisesRegex(RuntimeError, "started"):
                agent._play_audio("lied", "C:\\b.mp3", None, stamp="s3", keep_if_playing=True)
            playback.process.code = 0  # the music ended
            with self.assertRaisesRegex(RuntimeError, "started", msg="music that ended starts again"):
                agent._play_audio("musik", "C:\\a.mp3", None, stamp="s4", keep_if_playing=True)

    def test_keep_if_open_and_close_by_stamp(self):
        owners = {100: 7}
        agent._remember_window("spotlight", 100, 7, owners.get)
        self.assertTrue(agent._keep_open("spotlight", "s1", owners.get))
        self.assertEqual(agent._opened_stamps["spotlight"], "s1", "kept, with the new start's stamp")
        owners[100] = 0  # closed on the PC (Escape, the player ended)
        self.assertFalse(agent._keep_open("spotlight", "s9", owners.get), "opened again then")
        self.assertEqual(agent._opened_stamps["spotlight"], "s1")
        owners[100] = 7
        agent._opened_stamps["spotlight"] = "s2"
        posted = []
        self.assertEqual(agent._close_profile_windows("spotlight", owners.get, posted.append, stamp="s1"), 0)
        self.assertEqual((posted, set(agent._opened_windows)), ([], {"spotlight"}), "opened again since: stays")
        self.assertEqual(agent._close_profile_windows("spotlight", owners.get, lambda h: posted.append(h) or True,
                                                      stamp="s2"), 1)

    def test_a_window_still_being_opened_is_kept(self):
        agent._launching["ms"] = (agent.time.monotonic() + 5, "t1")
        try:
            self.assertTrue(agent._keep_open("ms", "s2", {}.get), "no second window within the first's start")
            agent._cancelled.add("ms")
            self.assertFalse(agent._keep_open("ms", "s3", {}.get), "closed meanwhile: opened again")
        finally:
            agent._launching.clear()
            agent._cancelled.clear()
        self.assertFalse(agent._keep_open("ms", "s3", {}.get))

    def test_a_late_launch_leaves_a_newer_one_alone(self):
        agent._launching["ms"] = (agent.time.monotonic() + 5, "new")
        agent._cancelled.add("ms")
        profile = {"id": "ms", "kind": "file", "path": "I:/x.url", "monitor": 1, "fullscreen": False,
                   "position": "top-left", "args": [], "launch_token": "old"}
        try:
            with patch.object(agent, "_visible_windows", side_effect=RuntimeError("test")), \
                    self.assertLogs(agent.logger, "ERROR"):
                agent._launch_profile(profile)  # the older launch ends (here: fails)
            self.assertEqual((agent._launching["ms"][1], "ms" in agent._cancelled), ("new", True))
            with patch.object(agent, "_visible_windows", side_effect=RuntimeError("test")), \
                    self.assertLogs(agent.logger, "ERROR"):
                agent._launch_profile({**profile, "launch_token": "new"})
            self.assertEqual(("ms" in agent._launching, "ms" in agent._cancelled), (False, False))
        finally:
            agent._launching.clear()
            agent._cancelled.clear()

    def test_only_a_real_mpv_plays_videos(self):
        from pathlib import Path as WinPath
        video = {"kind": "file", "title": "Spotlight", "monitor": 2, "fullscreen": True, "position": "top-left"}
        real = "C:\\Program Files\\MPV Player\\mpv.exe"
        with patch.object(agent, "_play_mpv", return_value=real):
            self.assertEqual(agent._video_player(video, WinPath("I:/MS/01_Video.mp4")), real,
                             "every video, not only Mission Spotlight")
            self.assertIsNone(agent._video_player(video, WinPath("I:/MS/01_Folien.pdf")), "not a video")
            self.assertIsNone(agent._video_player({**video, "kind": "program"}, WinPath("I:/MS/x.mp4")))
        for other in (None, "C:\\Program Files\\MPV.NET\\mpvnet.exe",
                      "C:\\Users\\x\\AppData\\Local\\Microsoft\\WindowsApps\\mpv.exe"):
            with patch.object(agent, "_play_mpv", return_value=other), self.assertLogs(agent.logger, "WARNING"):
                self.assertIsNone(agent._video_player(video, WinPath("I:/MS/01_Video.mp4")), f"default app: {other}")

    def test_mpv_command_picks_the_screen_by_its_number(self):
        from pathlib import Path as WinPath
        video = {"title": "Spotlight", "monitor": 2, "fullscreen": True, "position": "top-left"}
        with patch.object(agent, "agent_log_directory", return_value=WinPath("/tmp")):
            command = agent._video_command("mpv.exe", WinPath("I:/MS/01_Video.mp4"), video, 0)
            self.assertTrue({"--screen=0", "--fs-screen=0", "--fs", "--keep-open=yes"} <= set(command), command)
            self.assertFalse(any("screen-name" in c or c.startswith("--geometry") for c in command),
                             "mpv on Windows knows screens by number only")
            self.assertEqual(command[-2:], ["--", str(WinPath("I:/MS/01_Video.mp4"))])
            framed = agent._video_command("mpv.exe", WinPath("I:/v.mp4"), {**video, "fullscreen": False,
                                                                           "position": "bottom-right"}, 1)
            self.assertIn("--screen=1", framed)
            self.assertIn("--geometry=100%:100%", framed)
            self.assertNotIn("--fs", framed)
            self.assertIn("--fs", agent._video_command("mpv.exe", WinPath("I:/v.mp4"), video, None))

    def test_monitor_n_maps_to_mpvs_screen_number(self):
        # Windows reports the right monitor first: our "monitor 2" (sorted
        # left to right, as for images) is mpv's screen 0.
        raw = [((1920, 0, 3840, 1080), (1920, 0, 3840, 1080), "\\\\.\\DISPLAY1", 0),
               ((0, 0, 1920, 1080), (0, 0, 1920, 1040), "\\\\.\\DISPLAY2", 1)]
        ordered = agent._order_monitors(raw)
        self.assertEqual([entry[3] for entry in ordered], [1, 0])
        with patch.object(agent, "_monitors", return_value=ordered):
            self.assertEqual(agent.mpv_screen(2), (0, "\\\\.\\DISPLAY1"))
            self.assertEqual(agent.mpv_screen(1), (1, "\\\\.\\DISPLAY2"))
            self.assertEqual(agent.monitor_bounds(2), (1920, 0, 3840, 1080), "the same monitor as for images")
            with self.assertRaises(RuntimeError):
                agent.mpv_screen(3)

    def test_a_new_video_start_ends_the_one_still_playing(self):
        from pathlib import Path as WinPath
        owners = {100: 7, 200: 8}
        agent._remember_window("ms", 100, 7, owners.get)
        closed, started = [], []

        class Process:
            pid = 8

        def popen(command, **_kwargs):
            started.append(command)
            self.assertEqual(closed, [100], "the old player is closed before the new one starts")
            return Process()

        class Starting:
            pid = 6
            ended = False

            def poll(self):
                return None

            def terminate(self):
                Starting.ended = True

        agent._video_processes["ms"] = Starting()  # an earlier start, its window not found yet
        video = {"id": "ms", "title": "Spotlight", "monitor": 2, "fullscreen": True, "position": "top-left"}
        with patch.object(agent, "_window_process_id", owners.get), patch.object(agent, "_post_close",
                                                                               lambda h: closed.append(h) or True), \
                patch.object(agent, "mpv_screen", return_value=(0, "\\\\.\\DISPLAY2")), \
                patch.object(agent, "agent_log_directory", return_value=WinPath("/tmp")), \
                patch.object(agent.subprocess, "Popen", popen), \
                patch.object(agent, "_find_window", return_value=200), \
                self.assertLogs(agent.logger, "INFO") as logs:
            agent._launch_video("mpv.exe", WinPath("I:/MS/01_Video.mp4"), video, set())
        self.assertEqual(agent._opened_windows, {"ms": {(200, 8)}}, "the new window is remembered, not placed")
        self.assertTrue(Starting.ended, "the earlier mpv still starting is ended too")
        self.assertIsInstance(agent._video_processes["ms"], Process)
        agent._video_processes.clear()
        text = "\n".join(logs.output)
        self.assertIn("mpv screen 0, \\\\.\\DISPLAY2", text, "the screen number and the device name are logged")
        self.assertIn("--fs-screen=0", text, "the full command line is in the log")

    def test_a_close_while_opening_closes_the_window_once_it_is_there(self):
        agent._launching["ms"] = (agent.time.monotonic() + 5, "t1")
        try:
            self.assertEqual(agent._close_profile_windows("ms", {}.get, lambda h: True), 0)
            self.assertIn("ms", agent._cancelled)
            closed = []
            with patch.object(agent, "_window_process_id", {300: 9}.get), \
                    patch.object(agent, "_post_close", lambda h: closed.append(h) or True):
                agent._remember_video_window("ms", 9, set(), find=lambda pid, before: 300)
            self.assertEqual((closed, "ms" in agent._cancelled), ([300], False))
        finally:
            agent._launching.clear()
            agent._cancelled.clear()

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
