"""One text size for all slides of a song (display agent _song_size /
_layout_slide, the same as songFitSize()/layoutSlide() in
app/static/js/songs.js), the "fit" list the service sends for it, the
agent's check and cache of it, and the setting taken over from before.

Measuring is a fixed per-character width (roughly Verdana: 0.6 em), so no
screen or Tk is needed.
"""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import require_tk

# None without tkinter (tests skip); with REQUIRE_TK=1 (CI) that is an error.
tkinter = require_tk()

if tkinter is not None:
    from desktop_agent import show_image_agent as agent


def measure(text, size):
    return len(text) * size * 0.6


SHORT = "Halleluja,\nHalleluja"
LONG = ("Großer Gott, wir loben dich, Herr, wir preisen deine Stärke. Vor dir neigt die Erde sich "
        "und bewundert deine Werke. Wie du warst vor aller Zeit, so bleibst du in Ewigkeit. "
        "Alles, was dich preisen kann, Cherubim und Seraphinen, stimmen dir ein Loblied an. "
        "Alle Engel, die dir dienen, rufen dir stets ohne Ruh: Heilig, heilig, heilig zu. "
        "Heilig, Herr Gott Zebaoth! Heilig, Herr der Kriegesheere!")
HUGE = "\n".join(["Christ be with me, Christ within me, Christ behind me, Christ before me"] * 14)


def fit(*texts, first_title=False):
    """The fit list of a song: title and copyright rows on the first slide only."""
    return [{"text": t, "title": first_title and i == 0, "copyright": first_title and i == 0}
            for i, t in enumerate(texts)]


def size_of(fit_list, width=1920, height=1080, set_px=72):
    return agent._song_size(measure, fit_list, set_px * height / 1080, agent.SLIDE_MIN_PX * height / 1080,
                            width, height)


def layout(text, song_px, width=1920, height=1080, set_px=72, title=False, copyright=False):
    box_w, box_h = agent._slide_box(width, height, title, copyright)
    return agent._layout_slide(measure, text, song_px, set_px * height / 1080,
                               agent.SLIDE_MIN_PX * height / 1080, box_w, box_h)


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class UniformSongSizeTests(unittest.TestCase):
    def test_short_verses_keep_the_set_size_on_every_slide(self):
        song = fit(SHORT, "Kurz\nund gut", "Noch eine\nkurze Strophe")
        self.assertEqual(size_of(song), 72)
        for entry in song:
            px, lines, fits = layout(entry["text"], 72)
            self.assertEqual((px, lines, fits), (72, entry["text"].split("\n"), True))

    def test_one_long_verse_makes_the_whole_song_smaller(self):
        alone_short = agent._fit_text(measure, SHORT, 72, 40, *agent._slide_box(1920, 1080, False, False))[0]
        alone_long = agent._fit_text(measure, LONG, 72, 40, *agent._slide_box(1920, 1080, False, False))[0]
        self.assertEqual(alone_short, 72)        # before: the short verse big ...
        self.assertLess(alone_long, 72)          # ... the long one small
        song_px = size_of(fit(SHORT, LONG))
        self.assertEqual(song_px, alone_long)    # now: both at the long verse's size
        self.assertGreaterEqual(song_px, 40)
        for text in (SHORT, LONG):
            px, lines, fits = layout(text, song_px)
            self.assertEqual(px, song_px)
            self.assertTrue(fits)
            box_w, box_h = agent._slide_box(1920, 1080, False, False)
            self.assertTrue(all(measure(line, px) <= box_w for line in lines))
            self.assertLessEqual(len(lines) * px * agent.SLIDE_LINE, box_h)
        self.assertEqual(layout(SHORT, song_px)[1], SHORT.split("\n"))   # its own lines kept

    def test_the_size_is_one_of_the_sizes_tried(self):
        sizes = list(agent._slide_sizes(72, 40))
        self.assertIn(size_of(fit(SHORT, LONG)), sizes)

    def test_title_and_copyright_rows_count_for_their_slide(self):
        # The first slide with title and copyright has a smaller box: a verse
        # that fits big alone may make the song smaller there.
        box_full = agent._slide_box(1920, 1080, False, False)
        box_first = agent._slide_box(1920, 1080, True, True)
        self.assertEqual(box_full[0], box_first[0])
        self.assertAlmostEqual(box_full[1] - box_first[1], (agent.SLIDE_TITLE_H + agent.SLIDE_COPY_H) * 1080)
        verse = "\n".join(["Eine Zeile"] * 9)
        self.assertEqual(size_of(fit(verse, SHORT)), 72)
        self.assertLess(size_of(fit(verse, SHORT, first_title=True)), 72)

    def test_a_slide_too_long_even_at_the_minimum_keeps_the_old_fallback_alone(self):
        song = fit(SHORT, LONG, HUGE)
        song_px = size_of(song)
        self.assertEqual(song_px, size_of(fit(SHORT, LONG)))   # HUGE does not drag the others down
        px, lines, fits = layout(HUGE, song_px)
        self.assertEqual(px, 40)
        self.assertFalse(fits)
        self.assertEqual((px, lines), agent._fit_text(measure, HUGE, 72, 40, *agent._slide_box(1920, 1080, False, False)))
        self.assertIsNone(size_of(fit(HUGE)))                  # nothing fits: no song size
        self.assertEqual(layout(SHORT, None)[0], 72)           # without a song size: the slide's own

    def test_each_screen_gets_its_own_size(self):
        song = fit(SHORT, LONG)
        hd = size_of(song, 1920, 1080)
        four_three = size_of(song, 1024, 768)
        # Relative to the screen height the 4:3 screen (narrower) needs a
        # smaller size than the 16:9 one; on each screen all slides share it.
        self.assertLess(four_three / 768, hd / 1080)
        for text in (SHORT, LONG):
            self.assertEqual(layout(text, four_three, 1024, 768)[0], four_three)

    def test_the_song_size_is_measured_once_per_song_screen_and_look(self):
        agent._SONG_SIZE_CACHE.clear()
        song = fit(SHORT, LONG)
        with patch.object(agent, "_song_size", wraps=agent._song_size) as computed:
            for _ in range(5):  # stepping through the verses
                agent._cached_song_size(measure, song, "Verdana", 72, 40, 1920, 1080)
            self.assertEqual(computed.call_count, 1)
            agent._cached_song_size(measure, song, "Verdana", 72, 40, 1280, 720)     # other screen
            agent._cached_song_size(measure, song, "Arial", 72, 40, 1920, 1080)      # other font
            agent._cached_song_size(measure, song, "Verdana", 60, 40, 1920, 1080)    # other size
            agent._cached_song_size(measure, fit(SHORT), "Verdana", 72, 40, 1920, 1080)  # other song
            self.assertEqual(computed.call_count, 5)
        for i in range(agent.SONG_SIZE_CACHE_MAX + 10):
            agent._cached_song_size(measure, fit(f"Lied {i}"), "Verdana", 72, 40, 1920, 1080)
        self.assertLessEqual(len(agent._SONG_SIZE_CACHE), agent.SONG_SIZE_CACHE_MAX)


SHOW = {
    "action": "show", "monitor": 2, "fullscreen": True,
    "style": {"font": "Verdana", "font_size": 72, "text_color": "#ffffff", "background_color": "#000000",
              "align": "center"},
    "text": "Zeile 1", "title": "", "copyright": "", "next": "",
}


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class FitRequestTests(unittest.TestCase):
    def test_fit_is_optional_and_checked(self):
        self.assertTrue(agent._valid_song_request(SHOW))                       # an older service
        self.assertTrue(agent._valid_song_request({**SHOW, "fit": fit("a", "b")}))
        self.assertTrue(agent._valid_song_request({**SHOW, "fit": []}))
        for bad in ("x", [1], [{"text": "a"}], [{"text": "a", "title": 1, "copyright": False}],
                    [{"text": "a", "title": False, "copyright": False, "extra": 1}],
                    [{"text": "x" * 8001, "title": False, "copyright": False}],
                    fit(*["a"] * (agent.MAX_FIT_SLIDES + 1))):
            with self.subTest(bad=str(bad)[:40]):
                self.assertFalse(agent._valid_song_request({**SHOW, "fit": bad}))


class SongSettingTakenOverTests(unittest.TestCase):
    def test_the_old_largest_size_becomes_the_fixed_size(self):
        from app.songs import SongService
        with tempfile.TemporaryDirectory() as directory:
            old = {"library_path": "", "monitor": 2, "fullscreen": True, "font": "Verdana", "font_size": 84,
                   "text_color": "#ffffff", "background_color": "#000000", "title_slide": True,
                   "show_title": True, "show_copyright": True, "text_align": "center", "show_next_preview": True}
            Path(directory, "song_display.json").write_text(json.dumps(old), encoding="utf-8")
            service = SongService(Path(directory))
            self.assertEqual(service.settings.font_size, 84)
            service.songs = {"a.foil": {"title": "A", "copyright": "(c)", "books": [],
                                        "slides": [{"key": "1", "text": SHORT}, {"key": "2", "text": LONG}]}}
            payload = service.slide_payload("a.foil", 2)
            self.assertEqual(payload["style"]["font_size"], 84)
            self.assertEqual(payload["fit"], [{"text": "A", "title": False, "copyright": True},
                                              {"text": SHORT, "title": False, "copyright": False},
                                              {"text": LONG, "title": False, "copyright": False}])


if __name__ == "__main__":
    unittest.main()
