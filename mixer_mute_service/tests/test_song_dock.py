"""Song dock (app/static/js/song-dock.js): verse keys at the running program row.

Verifies: the page loads the script after songs.js and program.js (it uses
their state and songAction) and its stylesheet; both redraws call it
(renderProgramItems in program.js, renderSongLive in songs.js), so the dock
and the Liedanzeige section show the same; every song path it sends to is a
route of app/songs.py (no new server behaviour); the chip texts and the
first line of a slide (in quickjs; skipped where quickjs is missing, an error
in CI with REQUIRE_TK=1).

Run from mixer_mute_service:  python -m tests.test_song_dock
"""

import os
import re
import unittest
from pathlib import Path

try:
    import quickjs
except ImportError:  # pragma: no cover - only where quickjs is not installed
    if os.environ.get("REQUIRE_TK") == "1":  # CI: never skip silently
        raise
    quickjs = None

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "app" / "static"
DOCK_JS = (STATIC / "js" / "song-dock.js").read_text(encoding="utf-8")


def function_body(source: str, name: str) -> str:
    return re.search(rf"function {name}\(.*?\n\}}\n", source, re.S).group(0)


class SongDockWiringTest(unittest.TestCase):
    def test_loaded_after_songs_and_program(self):
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        scripts = [src.split("?")[0] for src in re.findall(r'<script\s+src="([^"]+)"', html)]
        self.assertIn("js/song-dock.js", scripts)
        dock = scripts.index("js/song-dock.js")
        self.assertLess(scripts.index("js/songs.js"), dock)
        self.assertLess(scripts.index("js/program.js"), dock)
        self.assertLess(dock, scripts.index("js/main.js"))
        self.assertRegex(html, r'<link rel="stylesheet" href="css/song-dock\.css\?v=__BUILD_ID__"')
        self.assertTrue((STATIC / "css" / "song-dock.css").is_file())

    def test_both_redraws_update_the_dock(self):
        program = (STATIC / "js" / "program.js").read_text(encoding="utf-8")
        songs = (STATIC / "js" / "songs.js").read_text(encoding="utf-8")
        self.assertIn("renderSongDock();", function_body(program, "renderProgramItems"))
        self.assertIn("renderSongDock();", function_body(songs, "renderSongLive"))

    def test_uses_existing_song_routes_only(self):
        from app.songs import router
        routes = {(method, route.path) for route in router.routes for method in route.methods}
        paths = set(re.findall(r'"(/songs/[a-z/]+)"', DOCK_JS))
        self.assertTrue(paths, "the dock sends nothing itself?")
        for path in paths:
            self.assertIn(("POST", path), routes, path)
        # Stepping goes through songs.js (the same calls as the Liedanzeige keys).
        for call in ("songPrev()", "songNext()", "songBlankToggle()", "songAction("):
            self.assertIn(call, DOCK_JS)


@unittest.skipIf(quickjs is None, "quickjs not installed")
class SongDockTextTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = quickjs.Context()
        cls.js.eval(function_body(DOCK_JS, "songDockChipText") + function_body(DOCK_JS, "songDockFirstLine"))

    def chip(self, slide: dict, index: int) -> str:
        import json
        return self.js.eval(f"songDockChipText({json.dumps(slide)}, {index})")

    def test_chip_texts(self):
        self.assertEqual(self.chip({"key": "Titel", "text": "Lied", "title_slide": True}, 0), "T")
        self.assertEqual(self.chip({"key": "1", "text": "a"}, 1), "1")
        for key in ("Ref", "refrain", "R", "Chorus"):
            self.assertEqual(self.chip({"key": key, "text": "a"}, 2), "R", key)
        self.assertEqual(self.chip({"key": "Bridge", "text": "a"}, 3), "Bri")
        self.assertEqual(self.chip({"key": "  ", "text": "a"}, 4), "5")   # no key: the slide number
        self.assertEqual(self.chip({"text": "a"}, 0), "1")

    def test_first_line(self):
        self.assertEqual(self.js.eval('songDockFirstLine({text: "\\n  Großer Gott,  \\nwir loben dich"})'), "Großer Gott,")
        self.assertEqual(self.js.eval("songDockFirstLine({text: ''})"), "")
        self.assertEqual(self.js.eval("songDockFirstLine(undefined)"), "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
