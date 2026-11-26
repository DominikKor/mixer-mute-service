"""The page's preview lays out lyrics exactly like the display agent.

Runs wrapSlideLines/fitSlideText/previewLayout/visibleSlideLines from
app/static/js/songs.js in quickjs and compares them with the agent's
_wrap_lines/_fit_text/_preview_layout/_visible_lines, with the same width
estimate on both sides: 0.6 em per code point, 1.2 em for one outside the
BMP (an emoji), so cutting a word by UTF-16 units instead of code points
gives different lines. The JS constants (SLIDE, PREVIEW, OWN_LINES_KEEP,
repeat marks) are read from the script and compared, and the JS side uses
its own minimum size. Skips without quickjs or tkinter; with REQUIRE_TK=1
(CI) a missing quickjs is an error, like a missing tkinter.
"""

import json
import os
import re
import unittest
from pathlib import Path

from tests.helpers import require_tk

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
JS_NAMES = ("slideWordUnits", "wrapSlideLines", "slideSizes", "fitSlideText", "visibleSlideLines", "previewLayout")

TEXTS = [
    # Real stanzas: one line in the file, own lines, repeat marks, empty line.
    "Meine Seele singe, denn die Nacht ist vorbei. Mach dich auf und bringe deinem Gott Lob und Preis. "
    "Alle Schöpfung juble, wenn der Tag nun anbricht. Gottes Töchter und Söhne strahlen in seinem Licht.",
    "|: Alles ist eitel, du aber bleibst\nund wen du ins Buch des\nLebens schreibst.\n\nDu aber bleibst. :|",
    "|: Am Tage sendet der Herr seine Güte, un des Nachts singe ich ihm, und des Nachts singe ich ihm\n"
    "und bete zu dem Gott meines Lebens. :|",
    " |: An hellen Tagen, Herz, welch ein Schlagen!\nFa la la  la la la  la la. :|",
    "Großer Gott, wir loben dich,\nHerr, wir preisen deine Stärke.\nVor dir neigt die Erde sich\nund bewundert deine Werke.",
    "Kurz\nDu bist meine Stärke, nichts sonst hab ich hier, Herr!!",
    "Herr, du bist mein Leben, Herr du bist mei",
    # Words wider than a line: with emoji (UTF-16 and code points differ)
    # and with decomposed umlauts (a + combining diaeresis).
    "Halleluja" * 25 + " 🙌" + "ä" * 40,
    "a" + "🙌" * 61 + " Jubel",
    "Gro\u0308\u00dfer Go\u0308tt " * 3 + "Scho\u0308pfung" * 12,
    "|: Wir singen dir  :|\nHalleluja, Halleluja, wir singen dir, Halleluja, singen dir |:",
    # Far too much for one slide.
    "\n".join(["Christ be with me, Christ within me, Christ behind me, Christ before me"] * 14),
    "",
]
# (width, height, title, copyright) of the screen.
SCREENS = [(1920, 1080, False, False), (1920, 1080, True, True), (1024, 768, True, True), (597, 336, False, False)]


def measure(text, size):
    """0.6 em per code point, 1.2 em outside the BMP (same sum order as JS)."""
    return sum(1.2 if ord(c) > 0xFFFF else 0.6 for c in text) * size


@unittest.skipIf(quickjs is None or tkinter is None, "needs quickjs and tkinter")
class SlideParityTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = SONGS_JS.read_text(encoding="utf-8")
        parts = [re.search(r"const SLIDE = \{.*?\};", source, re.S).group(0),
                 re.search(r"const PREVIEW = \{.*?\};", source, re.S).group(0),
                 re.search(r"const OWN_LINES_KEEP = .*?;", source).group(0),
                 re.search(r"const REPEAT_OPEN = .*?;", source).group(0)]
        for name in JS_NAMES:
            parts.append(re.search(rf"function {name}\(.*?\n\}}\n", source, re.S).group(0))
        parts.append("const measure = (t, s) => Array.from(t).reduce((w, c) => w + (c.codePointAt(0) > 0xFFFF ? 1.2 : 0.6), 0) * s;")
        cls.js = quickjs.Context()
        cls.js.eval("\n".join(parts))

    def call(self, expression):
        return json.loads(self.js.eval(f"JSON.stringify({expression})"))

    def test_constants_match_the_agent(self):
        js = self.call("[SLIDE, PREVIEW, OWN_LINES_KEEP, REPEAT_OPEN, REPEAT_CLOSE]")
        slide, preview, keep, repeat_open, repeat_close = js
        self.assertEqual(slide, {"padX": agent.SLIDE_PAD_X, "padY": agent.SLIDE_PAD_Y, "titleH": agent.SLIDE_TITLE_H,
                                 "copyH": agent.SLIDE_COPY_H, "line": agent.SLIDE_LINE, "minPx": agent.SLIDE_MIN_PX})
        self.assertEqual(preview, {"scale": agent.PREVIEW_SCALE, "min": agent.PREVIEW_MIN, "gap": agent.PREVIEW_GAP,
                                   "mix": agent.PREVIEW_MIX})
        self.assertEqual((keep, repeat_open, repeat_close),
                         (agent.OWN_LINES_KEEP, agent.REPEAT_OPEN, agent.REPEAT_CLOSE))

    def test_fit_preview_and_cut_match_the_agent(self):
        checked = 0
        for width, height, title, copyright in SCREENS:
            box_w = width - 2 * agent.SLIDE_PAD_X * width
            box_h = (height - 2 * agent.SLIDE_PAD_Y * height - (agent.SLIDE_TITLE_H * height if title else 0)
                     - (agent.SLIDE_COPY_H * height if copyright else 0))
            max_px, min_px = 72 * height / 1080, agent.SLIDE_MIN_PX * height / 1080
            for i, text in enumerate(TEXTS):
                px, lines = agent._fit_text(measure, text, max_px, min_px, box_w, box_h)
                # The page's own minimum size (SLIDE.minPx), not the agent's.
                js = self.call(f"fitSlideText(measure, {json.dumps(text)}, {max_px}, SLIDE.minPx * {height} / 1080, "
                               f"{box_w}, {box_h})")
                self.assertEqual((js["px"], js["lines"]), (px, lines), (width, i))
                top = max(0.0, (box_h - len(lines) * px * agent.SLIDE_LINE) / 2)
                self.assertEqual(self.call(f"visibleSlideLines({len(lines)}, {top}, {px}, {box_h})"),
                                 agent._visible_lines(len(lines), top, px, box_h))
                following = TEXTS[(i + 1) % len(TEXTS)].split("\n")
                preview = agent._preview_layout(measure, lines, following, px, box_w, box_h, height)
                js_preview = self.call(f"previewLayout(measure, {json.dumps(lines)}, {json.dumps(following)}, "
                                       f"{px}, {box_w}, {box_h}, {height})")
                self.assertEqual(js_preview, None if preview is None else {"px": preview[0], "lines": preview[1]})
                checked += 1
        self.assertEqual(checked, len(SCREENS) * len(TEXTS))

    def test_word_units_and_letter_cuts_match(self):
        for paragraph in ("|: Alles ist eitel, du aber bleibst :|", "a  :| |: b", ":| allein", "Wort |:",
                          "bleibst.  :|", "wir singen dir |:", "|:  Halleluja  :|", "|:", ":|", "", "Fa la  la"):
            self.assertEqual(self.call(f"slideWordUnits({json.dumps(paragraph)})"), agent._word_units(paragraph))
        for long_word in ("🙌" * 30 + "ä" * 30, "a" + "🙌" * 40, "o\u0308" * 60):
            for width in (400, 410, 433):
                self.assertEqual(self.call(f"wrapSlideLines(measure, [{json.dumps(long_word)}], 40, {width})"),
                                 agent._wrap_lines(measure, [long_word], 40, width), (long_word[:3], width))


if __name__ == "__main__":
    unittest.main()
