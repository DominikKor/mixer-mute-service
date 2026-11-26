"""The page draws what the service laid out, like the display agent.

The service lays songs out (app/slide_layout.py) and sends each display
agent the lines and size of a slide; the page gets the first screen's layout
with the song ("shown", "layout"). Checked here in quickjs with the page's
own functions from app/static/js/songs.js: the drawing constants match the
service and the agent, the text box and the cut at its bottom are the same,
and for every slide of a song with a split verse the page shows the same
slide (text, title, copyright rows, lines, size) as the service sends to the
display agent. Skips without quickjs or tkinter; with REQUIRE_TK=1 (CI) a
missing quickjs is an error, like a missing tkinter.
"""

import asyncio
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers import require_tk

from app import slide_layout as L
from app import songs
from app.songs import SongService

try:
    import quickjs
except ImportError:  # pragma: no cover - only where quickjs is not installed
    if os.environ.get("REQUIRE_TK") == "1":  # CI: never skip silently
        raise
    quickjs = None

# None without tkinter (tests skip); with REQUIRE_TK=1 (CI) that is an error.
tkinter = require_tk()

if tkinter is not None:
    from desktop_agent import show_image_agent as agent

SONGS_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "songs.js"
JS_NAMES = ("slideBox", "visibleSlideLines", "songDisplaySlides", "songSlide")

LONG_VERSE = "\n".join([f"Erste Strophe, Zeile {i}." for i in range(1, 10)] + [""]
                       + [f"Zweite Strophe, Zeile {i}" for i in range(1, 10)])
SONG = {"title": "Allein deine Gnade", "copyright": "Text: Test", "books": [],
        "slides": [{"key": "Ref", "text": "Refrain"}, {"key": "1", "text": LONG_VERSE},
                   {"key": "2", "text": "Zweite Strophe\nkurz"}]}


async def fake_agent(cfg, ip, path, payload, timeout=3.0):
    if path == "/song/metrics":
        return {"ok": True, "width": 1920, "height": 1080, "ref": 100, "widths": [60] * len(payload["chars"])}
    return {"ok": True}


@unittest.skipIf(quickjs is None or tkinter is None, "needs quickjs and tkinter")
class SlideParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = SONGS_JS.read_text(encoding="utf-8")
        parts = [re.search(r"const SLIDE = \{.*?\};", source, re.S).group(0),
                 re.search(r"const PREVIEW = \{.*?\};", source, re.S).group(0)]
        for name in JS_NAMES:
            parts.append(re.search(rf"function {name}\(.*?\n\}}\n", source, re.S).group(0))
        cls.js = quickjs.Context()
        cls.js.eval("\n".join(parts))

    def call(self, expression):
        return json.loads(self.js.eval(f"JSON.stringify({expression})"))

    def test_constants_match_the_service_and_the_agent(self):
        slide, preview = self.call("[SLIDE, PREVIEW]")
        expected = {"padX": L.SLIDE_PAD_X, "padY": L.SLIDE_PAD_Y, "titleH": L.SLIDE_TITLE_H,
                    "copyH": L.SLIDE_COPY_H, "line": L.SLIDE_LINE}
        self.assertEqual(slide, expected)
        self.assertEqual(expected, {"padX": agent.SLIDE_PAD_X, "padY": agent.SLIDE_PAD_Y, "titleH": agent.SLIDE_TITLE_H,
                                    "copyH": agent.SLIDE_COPY_H, "line": agent.SLIDE_LINE})
        self.assertEqual(preview, {"mix": agent.PREVIEW_MIX})
        self.assertEqual((L.PREVIEW_SCALE, L.PREVIEW_MIN, L.PREVIEW_GAP, L.SLIDE_MIN_PX),
                         (agent.PREVIEW_SCALE, agent.PREVIEW_MIN, agent.PREVIEW_GAP, agent.SLIDE_MIN_PX))

    def test_box_and_cut_match(self):
        for width, height in ((1920, 1080), (1024, 768), (597, 336)):
            for title in (False, True):
                for copyright in (False, True):
                    box = list(L.slide_box(width, height, title, copyright))
                    self.assertEqual(self.call(f"slideBox({width}, {height}, {json.dumps(title)}, {json.dumps(copyright)})"), box)
                    self.assertEqual(box, list(agent._slide_box(width, height, title, copyright)))
        for count, top, px, bottom in ((20, 100, 40, 500), (3, 100, 40, 500), (3, 100, 40, 130), (9, 75.6, 72, 1004.4)):
            self.assertEqual(self.call(f"visibleSlideLines({count}, {top}, {px}, {bottom})"),
                             agent._visible_lines(count, top, px, bottom))

    def test_the_page_shows_what_the_service_sends(self):
        cfg = SimpleNamespace(desktop_display=SimpleNamespace(enabled=True, token_env="DESKTOP_AGENT_TOKEN"))
        for title_slide in (True, False):
            for show_title in (True, False):
                with self.subTest(title_slide=title_slide, show_title=show_title), tempfile.TemporaryDirectory() as d:
                    service = SongService(Path(d))
                    service.songs = {"a.foil": SONG}
                    service.settings = service.settings.model_copy(update={"title_slide": title_slide,
                                                                           "show_title": show_title})
                    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cfg=cfg, songs=service)))
                    with patch("app.display_agent.call", fake_agent):
                        asyncio.run(songs.ensure_layout(request, "a.foil"))
                    view = songs._song_view("a.foil", SONG, service.layouts["a.foil"], service.slides("a.foil"))
                    view = json.loads(json.dumps(view))
                    settings = json.dumps(service.settings.model_dump())
                    shown = service.slides("a.foil")
                    self.assertTrue(any(s.get("parts", 1) > 1 for s in shown))    # the long verse is split
                    self.assertEqual(self.call(f"songDisplaySlides({json.dumps(view)}, {settings}).length"), len(shown))
                    for index in range(len(shown)):
                        sent = service.slide_payload("a.foil", index, 0)
                        page = self.call(f"songSlide({json.dumps(view)}, {index}, {settings})")
                        self.assertEqual((page["text"], page["title"], page["copyright"]),
                                         (sent["text"], sent["title"], sent["copyright"]), index)
                        self.assertEqual(page["screen"], sent["layout"]["screen"])
                        self.assertEqual((page["draw"]["px"], page["draw"]["lines"], page["draw"]["preview"]),
                                         (sent["layout"]["px"], sent["layout"]["lines"], sent["layout"]["preview"]))


if __name__ == "__main__":
    unittest.main()
