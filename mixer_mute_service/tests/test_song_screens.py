"""Song lyrics on several screens (app/songs.py) with fake display agents.

Verifies: settings from before several screens (one PC and monitor) load as
a one-screen list; an older page that saves "monitor" changes only the first
screen (never drops the others); a bad or doubled screen in the file is
dropped alone; readdressing a PC keeps the screens valid; one screen per PC
and 1..4 screens; every show, blank and hide goes to all screens in
parallel, each with its own monitor; one screen failing does not stop the
others (the answer and /songs/live name it with its reason, the live state
moves on), all failing leaves the live state alone; a screen that HANGS
costs a step at most the 1 s timeout once, then steps do not wait for it at
all, three quick steps land in order on the live screen, and the hanging
screen gets only the newest slide; the library stays on its own PC. No
request leaves the process (display_agent._post is replaced).

Run from mixer_mute_service:  python -m tests.test_song_screens
"""

import json
import os
import tempfile
import threading
import time
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
        self.hang = set()       # IPs that never answer: each call waits for its timeout
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
        if ip in self.hang:
            time.sleep(timeout)
            raise TimeoutError("no answer")
        if url.endswith("/songs/library"):
            return {"ok": True, "files": [{"name": "Testlied.foil", "xml": SONG}], "truncated": False}
        return {"ok": True, "action": payload["action"]}

    def song_calls(self):
        return [(ip, payload) for ip, path, payload in self.calls if path == "song"]


class MigrationTest(unittest.TestCase):
    def test_bad_screen_in_file_is_dropped_alone(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            (folder / "song_display.json").write_text(json.dumps({
                "library_path": "C:\\Lieder", "font": "Arial", "target_ip": ANZEIGE, "monitor": 2,
                "targets": [{"ip": ANZEIGE, "monitor": 2}, {"ip": ANZEIGE, "monitor": 3},
                            {"ip": "8.8.8.8", "monitor": 1}, {"ip": STREAMING, "monitor": 1}]}))
            settings = SongService(folder).settings
            self.assertEqual([(t.ip, t.monitor) for t in settings.targets], [(ANZEIGE, 2), (STREAMING, 1)])
            self.assertEqual((settings.font, settings.library_path), ("Arial", "C:\\Lieder"))   # the rest kept

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

    def test_older_page_changes_only_the_first_screen(self):
        r = self.client.put("/songs/settings", json={"target_ip": ANZEIGE, "monitor": 4})
        self.assertEqual(r.json()["settings"]["targets"], [{"ip": ANZEIGE, "monitor": 4}, {"ip": STREAMING, "monitor": 1}])
        r = self.client.put("/songs/settings", json={"target_ip": STREAMING, "monitor": 3})
        self.assertEqual(r.status_code, 400)   # would put two screens on one PC: refused, nothing lost
        self.assertEqual(len(self.client.get("/songs/settings").json()["targets"]), 2)

    def test_readdressing_a_pc_keeps_valid_screens(self):
        r = self.client.put("/target-pcs", json={"pcs": [
            {"id": "streaming", "name": "Streaming PC", "ip": ANZEIGE},
            {"id": "display", "name": "Anzeige PC", "ip": ANZEIGE}]})
        if r.status_code == 200:   # the store may refuse two PCs on one address itself
            targets = self.client.get("/songs/settings").json()["targets"]
            self.assertEqual(len({t["ip"] for t in targets}), len(targets))
        r = self.client.put("/target-pcs", json={"pcs": [
            {"id": "streaming", "name": "Streaming PC", "ip": "192.168.188.21"},
            {"id": "display", "name": "Anzeige PC", "ip": ANZEIGE}]})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(self.client.get("/songs/settings").json()["targets"],
                         [{"ip": ANZEIGE, "monitor": 2}, {"ip": "192.168.188.21", "monitor": 1}])
        self.client.put("/target-pcs", json={"pcs": [
            {"id": "streaming", "name": "Streaming PC", "ip": STREAMING},
            {"id": "display", "name": "Anzeige PC", "ip": ANZEIGE}]})

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
        # Back up: the next step reaches it again (without waiting for it) and the report clears.
        self.agents.down.clear()
        self.client.post("/songs/prev")
        deadline = time.monotonic() + 2
        while not all(s["ok"] for s in self.client.get("/songs/live").json()["screens"]) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertTrue(all(s["ok"] for s in self.client.get("/songs/live").json()["screens"]))

    def test_hanging_screen_never_holds_up_the_steps(self):
        self.client.post("/songs/show", json={"song_id": "Testlied.foil", "slide": 0})
        self.agents.hang.add(STREAMING)
        self.agents.calls.clear()
        started = time.monotonic()
        r = self.client.post("/songs/show", json={"song_id": "Testlied.foil", "slide": 1})
        self.assertLess(time.monotonic() - started, 1.25)   # once at most the timeout
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual([f["ip"] for f in r.json()["failed"]], [STREAMING])
        timings, slides = [], [0, 1, 0]
        for slide in slides:   # three quick taps
            started = time.monotonic()
            r = self.client.post("/songs/show", json={"song_id": "Testlied.foil", "slide": slide})
            timings.append(time.monotonic() - started)
            self.assertEqual((r.status_code, r.json()["slide"]), (200, slide))
            self.assertEqual([f["ip"] for f in r.json()["failed"]], [STREAMING])
        self.assertLess(max(timings), 0.3, timings)   # not waiting for the dead screen
        shown = [p["text"] for ip, p in self.agents.song_calls() if ip == ANZEIGE]
        self.assertEqual(shown, ["Zwei", "Eins", "Zwei", "Eins"])   # every step, in order
        time.sleep(1.2)   # the hanging screen's own queue: only ever the newest slide
        hung = [p["text"] for ip, p in self.agents.song_calls() if ip == STREAMING]
        self.assertLess(len(hung), 4)
        self.assertEqual(hung[-1], "Eins")
        self.agents.hang.clear()

    def test_quick_taps_at_once_all_count(self):
        self.client.post("/songs/show", json={"song_id": "Testlied.foil", "slide": 0})
        self.client.put("/songs/settings", json={"title_slide": True})
        self.client.post("/songs/show", json={"song_id": "Testlied.foil", "slide": 0})
        self.agents.calls.clear()
        threads = [threading.Thread(target=self.client.post, args=("/songs/next",)) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.client.get("/songs/live").json()["slide"], 2)   # two taps, two steps
        shown = [p["text"] for ip, p in self.agents.song_calls() if ip == ANZEIGE]
        self.assertEqual(shown[-1], "Zwei")
        self.client.put("/songs/settings", json={"title_slide": False})

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
