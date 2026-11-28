"""The stop key of a Desktop key that shows something (POST
/desktop/{id}/close, app/profile_stops.py close_profile): it closes the
image, video or document on its PC, started by hand or by the program,
naming the latest start (stamp); programs (OBS) have none; a PC that is off
fails fast. The agent's side says whether anything of it was open."""

import queue
import threading
import time
import unittest
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.row_files import start_id

from tests.helpers import app_client, require_tk

tkinter = require_tk()
if tkinter is not None:
    from desktop_agent import show_image_agent as agent

VIDEO_PC = "192.168.188.11"
CONFIG = (
    "desktop_display:\n  enabled: true\n  profiles:\n"
    f"    qr:\n      title: Gemeindegaben QR\n      kind: image\n      path: 'C:\\Bilder\\qr.png'\n      target_ip: {VIDEO_PC}\n"
    f"    spotlight:\n      title: Mission Spotlight\n      kind: file\n      path: 'C:\\Videos\\spot.mp4'\n      target_ip: {VIDEO_PC}\n"
    f"    doc:\n      title: Missionsgaben\n      kind: file\n      path: 'C:\\Texte\\gaben.pdf'\n      target_ip: {VIDEO_PC}\n"
    f"    obs:\n      title: OBS\n      kind: program\n      path: 'C:\\obs\\obs64.exe'\n      target_ip: {VIDEO_PC}\n"
    f"    musik:\n      title: Musik\n      kind: audio\n      path: 'C:\\Musik\\bg.mp3'\n      target_ip: {VIDEO_PC}\n"
)
ENV = {"DESKTOP_AGENT_TOKEN": "test-token"}


class Agents:
    def __init__(self, was_open=True):
        self.calls, self.dead, self.slow, self.was_open = [], False, 0.0, was_open

    def post(self, url, _token, payload, timeout):
        path = "/" + url.split("/", 3)[3]
        self.calls.append((path, payload, timeout))
        if self.dead:
            time.sleep(min(self.slow, timeout))
            raise urllib.error.URLError("timed out")
        if path == "/close":
            return {"ok": True, "profile": payload["id"], "windows_closed": 0, "was_open": self.was_open}
        return {"ok": True}

    def closes(self):
        return [payload for path, payload, _t in self.calls if path == "/close"]


class CloseKeyTests(unittest.TestCase):
    def test_image_video_and_document_close_on_their_pc(self):
        agents = Agents()
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            for profile in ("qr", "spotlight", "doc"):
                answer = client.post(f"/desktop/{profile}/close")
                self.assertEqual(answer.status_code, 200, answer.text)
                self.assertEqual((answer.json()["was_open"], agents.closes()[-1]), (True, {"id": profile}),
                                 "never started here (e.g. after a restart): closed by profile")

    def test_programs_and_music_have_no_close(self):
        agents = Agents()
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            self.assertEqual(client.post("/desktop/obs/close").status_code, 400, "OBS is never closed from here")
            self.assertEqual(client.post("/desktop/musik/close").status_code, 400)
            self.assertEqual(client.post("/desktop/nope/close").status_code, 404)
            self.assertEqual(agents.closes(), [])

    def test_the_close_names_the_latest_start(self):
        agents = Agents()
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            client.post("/desktop/spotlight/open")  # by hand
            stamp = [p for path, p, _t in agents.calls if path == "/display"][-1]["stamp"]
            client.post("/desktop/spotlight/close")
            self.assertEqual(agents.closes()[-1], {"id": "spotlight", "stamp": stamp},
                             "the agent ignores it if the video was started again since")

    def test_a_program_start_is_closed_and_forgotten(self):
        agents = Agents()
        via = "via:spotlight|C:\\Videos\\anderes.mp4"
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            item = client.post("/program/items", json={"time": "10:00", "title": "Spotlight", "group": "",
                                                       "desktop": ["qr", via]}).json()["item"]
            self.assertTrue(client.post(f"/program/items/{item['id']}/play").json()["ok"])
            self.assertEqual(set(client.app.state.shown_profiles.shown()),
                             {"qr", start_id("spotlight", "C:\\Videos\\anderes.mp4")})
            client.post("/desktop/spotlight/close")
            self.assertEqual({p["id"] for p in agents.closes()},
                             {"spotlight", start_id("spotlight", "C:\\Videos\\anderes.mp4")},
                             "the key's own start and the program's start of it with another file")
            self.assertEqual(set(client.app.state.shown_profiles.shown()), {"qr"}, "its program note is gone")

    def test_nothing_was_open(self):
        agents = Agents(was_open=False)
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            answer = client.post("/desktop/qr/close").json()
            self.assertEqual((answer["ok"], answer["was_open"]), (True, False))

    def test_a_pc_that_is_off_fails_fast(self):
        agents = Agents()
        agents.dead, agents.slow = True, 5.0
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            started = time.monotonic()
            answer = client.post("/desktop/spotlight/close")
            self.assertEqual((answer.status_code, answer.json()["reason"]), (502, "display agent unavailable"))
            self.assertLess(time.monotonic() - started, 3.0)
            self.assertLessEqual(agents.calls[-1][2], 2.0, "a short timeout")


@unittest.skipIf(tkinter is None, "the desktop agent needs tkinter")
class AgentCloseAnswerTests(unittest.TestCase):
    class Root:
        def withdraw(self):
            pass

    def close(self, profile_id, stamp=None):
        """POST /close with its tk thread answering (a thread here)."""
        sent = {}
        handler = SimpleNamespace(send_error=lambda code: sent.setdefault("error", code),
                                  _send_json=lambda status, data: sent.update(status=status, data=data))
        worker = threading.Thread(target=lambda: agent.handle_request(self.Root(), None, None,
                                                                      agent.requests.get(timeout=3)))
        worker.start()
        body = {"id": profile_id, **({"stamp": stamp} if stamp else {})}
        agent.DisplayHandler._handle_close(handler, body)
        worker.join(3)
        return sent["data"]

    def setUp(self):
        while not agent.requests.empty():
            agent.requests.get_nowait()
        agent._opened_windows.clear()
        agent._unknown_windows.clear()
        agent._opened_stamps.clear()
        agent._video_processes.clear()
        agent._shown_image = agent._shown_image_stamp = None

    def test_an_image_still_shown(self):
        agent._shown_image, agent._shown_image_stamp = "qr", "s1"
        answer = self.close("qr")
        self.assertEqual((answer["image_hidden"], answer["was_open"]), (True, True))
        self.assertIsNone(agent._shown_image)

    def test_another_image_or_a_newer_start_stays(self):
        agent._shown_image, agent._shown_image_stamp = "logo", "s1"
        self.assertEqual(self.close("qr")["was_open"], False, "War nicht offen")
        agent._shown_image, agent._shown_image_stamp = "qr", "s2"
        self.assertEqual(self.close("qr", stamp="s1")["image_hidden"], False, "shown again since: stays")
        self.assertEqual(agent._shown_image, "qr")

    def test_a_document(self):
        view = SimpleNamespace(profile_id="doc", stamp="s1", pages=Mock())
        with patch.object(agent, "_document_view", view), patch.object(agent, "_document_windows", []):
            answer = self.close("doc", stamp="s1")
        self.assertEqual((answer["document_hidden"], answer["was_open"]), (True, True))
        view.pages.close.assert_called_once()

    def test_an_mpv_video_window(self):
        with patch.object(agent, "_window_process_id", {100: 7}.get), \
                patch.object(agent, "_post_close", lambda hwnd: True):
            agent._remember_window("spotlight", 100, 7, {100: 7}.get)
            answer = self.close("spotlight")
        self.assertEqual((answer["windows_closed"], answer["was_open"]), (1, True))

    class Mpv:
        """The agent's own mpv process (its window may never have been found)."""
        pid = 4711

        def __init__(self):
            self.ended = False

        def poll(self):
            return 0 if self.ended else None

        def terminate(self):
            self.ended = True

    def test_an_mpv_whose_window_was_never_found_ends(self):
        mpv = self.Mpv()
        agent._video_processes["spotlight"] = mpv
        answer = self.close("spotlight")
        self.assertTrue(mpv.ended, "the agent's own process is ended")
        self.assertEqual((answer["windows_closed"], answer["was_open"]), (1, True))
        self.assertNotIn("spotlight", agent._video_processes)
        self.assertEqual(self.close("spotlight")["was_open"], False, "the second time: War nicht offen")

    def test_an_mpv_started_again_since_keeps_playing(self):
        mpv = self.Mpv()
        agent._video_processes["spotlight"] = mpv
        agent._opened_stamps["spotlight"] = "s2"
        answer = self.close("spotlight", stamp="s1")
        self.assertFalse(mpv.ended, "the close names an older start: the newer video plays on")
        self.assertIs(agent._video_processes["spotlight"], mpv)
        self.assertEqual(answer["windows_closed"], 0)

    def test_an_mpv_with_its_window_closes_by_its_window(self):
        mpv = self.Mpv()
        agent._video_processes["spotlight"] = mpv
        with patch.object(agent, "_window_process_id", {100: 4711}.get), \
                patch.object(agent, "_post_close", lambda hwnd: True):
            agent._remember_window("spotlight", 100, 4711, {100: 4711}.get)
            answer = self.close("spotlight")
        self.assertFalse(mpv.ended, "its window's close lets mpv end by itself")
        self.assertEqual(answer["windows_closed"], 1)

    def test_a_window_of_an_app_the_agent_did_not_start(self):
        agent._unknown_windows.add("spotlight")
        self.assertIsNone(self.close("spotlight")["was_open"], "cannot tell: no 'War nicht offen'")


if __name__ == "__main__":
    unittest.main()
