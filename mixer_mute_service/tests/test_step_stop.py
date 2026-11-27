"""A step's stop key in the running program row (POST /program/stop
{"step": start id}, app/profile_stops.py stop_started): it stops that one
step the program started (music fades out, a window closes) and leaves the
row's other steps running; stamp-safe; a PC that is off does not hang it.
The display agents are faked."""

import time
import unittest
import urllib.error
from unittest.mock import patch

from app.row_files import start_id

from tests.helpers import app_client

VIDEO_PC, MUSIC_PC = "192.168.188.11", "192.168.188.20"
CONFIG = (
    "desktop_display:\n  enabled: true\n  profiles:\n"
    f"    musik:\n      title: Hintergrundmusik\n      kind: audio\n      path: 'C:\\Musik\\bg.mp3'\n      target_ip: {MUSIC_PC}\n"
    "      fade: true\n      fade_seconds: 4\n"
    f"    video:\n      title: Einspieler\n      kind: file\n      path: 'C:\\Videos\\spot.mp4'\n      target_ip: {VIDEO_PC}\n"
    f"    qr:\n      title: QR\n      kind: image\n      path: 'C:\\Bilder\\qr.png'\n      target_ip: {VIDEO_PC}\n"
)
ENV = {"DESKTOP_AGENT_TOKEN": "test-token"}
OTHER_FILM = "C:\\Videos\\anderer.mp4"


def row(title, **extra):
    return {"time": "10:00", "title": title, "person": "", "group": "", **extra}


class Agents:
    def __init__(self):
        self.calls, self.dead, self.slow = [], set(), 0.0

    def post(self, url, _token, payload, timeout):
        pc, path = url.removeprefix("http://").split(":", 1)
        path = "/" + path.split("/", 1)[1]
        self.calls.append((pc, path, payload))
        if pc in self.dead:
            time.sleep(min(self.slow, timeout))
            raise urllib.error.URLError("timed out")
        return {"ok": True}

    def stops(self):
        return [(pc, path, payload) for pc, path, payload in self.calls
                if path == "/close" or (path == "/audio" and payload.get("action") == "stop")]

    def starts(self):
        return [(pc, path, payload) for pc, path, payload in self.calls
                if path == "/display" or (path == "/audio" and payload.get("action") == "play")]


class StepStopTests(unittest.TestCase):
    def start_row(self, client, steps):
        item = client.post("/program/items", json=row("Einspieler", desktop=steps)).json()
        self.assertIn("item", item, item)
        played = client.post(f"/program/items/{item['item']['id']}/play").json()
        self.assertTrue(played["ok"], played)
        return item["item"]["id"]

    def running(self, client):
        return {entry["step"]: (entry["start"], entry["kind"]) for entry in client.get("/program/status").json()["running"]}

    def test_one_step_stops_the_others_keep_running(self):
        agents = Agents()
        via = f"via:video|{OTHER_FILM}"
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            self.start_row(client, ["musik", via, "qr"])
            self.assertEqual(self.running(client), {
                "musik": ("musik", "audio"),
                via: (start_id("video", OTHER_FILM), "file"),
                "qr": ("qr", "image"),
            }, "the running row lists its steps with the id the agent knows them by")
            answer = client.post("/program/stop", json={"step": "musik"})
            self.assertEqual(answer.status_code, 200, answer.text)
            stop = agents.stops()[-1]
            self.assertEqual((stop[0], stop[1], stop[2]["id"], stop[2]["only_if"], stop[2]["fade"]["seconds"]),
                             (MUSIC_PC, "/audio", "musik", True, 4.0), "the music fades out with its profile's fade")
            self.assertEqual(len(agents.stops()), 1, "nothing else is stopped")
            self.assertEqual(set(self.running(client)), {via, "qr"})
            client.post("/program/stop", json={"step": start_id("video", OTHER_FILM)})
            self.assertEqual(agents.stops()[-1][:2], (VIDEO_PC, "/close"))
            self.assertEqual(agents.stops()[-1][2]["id"], start_id("video", OTHER_FILM), "the key with its own file")
            self.assertEqual(set(self.running(client)), {"qr"})

    def test_a_stop_names_the_start_it_means(self):
        agents = Agents()
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            self.start_row(client, ["video"])
            started = agents.starts()[-1][2]["stamp"]
            client.post("/program/stop", json={"step": "video"})
            self.assertEqual(agents.stops()[-1][2], {"id": "video", "stamp": started},
                             "the agent ignores it if the video was started again since")
            again = client.post("/program/stop", json={"step": "video"})
            self.assertEqual(again.status_code, 404, "stopped: no longer the program's")
            # Started by hand meanwhile: the operator's, its key is gone.
            self.start_row(client, ["musik"])
            client.post("/desktop/musik/audio/play")
            self.assertEqual(client.post("/program/stop", json={"step": "musik"}).status_code, 404)
            self.assertEqual(self.running(client), {})
            self.assertEqual(client.post("/program/stop", json={"step": ""}).status_code, 400)

    def test_a_pc_that_is_off(self):
        agents = Agents()
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            self.start_row(client, ["video", "musik"])
            agents.dead, agents.slow = {VIDEO_PC}, 1.0
            started = time.monotonic()
            answer = client.post("/program/stop", json={"step": "video"})
            self.assertEqual((answer.status_code, answer.json()["reason"]), (502, "display agent unavailable"))
            self.assertLess(time.monotonic() - started, 2.5, "it gives up after the short timeout")
            self.assertIn("video", self.running(client), "still noted: the next row (or key) tries again")
            started = time.monotonic()
            self.assertEqual(client.post("/program/stop", json={"step": "video"}).status_code, 502)
            self.assertLess(time.monotonic() - started, 0.3, "marked down: no second wait")
            self.assertEqual(client.post("/program/stop", json={"step": "musik"}).status_code, 200,
                             "another PC is not affected")

    def test_only_the_running_row_lists_steps(self):
        agents = Agents()
        with app_client(CONFIG, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            self.assertEqual(client.get("/program/status").json()["running"], [])
            self.start_row(client, ["musik"])
            item = client.post("/program/items", json=row("Ohne", time="11:00")).json()["item"]
            client.post(f"/program/items/{item['id']}/play")
            time.sleep(0.2)
            self.assertEqual(self.running(client), {}, "the next row started: the music was stopped, nothing listed")


if __name__ == "__main__":
    unittest.main()
