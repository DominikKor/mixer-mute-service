"""Song parts of the desktop agent: reading the library and checking /song requests.

Drawing needs a screen and Windows, so only the pure parts are tested here.
"""

import tempfile
import unittest
from pathlib import Path

from tests.helpers import require_tk

# None without tkinter (tests skip); with REQUIRE_TK=1 (CI) that is an error.
tkinter = require_tk()

if tkinter is not None:
    from desktop_agent import show_image_agent as agent

SHOW = {
    "action": "show", "monitor": 2, "fullscreen": True,
    "style": {"font": "Verdana", "font_size": 72, "text_color": "#ffffff", "background_color": "#000000"},
    "text": "Zeile 1\nZeile 2", "title": "Titel", "copyright": "",
}


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class SongLibraryTests(unittest.TestCase):
    def test_reads_songs_folder_of_a_library(self):
        with tempfile.TemporaryDirectory() as directory:
            songs = Path(directory) / "Songs"
            songs.mkdir()
            (songs / "B Lied.foil").write_text("<b/>", encoding="utf-8")
            (songs / "a lied.FOIL").write_text("<a>ä</a>", encoding="utf-8")
            (songs / "notes.txt").write_text("x", encoding="utf-8")
            (songs / "Unterordner.foil").mkdir()
            files, truncated = agent._read_song_library(directory)
            self.assertEqual([f["name"] for f in files], ["a lied.FOIL", "B Lied.foil"])
            self.assertEqual(files[0]["xml"], "<a>ä</a>")
            self.assertFalse(truncated)
            # The Songs folder itself works as well.
            self.assertEqual(len(agent._read_song_library(str(songs))[0]), 2)

    def test_missing_or_relative_folder(self):
        with self.assertRaises(FileNotFoundError):
            agent._read_song_library("/does/not/exist")
        with self.assertRaises(FileNotFoundError):
            agent._read_song_library("relative/path")

    def test_saves_a_new_song_into_the_library(self):
        with tempfile.TemporaryDirectory() as directory:
            songs = Path(directory) / "Songs"
            songs.mkdir()
            target = agent._save_song(directory, "Gott_ du bist da.foil", "<x>ä</x>")
            self.assertEqual(target, songs / "Gott_ du bist da.foil")
            self.assertEqual(target.read_text(encoding="utf-8"), "<x>ä</x>")
            with self.assertRaises(FileExistsError):
                agent._save_song(directory, "Gott_ du bist da.foil", "<neu/>")
            self.assertEqual(target.read_text(encoding="utf-8"), "<x>ä</x>")  # never replaced
            for name in ("../boese.foil", "Unter\\ordner.foil", "a/b.foil", "kein-foil.txt", ".foil", " Lied.foil"):
                with self.subTest(name=name), self.assertRaises(ValueError):
                    agent._save_song(directory, name, "<x/>")
            with self.assertRaises(ValueError):
                agent._save_song(directory, "Gross.foil", "x" * (agent.MAX_SONG_FILE_BYTES + 1))
            with self.assertRaises(FileNotFoundError):
                agent._save_song(str(Path(directory) / "fehlt"), "Lied.foil", "<x/>")

    def test_song_requests(self):
        self.assertTrue(agent._valid_song_request(SHOW))
        self.assertTrue(agent._valid_song_request({**SHOW, "action": "blank", "text": ""}))
        self.assertTrue(agent._valid_song_request({"action": "hide"}))
        self.assertFalse(agent._valid_song_request({"action": "hide", "monitor": 1}))
        self.assertFalse(agent._valid_song_request({**SHOW, "action": "launch"}))
        self.assertFalse(agent._valid_song_request({**SHOW, "monitor": True}))
        self.assertFalse(agent._valid_song_request({**SHOW, "monitor": 0}))
        self.assertFalse(agent._valid_song_request({**SHOW, "extra": 1}))
        self.assertFalse(agent._valid_song_request({**SHOW, "text": "x" * 8001}))
        self.assertFalse(agent._valid_song_request({**SHOW, "style": {**SHOW["style"], "text_color": "white"}}))
        self.assertFalse(agent._valid_song_request({**SHOW, "style": {**SHOW["style"], "font_size": 500}}))

    def test_preview_and_alignment_requests(self):
        new = {**SHOW, "next": "Nächste\nStrophe", "style": {**SHOW["style"], "align": "left"}}
        self.assertTrue(agent._valid_song_request(new))
        self.assertTrue(agent._valid_song_request({**new, "next": ""}))
        self.assertTrue(agent._valid_song_request({**SHOW, "next": "x"}))  # align is optional
        self.assertFalse(agent._valid_song_request({**new, "style": {**new["style"], "align": "justify"}}))
        self.assertFalse(agent._valid_song_request({**new, "next": 5}))
        self.assertFalse(agent._valid_song_request({**new, "next": "x" * 8001}))

    def test_layout_from_the_service(self):
        layout = {"screen": [1920, 1080], "px": 66.2, "lines": ["Zeile 1", "Zeile 2"],
                  "preview": {"px": 33.1, "lines": ["Nächste"]}}
        self.assertTrue(agent._valid_song_request({**SHOW, "layout": layout}))
        self.assertTrue(agent._valid_song_request({**SHOW, "layout": {**layout, "preview": None}}))
        for bad in ({**layout, "px": 0}, {**layout, "px": True}, {**layout, "lines": "Zeile"},
                    {**layout, "lines": ["x" * 2001]}, {**layout, "screen": [1920]}, {**layout, "screen": [1920.0, 1080]},
                    {**layout, "preview": {"px": 30}}, {**layout, "extra": 1}, {k: v for k, v in layout.items() if k != "px"}):
            with self.subTest(bad=str(bad)[:60]):
                self.assertFalse(agent._valid_song_request({**SHOW, "layout": bad}))
        self.assertFalse(agent._valid_song_request({**SHOW, "fit": []}))   # never shipped

    def test_the_service_layout_is_drawn_as_it_is(self):
        measure = lambda text, size: len(text) * size * 0.6  # noqa: E731
        box_w, box_h = agent._slide_box(1920, 1080, True, False)
        body = {**SHOW, "layout": {"screen": [1920, 1080], "px": 60.0, "lines": ["Eins", "Zwei"],
                                   "preview": {"px": 30.0, "lines": ["Drei"]}}}
        self.assertEqual(agent._slide_layout(measure, body, 1920, 1080, box_w, box_h),
                         (60.0, ["Eins", "Zwei"], (30.0, ["Drei"])))
        # Made for another window size: the slide is fitted here as before.
        px, lines, _preview = agent._slide_layout(measure, body, 1280, 720, *agent._slide_box(1280, 720, True, False))
        self.assertEqual(lines, SHOW["text"].split("\n"))
        # A line far wider than the margin (the font measured differently) is wrapped all the same.
        wide = {**body, "layout": {**body["layout"], "lines": ["Wort " * 80]}}
        _px, lines, _ = agent._slide_layout(measure, wide, 1920, 1080, box_w, box_h)
        self.assertGreater(len(lines), 1)
        # Without a layout (an older service): fitted on the agent.
        px, lines, _ = agent._slide_layout(measure, SHOW, 1920, 1080, box_w, box_h)
        self.assertEqual((px, lines), (72.0, ["Zeile 1", "Zeile 2"]))

    def test_metrics_requests(self):
        good = {"font": "Verdana", "monitor": 2, "fullscreen": True, "chars": "abcäö "}
        self.assertTrue(agent._valid_metrics_request(good))
        for bad in ({**good, "font": ""}, {**good, "font": "a\nb"}, {**good, "monitor": 0}, {**good, "monitor": True},
                    {**good, "fullscreen": 1}, {**good, "chars": ""}, {**good, "chars": "x" * 4001}, {**good, "extra": 1}):
            with self.subTest(bad=bad):
                self.assertFalse(agent._valid_metrics_request(bad))

    def test_preview_grey_and_room(self):
        self.assertEqual(agent._mix_color("#ffffff", "#000000", 0.45), "#737373")
        self.assertEqual(agent._mix_color("#000000", "#ffffff", 0.45), "#8c8c8c")
        measure = lambda text, size: len(text) * size * 0.5  # noqa: E731
        two_lines = ["Zeile 1", "Zeile 2"]
        # 1080 px screen, text 72 px: plenty of room below two lines.
        fits = agent._preview_layout(measure, two_lines, ["Nächste Zeile", "zweite"], 72, 1800, 900, 1080)
        self.assertEqual(fits, (36, ["Nächste Zeile", "zweite"]))
        # A full slide leaves no room: no preview, the text keeps its size.
        full = ["Zeile"] * 9
        self.assertIsNone(agent._preview_layout(measure, full, ["Nächste"], 72, 1800, 900, 1080))
        # Only as many preview lines as fit: 6 lines of 72 px leave
        # (900 - 540) / 2 - 36 = 144 px, three preview lines of 45 px.
        some = agent._preview_layout(measure, ["Z"] * 6, ["a", "b", "c", "d", "e"], 72, 1800, 900, 1080)
        self.assertEqual(some, (36, ["a", "b", "c"]))
        # A line wider than the slide is wrapped (no longer dropped), each
        # piece fitting; nothing to preview: none.
        size, wide = agent._preview_layout(measure, two_lines, ["x" * 400], 72, 1800, 900, 1080)
        self.assertEqual(size, 36)
        self.assertTrue(len(wide) >= 2 and all(measure(line, size) <= 1800 for line in wide))
        self.assertIsNone(agent._preview_layout(measure, two_lines, [], 72, 1800, 900, 1080))


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class SongRequestHandlerTests(unittest.TestCase):
    def test_song_request_gets_exactly_one_response(self):
        # Regression: after the /power endpoint was added, /song answered 202
        # and then fell through to the profile check and sent a 400 as well.
        import json
        import os
        import queue
        import socket
        import threading
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch

        token = "test-token"
        body = json.dumps({"action": "hide"}).encode()
        with patch.dict(os.environ, {"DESKTOP_AGENT_TOKEN": token}), \
                patch.object(agent, "requests", queue.Queue()) as requests:
            server = ThreadingHTTPServer(("127.0.0.1", 0), agent.DisplayHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                with socket.create_connection(server.server_address, timeout=5) as sock:
                    sock.sendall(
                        b"POST /song HTTP/1.1\r\nHost: x\r\nX-Display-Token: " + token.encode()
                        + b"\r\nContent-Type: application/json\r\nContent-Length: "
                        + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body
                    )
                    raw = b""
                    while chunk := sock.recv(4096):
                        raw += chunk
            finally:
                server.shutdown()
                server.server_close()
        self.assertTrue(raw.startswith(b"HTTP/1.0 202"), raw[:40])
        self.assertEqual(raw.count(b"HTTP/1."), 1, "a second response followed the 202")
        self.assertEqual(requests.get_nowait()["action"], "hide")

    def test_metrics_are_measured_on_the_tk_thread(self):
        # The handler hands the measuring to the Tk loop (here a thread
        # standing in for it) and answers with the window size and widths.
        import json
        import os
        import queue
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        from unittest.mock import patch

        token = "test-token"
        jobs = queue.Queue()

        def tk_loop():
            job = jobs.get(timeout=5)
            job["reply"].put([len(c) * 10 for c in job["chars"]])

        body = json.dumps({"font": "Verdana", "monitor": 2, "fullscreen": True, "chars": "aäb"}).encode()
        with patch.dict(os.environ, {"DESKTOP_AGENT_TOKEN": token}), patch.object(agent, "requests", jobs), \
                patch.object(agent, "_song_window", return_value=(1920, 1080, 0, 0)):
            server = ThreadingHTTPServer(("127.0.0.1", 0), agent.DisplayHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            threading.Thread(target=tk_loop, daemon=True).start()
            try:
                request = urllib.request.Request(
                    f"http://127.0.0.1:{server.server_address[1]}/song/metrics", data=body, method="POST",
                    headers={"X-Display-Token": token, "Content-Type": "application/json"})
                with urllib.request.urlopen(request, timeout=5) as answer:
                    result = json.loads(answer.read())
            finally:
                server.shutdown()
                server.server_close()
        self.assertEqual(result, {"ok": True, "width": 1920, "height": 1080, "ref": 100, "widths": [10, 10, 10]})

if __name__ == "__main__":
    unittest.main()
