"""Song lyrics on several screens (app/songs.py) with fake display agents.

Verifies: settings from before several screens (one PC and monitor) load as
a one-screen list, also when an older page saves "monitor" only; one screen
per PC and 1..4 screens; every show, blank and hide goes to all screens in
parallel, each with its own monitor; one screen failing does not stop the
others (the answer and /songs/live name it with its reason, the live state
moves on), all failing leaves the live state alone; the library stays on its
own PC. No request leaves the process (display_agent._post is replaced).

Run from mixer_mute_service:  python -m tests.test_song_screens
"""

import json
import os
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path

from tests.helpers import use_app_config  # noqa: E402

_tmp = use_app_config("mixer_song_screens_",
    "desktop_display:\n  enabled: true\n  token_env: SONG_SCREENS_TOKEN\n  profiles: {}\n")
os.environ["SONG_SCREENS_TOKEN"] = "test-token"

from fastapi.testclient import TestClient  # noqa: E402

import app.display_agent as display_agent  # noqa: E402
from app.main import app  # noqa: E402
from app.songs import SongService, SongSettings  # noqa: E402

SONG = """<?xml version="1.0" encoding="UTF-8"?>
<foilpresenterfolie version="00300.000105"><titel><titelstring>Testlied</titelstring></titel>
<strophen><strophe><key>1</key><text>Eins</text></strophe><strophe><key>2</key><text>Zwei</text></strophe></strophen>
</foilpresenterfolie>"""

ANZEIGE, STREAMING = "192.168.188.11", "192.168.188.20"


class FakeAgents:
    """Display agents by IP. ``down`` IPs are unreachable; with ``barrier``
    every /song call waits for the others (only parallel calls get through)."""

    def __init__(self):
        self.calls = []
        self.down = set()
        self.barrier = None
        self.lock = threading.Lock()

    def __call__(self, url, token, payload, timeout):
        ip = url.split("/")[2].split(":")[0]
        with self.lock:
            self.calls.append((ip, url.rsplit("/", 1)[-1], payload))
        if self.barrier is not None and url.endswith("/song"):
            self.barrier.wait()
        if ip in self.down:
            raise urllib.error.URLError("down")
        if url.endswith("/songs/library"):
            return {"ok": True, "files": [{"name": "Testlied.foil", "xml": SONG}], "truncated": False}
        return {"ok": True, "action": payload["action"]}

    def song_calls(self):
        return [(ip, payload) for ip, path, payload in self.calls if path == "song"]


class MigrationTest(unittest.TestCase):
    def test_old_settings_file_is_one_screen(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / "song_display.json").write_text(json.dumps({
                "library_path": "C:\\Lieder", "target_ip": STREAMING, "monitor": 3, "fullscreen": True}))
            service = SongService(folder)
            self.assertEqual([t.model_dump() for t in service.settings.targets], [{"ip": STREAMING, "monitor": 3}])
            self.assertEqual((service.settings.target_ip, service.settings.monitor), (STREAMING, 3))
            service.save_settings(service.settings)
            saved = json.loads((folder / "song_display.json").read_text())
            self.assertEqual(saved["targets"], [{"ip": STREAMING, "monitor": 3}])
            self.assertEqual(saved["monitor"], 3)   # still readable by an older build

    def test_defaults(self):
        settings = SongSettings()
        self.assertEqual([t.model_dump() for t in settings.targets], [{"ip": ANZEIGE, "monitor": 2}])

    def test_screen_rules(self):
        for targets in ([], [{"ip": ANZEIGE, "monitor": 1}, {"ip": ANZEIGE, "monitor": 2}],
                        [{"ip": "8.8.8.8", "monitor": 1}], [{"ip": ANZEIGE, "monitor": 9}],
                        [{"ip": f"192.168.188.{n}", "monitor": 1} for n in range(10, 15)]):
            with self.assertRaises(ValueError, msg=targets):
                SongSettings(targets=targets)
        settings = SongSettings(targets=[{"ip": STREAMING, "monitor": 1}, {"ip": ANZEIGE, "monitor": 2}])
        self.assertEqual(settings.monitor, 1)   # the first screen's


class ScreensTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.agents = FakeAgents()
        cls._post = display_agent._post
        display_agent._post = cls.agents
        cls.client = TestClient(app).__enter__()
        r = cls.client.put("/songs/settings", json={"library_path": "C:\\Lieder", "title_slide": False})
        assert r.status_code == 200, r.text
        assert cls.client.post("/songs/library/reload").status_code == 200

    @classmethod
    def tearDownClass(cls):
        cls.client.__exit__(None, None, None)
        display_agent._post = cls._post

    def setUp(self):
        self.agents.calls.clear()
        self.agents.down.clear()
        self.agents.barrier = None
        r = self.client.put("/songs/settings", json={
            "targets": [{"ip": ANZEIGE, "monitor": 2}, {"ip": STREAMING, "monitor": 1}]})
        self.assertEqual(r.status_code, 200, r.text)

    def test_older_page_saving_monitor_sets_one_screen(self):
        r = self.client.put("/songs/settings", json={"target_ip": ANZEIGE, "monitor": 4})
        self.assertEqual(r.json()["settings"]["targets"], [{"ip": ANZEIGE, "monitor": 4}])

    def test_one_screen_per_pc(self):
        r = self.client.put("/songs/settings", json={"targets": [{"ip": ANZEIGE, "monitor": 1}, {"ip": ANZEIGE, "monitor": 2}]})
        self.assertEqual(r.status_code, 400)
        self.assertIn("one screen per PC", r.json()["reason"])

    def test_every_step_goes_to_all_screens_in_parallel(self):
        self.agents.barrier = threading.Barrier(2, timeout=5)   # sequential calls would time out
        r = self.client.post("/songs/show", json={"song_id": "Testlied.foil", "slide": 0})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertNotIn("failed", r.json())
        sent = self.agents.song_calls()
        self.assertEqual(sorted((ip, p["monitor"]) for ip, p in sent), [(ANZEIGE, 2), (STREAMING, 1)])
        self.assertEqual(sent[0][1]["text"], sent[1][1]["text"])
        self.assertEqual({k: v for k, v in sent[0][1].items() if k != "monitor"},
                         {k: v for k, v in sent[1][1].items() if k != "monitor"})
        self.assertEqual([(s["ip"], s["ok"]) for s in r.json()["screens"]], [(ANZEIGE, True), (STREAMING, True)])
        for path, action in (("/songs/next", "show"), ("/songs/blank", "blank"), ("/songs/hide", "hide")):
            self.agents.calls.clear()
            r = self.client.post(path)
            self.assertEqual(r.status_code, 200, path)
            sent = self.agents.song_calls()
            self.assertEqual(sorted(ip for ip, _ in sent), [ANZEIGE, STREAMING], path)
            self.assertTrue(all(p["action"] == action for _, p in sent), path)
        self.assertEqual([p for _, p in sent], [{"action": "hide"}, {"action": "hide"}])

    def test_one_screen_down_the_other_still_updates(self):
        self.client.post("/songs/show", json={"song_id": "Testlied.foil", "slide": 0})
        self.agents.down.add(STREAMING)
        self.agents.calls.clear()
        r = self.client.post("/songs/next")
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["slide"], 1)
        self.assertEqual(body["failed"], [{"ip": STREAMING, "monitor": 1, "ok": False, "reason": "display agent unavailable"}])
        self.assertIn((ANZEIGE, "song"), [(ip, path) for ip, path, _ in self.agents.calls])
        live = self.client.get("/songs/live").json()
        self.assertEqual(live["slide"], 1)
        self.assertEqual([(s["ip"], s["ok"]) for s in live["screens"]], [(ANZEIGE, True), (STREAMING, False)])
        # Back up: the next step reaches it again and the report clears.
        self.agents.down.clear()
        r = self.client.post("/songs/prev")
        self.assertNotIn("failed", r.json())
        self.assertTrue(all(s["ok"] for s in self.client.get("/songs/live").json()["screens"]))

    def test_all_screens_down_leaves_the_live_state(self):
        self.client.post("/songs/show", json={"song_id": "Testlied.foil", "slide": 0})
        self.agents.down.update({ANZEIGE, STREAMING})
        r = self.client.post("/songs/next")
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.json()["reason"], "display agent unavailable")
        self.assertEqual([s["ok"] for s in r.json()["screens"]], [False, False])
        self.assertEqual(self.client.get("/songs/live").json()["slide"], 0)

    def test_library_stays_on_its_pc(self):
        self.client.put("/songs/settings", json={"target_ip": ANZEIGE,
                                                 "targets": [{"ip": STREAMING, "monitor": 1}]})
        self.agents.calls.clear()
        self.client.post("/songs/library/reload")
        self.assertEqual([(ip, path) for ip, path, _ in self.agents.calls], [(ANZEIGE, "library")])


if __name__ == "__main__":
    unittest.main(verbosity=2)
