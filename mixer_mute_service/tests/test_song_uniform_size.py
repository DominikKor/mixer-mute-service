"""Song layout (app/slide_layout.py): one text size per song and screen,
line breaks that keep phrases together, the shrink limit, verses split into
more slides, the minimum-size fallback, several screens in step; and how the
service uses it: verse navigation over split slides, the layout sent to each
screen, the setting taken over from before.

Measuring is a fixed width per character (0.6 em, like the fake agent), so no
screen or Tk is needed. No request leaves the process.
"""

import json
import math
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import slide_layout as L
from app import songs
from app.songs import SongService


def metrics(em=0.6):
    return L.Metrics({}, default=em * L.REF_PX)


HD = L.Screen(metrics(), 1920, 1080)
BOX_W, BOX_H = L.slide_box(1920, 1080, False, False)

SHORT = "Halleluja,\nHalleluja"
# One line of the file, 2.5 screen lines at 72 px.
STANZA = ("Großer Gott, wir loben dich, Herr, wir preisen deine Stärke. "
          "Vor dir neigt die Erde sich und bewundert deine Werke.")
# 12 short lines: 12 x 1.25 x 72 = 1080 px, more than the 929 px box; fits at
# 61.9 px (the size step below 72 % ... 66.2), within the shrink limit.
TWELVE = "\n".join(f"Zeile {i} vom Lied" for i in range(1, 13))
# 16 lines in two stanzas of 8 (an empty line between): too much even at
# 59 px (the limit), split at the empty line.
TWO_STANZAS = "\n".join([f"Erste Strophe {i}." for i in range(1, 9)] + [""] + [f"Zweite Strophe {i}" for i in range(1, 9)])


def plan(*texts, screens=(HD,), set_px=72, title_slide=None):
    slides = [L.SlideInput(t) for t in texts]
    if title_slide is not None:
        slides.insert(0, L.SlideInput(title_slide, splittable=False))
    return L.plan_song(list(screens), slides, set_px)


class LineBreakTests(unittest.TestCase):
    def test_a_long_line_breaks_after_punctuation(self):
        lines = L.slide_lines(metrics(), STANZA, 72, BOX_W, BOX_H)
        self.assertEqual(" ".join(lines), STANZA)
        self.assertTrue(all(metrics().width(line, 72) <= BOX_W for line in lines))
        # A line more than needed, for breaks after the phrases: only one
        # break inside a sentence (it is too long for one line).
        self.assertEqual(lines, ["Großer Gott, wir loben dich,", "Herr, wir preisen deine Stärke.",
                                 "Vor dir neigt die Erde sich", "und bewundert deine Werke."])

    def test_own_line_breaks_are_kept_and_never_joined(self):
        lines = L.slide_lines(metrics(), SHORT, 72, BOX_W, BOX_H)
        self.assertEqual(lines, ["Halleluja,", "Halleluja"])

    def test_fewest_lines_when_there_is_no_room_for_nicer_breaks(self):
        nice = L.slide_lines(metrics(), STANZA, 72, BOX_W)
        tight = L.slide_lines(metrics(), STANZA, 72, BOX_W, box_h=3 * 72 * L.SLIDE_LINE)
        self.assertGreater(len(nice), len(tight))
        self.assertEqual(len(tight), 3)
        self.assertEqual(" ".join(tight), STANZA)

    def test_repeat_marks_stay_with_their_word(self):
        text = "|: Alles ist eitel, du aber bleibst und wen du ins Buch des Lebens schreibst. Du aber bleibst. :|"
        lines = L.slide_lines(metrics(), text, 72, BOX_W, BOX_H)
        self.assertTrue(lines[0].startswith("|: Alles") and lines[-1].endswith("bleibst. :|"), lines)
        self.assertNotIn("|:", lines)
        self.assertNotIn(":|", lines)

    def test_a_word_wider_than_the_line_is_split_between_letters(self):
        word = "Halleluja" * 30
        lines = L.slide_lines(metrics(), word, 40, BOX_W)
        self.assertEqual("".join(lines), word)
        self.assertTrue(all(metrics().width(line, 40) <= BOX_W for line in lines))


class UniformSizeTests(unittest.TestCase):
    def test_short_song_keeps_the_set_size_on_every_slide(self):
        shown, (layout,) = plan(SHORT, "Kurz\nund gut", STANZA)
        self.assertEqual(len(shown), 3)
        self.assertEqual(layout.px, 72)
        self.assertEqual({s["px"] for s in layout.slides}, {72})

    def test_one_long_verse_shrinks_the_whole_song_within_the_limit(self):
        shown, (layout,) = plan(SHORT, TWELVE)
        self.assertEqual(len(shown), 2)                       # no split needed
        self.assertLess(layout.px, 72)
        self.assertGreaterEqual(layout.px, 72 * L.SHRINK_LIMIT)
        self.assertEqual([s["px"] for s in layout.slides], [layout.px, layout.px])
        self.assertEqual(layout.slides[1]["lines"], TWELVE.split("\n"))

    def test_beyond_the_limit_the_verse_is_split_not_shrunk(self):
        shown, (layout,) = plan(SHORT, TWO_STANZAS)
        self.assertEqual([(s.source, s.part, s.parts) for s in shown], [(0, 1, 1), (1, 1, 2), (1, 2, 2)])
        # Split at the empty line between the stanzas, which is dropped.
        self.assertEqual(shown[1].text.split("\n")[-1], "Erste Strophe 8.")
        self.assertEqual(shown[2].text.split("\n")[0], "Zweite Strophe 1")
        self.assertGreaterEqual(layout.px, 72 * L.SHRINK_LIMIT)
        self.assertEqual({s["px"] for s in layout.slides}, {layout.px})
        self.assertFalse(any(s["overflow"] for s in layout.slides))

    def test_split_prefers_a_sentence_end_and_even_parts(self):
        # 18 lines without an empty line; only line 9 ends a sentence.
        lines = [f"Wort {i} geht weiter" for i in range(1, 19)]
        lines[8] += "."
        shown, (layout,) = plan("\n".join(lines))
        self.assertEqual(len(shown), 2)
        self.assertTrue(shown[0].text.endswith("Wort 9 geht weiter."), shown[0].text)
        self.assertEqual([len(s["lines"]) for s in layout.slides], [9, 9])

    def test_a_long_line_of_the_file_is_split_at_a_sentence_end(self):
        # A whole stanza in one line of the file, far too long for one slide.
        text = " ".join([STANZA] * 6)
        shown, (layout,) = plan(text)
        self.assertGreater(len(shown), 1)
        for part in shown[:-1]:
            self.assertTrue(part.text.endswith("Werke."), part.text[-30:])
        self.assertEqual(" ".join(s.text for s in shown), text)    # nothing lost
        self.assertEqual({s["px"] for s in layout.slides}, {layout.px})

    def test_the_title_slide_is_never_split_and_never_pulls_the_song_down(self):
        title = "Ein sehr langer Liedtitel, " * 30
        shown, (layout,) = plan(SHORT, title_slide=title)
        self.assertEqual(len(shown), 2)
        self.assertEqual(layout.slides[1]["px"], 72)               # the lyrics keep their size
        self.assertLess(layout.slides[0]["px"], 72 * L.SHRINK_LIMIT)

    def test_minimum_size_is_the_last_fallback(self):
        giant = "Halleluja" * 400          # one word, several screens of letters
        shown, (layout,) = plan(SHORT, title_slide=giant)
        self.assertTrue(layout.slides[0]["overflow"])
        self.assertAlmostEqual(layout.slides[0]["px"], L.SLIDE_MIN_PX)
        self.assertEqual(layout.slides[1]["px"], 72)

    def test_next_slide_preview_follows_the_split(self):
        shown, (layout,) = plan(SHORT, TWO_STANZAS)
        preview = layout.slides[1]["preview"]
        self.assertTrue(preview is None or preview["lines"][0] == "Zweite Strophe 1")


class ScreensTests(unittest.TestCase):
    def test_all_screens_show_the_same_slides_split_for_the_tightest(self):
        narrow = L.Screen(metrics(), 1024, 768)    # 4:3: less room per line
        text = "\n".join(f"Eine längere Zeile Nummer {i:02d} im Lied" for i in range(1, 12))
        hd_only, _ = plan(text)
        both, (hd, four_three) = plan(text, screens=(HD, narrow))
        narrow_only, _ = plan(text, screens=(narrow,))
        self.assertEqual(len(hd_only), 1)
        self.assertEqual(len(narrow_only), 2)
        self.assertEqual(len(both), 2)             # the 4:3 screen decides the split
        self.assertEqual(len(hd.slides), len(four_three.slides))
        self.assertEqual({s["px"] for s in hd.slides}, {hd.px})          # each uniform on its own
        self.assertEqual({s["px"] for s in four_three.slides}, {four_three.px})

    def test_each_screen_keeps_its_own_size(self):
        small_font = L.Screen(metrics(0.5), 1920, 1080)
        _, (a, b) = plan(TWELVE, screens=(HD, small_font))
        self.assertLessEqual(a.px, b.px)


class AgentStub:
    """The display agent: 0.6 em per character, 1920 x 1080; old: no
    /song/metrics and no layout."""

    def __init__(self, old=False):
        self.old, self.calls = old, []

    async def __call__(self, cfg, ip, path, payload, timeout=3.0):
        self.calls.append((ip, path, payload))
        if path == "/song/metrics":
            if self.old:
                raise songs.AgentError("not found", 502, agent_status=404)
            return {"ok": True, "width": 1920, "height": 1080, "ref": 100, "widths": [60] * len(payload["chars"])}
        if self.old and "layout" in payload:
            raise songs.AgentError("bad request", 502, agent_status=400)
        return {"ok": True, "action": payload["action"]}

    def shows(self):
        return [p for _ip, path, p in self.calls if path == "/song"]


def fake_request(service):
    cfg = SimpleNamespace(desktop_display=SimpleNamespace(enabled=True, token_env="DESKTOP_AGENT_TOKEN"))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cfg=cfg, songs=service)))


def service_with(*texts, **settings):
    service = SongService(Path(tempfile.mkdtemp()))
    service.songs = {"a.foil": {"title": "Lied", "copyright": "(c) Test", "books": [],
                                "slides": [{"key": str(i + 1), "text": t} for i, t in enumerate(texts)]}}
    if settings:
        service.settings = service.settings.model_copy(update=settings)
    return service


class NavigationTests(unittest.IsolatedAsyncioTestCase):
    async def test_steps_and_counts_go_over_the_split_slides(self):
        service = service_with(SHORT, TWO_STANZAS, "Kurz")
        agent = AgentStub()
        request = fake_request(service)
        with patch("app.display_agent.call", agent):
            answer = await songs.show_slide(request, "a.foil", 0)
            self.assertEqual(answer["slide_count"], 5)     # title, 1, 2.1, 2.2, 3
            keys = [s["key"] for s in answer["song"]["shown"]]
            self.assertEqual(keys, ["Titel", "1", "2.1", "2.2", "3"])
            texts = []
            for _ in range(4):
                await songs.song_next(request)
                texts.append(agent.shows()[-1]["text"])
            self.assertTrue(texts[1].startswith("Erste Strophe 1.") and texts[2].startswith("Zweite Strophe 1"))
            self.assertEqual(texts[3], "Kurz")
            await songs.song_next(request)                   # after the last: dark
            self.assertEqual(agent.shows()[-1]["action"], "blank")
            await songs.song_prev(request)
            self.assertEqual(service.live["slide"], 4)
            # Direct jump to the second part; its layout: lines at the song's size.
            await songs.show_slide(request, "a.foil", 3)
            sent = agent.shows()[-1]
            self.assertEqual(sent["layout"]["lines"][0], "Zweite Strophe 1")
            self.assertEqual(sent["layout"]["screen"], [1920, 1080])
            # The second part has no title/copyright rows and no preview of the past.
            self.assertEqual((sent["title"], sent["copyright"], sent["next"]), ("", "", "Kurz"))
            live = songs._live_view(service)
            self.assertEqual(live["slide_count"], 5)
            self.assertEqual(live["song"]["layout"]["slides"][3]["lines"][0], "Zweite Strophe 1")

    async def test_every_screen_gets_its_own_layout_of_the_same_slides(self):
        service = service_with(SHORT, TWO_STANZAS, targets=[songs.SongTarget(ip="192.168.188.11", monitor=2),
                                                         songs.SongTarget(ip="192.168.188.12", monitor=1)])
        sizes = {"192.168.188.11": (1920, 1080), "192.168.188.12": (1024, 768)}
        calls = []

        async def two_agents(cfg, ip, path, payload, timeout=3.0):
            calls.append((ip, path, payload))
            if path == "/song/metrics":
                w, h = sizes[ip]
                return {"ok": True, "width": w, "height": h, "ref": 100, "widths": [60] * len(payload["chars"])}
            return {"ok": True}

        with patch("app.display_agent.call", two_agents):
            await songs.show_slide(fake_request(service), "a.foil", 2)
        sent = {ip: p for ip, path, p in calls if path == "/song"}
        self.assertEqual(sent["192.168.188.11"]["text"], sent["192.168.188.12"]["text"])
        self.assertEqual(sent["192.168.188.11"]["layout"]["screen"], [1920, 1080])
        self.assertEqual(sent["192.168.188.12"]["layout"]["screen"], [1024, 768])
        self.assertEqual((sent["192.168.188.11"]["monitor"], sent["192.168.188.12"]["monitor"]), (2, 1))

    async def test_old_agent_gets_the_split_without_layout_and_is_remembered(self):
        service = service_with(SHORT, TWO_STANZAS)
        agent = AgentStub(old=True)
        request = fake_request(service)
        with patch("app.display_agent.call", agent):
            answer = await songs.show_slide(request, "a.foil", 2)
            self.assertEqual(answer["slide_count"], 4)    # split with the estimated widths
            first = agent.shows()
            self.assertEqual(len(first), 1)                # estimate: no layout sent at all
            self.assertNotIn("layout", first[0])
            n = len(agent.calls)
            await songs.song_next(request)
            self.assertEqual(len(agent.calls), n + 1)      # no metrics asked again, one request

    async def test_the_layout_is_worked_out_once_per_song_and_settings(self):
        service = service_with(SHORT, TWELVE)
        request = fake_request(service)
        with patch("app.display_agent.call", AgentStub()), \
                patch.object(L, "plan_song", wraps=L.plan_song) as planned:
            await songs.show_slide(request, "a.foil", 0)
            for _ in range(2):
                await songs.song_next(request)
            self.assertEqual(planned.call_count, 1)        # stepping on: the kept layout
            service.save_settings(service.settings.model_copy(update={"font_size": 60}))
            await songs.song_prev(request)
            self.assertEqual(planned.call_count, 2)        # new settings: laid out again
        self.assertEqual(service.layout_rev, 2)

    async def test_settings_change_keeps_the_live_slide_in_range(self):
        service = service_with(TWO_STANZAS)
        request = fake_request(service)
        with patch("app.display_agent.call", AgentStub()):
            await songs.show_slide(request, "a.foil", 2)   # title, 1.1, 1.2
            service.save_settings(service.settings.model_copy(update={"font_size": 40}))
            await songs.ensure_layout(request, "a.foil")
        self.assertEqual(len(service.slides("a.foil")), 2)  # no split at 40 px


class SettingTakenOverTests(unittest.TestCase):
    def test_the_old_largest_size_becomes_the_fixed_size(self):
        with tempfile.TemporaryDirectory() as directory:
            old = {"library_path": "", "monitor": 2, "fullscreen": True, "font": "Verdana", "font_size": 84,
                   "text_color": "#ffffff", "background_color": "#000000", "title_slide": True,
                   "show_title": True, "show_copyright": True, "text_align": "center", "show_next_preview": True}
            Path(directory, "song_display.json").write_text(json.dumps(old), encoding="utf-8")
            self.assertEqual(SongService(Path(directory)).settings.font_size, 84)


class LayoutSpeedTests(unittest.TestCase):
    def test_a_long_song_is_laid_out_quickly(self):
        import time
        texts = [STANZA + "\n" + TWELVE for _ in range(150)]
        start = time.perf_counter()
        shown, _ = plan(*texts)
        self.assertLess(time.perf_counter() - start, 10)
        self.assertGreaterEqual(len(shown), 150)


class ReviewFixTests(unittest.TestCase):
    def test_the_shrink_limit_itself_is_a_size(self):
        # At 110 the steps are 110, 103.4, 97.2, 91.4, 85.9: the limit 90.2
        # lies between. 8 lines of 31 characters fit at 90.2, not at 91.4.
        verse = "\n".join(["x" * 31] * 8)
        shown, (layout,) = plan(verse, set_px=110)
        self.assertEqual(len(shown), 1)                                 # no split needed ...
        self.assertAlmostEqual(layout.px, 110 * L.SHRINK_LIMIT)         # ... drawn at the limit, not 85.9
        self.assertEqual(layout.slides[0]["lines"], verse.split("\n"))

    def test_beyond_the_limit_at_odd_sizes_the_verse_is_split(self):
        verse = "\n".join(["x" * 31] * 9)     # 9 lines do not fit at 90.2 either
        shown, (layout,) = plan(verse, set_px=110)
        self.assertEqual(len(shown), 2)
        self.assertGreaterEqual(layout.px, 110 * L.SHRINK_LIMIT - 1e-9)

    def test_every_screen_stays_at_or_above_its_limit(self):
        # Two screens whose fonts differ: on one "m" is wide, on the other
        # "x". Each needs another verse split; splitting only for one of
        # them would push the other far below its limit (to about 31 px).
        wide_m = L.Screen(L.Metrics({"m": 120}, default=50), 1920, 1080)
        wide_x = L.Screen(L.Metrics({"x": 120}, default=50), 1920, 1080)
        verse_m = "\n".join(["mmmmm mmmmm mmmmm mmmmm mmmmm mmmmm"] * 12)
        verse_x = "\n".join(["xxxxx xxxxx xxxxx xxxxx xxxxx xxxxx"] * 12)
        shown, layouts = plan(verse_m, verse_x, screens=(wide_m, wide_x))
        self.assertEqual({s.source for s in shown if s.parts > 1}, {0, 1})    # both verses split
        for layout in layouts:
            self.assertGreaterEqual(layout.px, 72 * L.SHRINK_LIMIT - 1e-9)
            self.assertFalse(any(slide["overflow"] for slide in layout.slides))
        self.assertEqual(len(layouts[0].slides), len(layouts[1].slides))      # the same slides everywhere

    def test_empty_lines_never_make_a_slide_or_stop_the_split(self):
        lines = [f"Zeile {i} der Strophe." for i in range(1, 9)]
        verse = "\n\n" + "\n".join(lines[:4]) + "\n\n\n\n" + "\n".join(lines[4:]) + "\n\n"
        shown, (layout,) = plan(verse, set_px=150)
        self.assertGreater(len(shown), 1)
        self.assertTrue(all(slide.text.strip() for slide in shown))
        self.assertEqual("\n".join(s.text for s in shown).split("\n").count("Zeile 5 der Strophe."), 1)
        self.assertFalse(any(slide["overflow"] for slide in layout.slides))

    def test_live_slide_follows_its_verse_part(self):
        old = [{"source": 0}, {"source": 1, "part": 1, "parts": 3}, {"source": 1, "part": 2, "parts": 3},
               {"source": 1, "part": 3, "parts": 3}, {"source": 2}]
        new = [{"source": 0}, {"source": 1, "part": 1, "parts": 2}, {"source": 1, "part": 2, "parts": 2}, {"source": 2}]
        self.assertEqual([songs.remap_slide(old, new, i) for i in range(5)], [0, 1, 1, 2, 3])
        unsplit = [{"source": 0}, {"source": 1}, {"source": 2}]
        self.assertEqual([songs.remap_slide(old, unsplit, i) for i in range(5)], [0, 1, 1, 1, 2])
        self.assertEqual(songs.remap_slide(unsplit, old, 2), 4)


class ServiceReviewFixTests(unittest.IsolatedAsyncioTestCase):
    async def test_live_slide_is_remapped_when_the_layout_changes(self):
        service = service_with(SHORT, TWO_STANZAS, "Kurz")
        request = fake_request(service)
        with patch("app.display_agent.call", AgentStub()):
            await songs.show_slide(request, "a.foil", 3)       # title, 1, 2.1, [2.2], 3
            self.assertEqual(service.slides("a.foil")[3]["key"], "2.2")
            service.save_settings(service.settings.model_copy(update={"font_size": 40}))
            await songs.relayout_live(request)
        self.assertEqual([s["key"] for s in service.slides("a.foil")], ["Titel", "1", "2", "3"])
        self.assertEqual(service.live["slide"], 2)              # still verse 2, not verse 3

    async def test_a_jump_from_an_older_numbering_lands_on_the_meant_slide(self):
        service = service_with(SHORT, TWO_STANZAS, "Kurz")
        agent = AgentStub()
        request = fake_request(service)
        with patch("app.display_agent.call", agent):
            answer = await songs.show_slide(request, "a.foil", 0)
            old_rev = answer["layout_rev"]
            service.save_settings(service.settings.model_copy(update={"font_size": 40}))
            await songs.relayout_live(request)
            # The page still counts in the old layout: its slide 4 was "3".
            answer = await songs.show_slide(request, "a.foil", 4, rev=old_rev)
            self.assertEqual(agent.shows()[-1]["text"], "Kurz")
            self.assertEqual(answer["slide"], 3)
            stale = await songs.show_slide(request, "a.foil", 1, rev=12345)
            self.assertEqual(stale.status_code, 409)

    async def test_an_updated_agent_is_asked_for_everything_again(self):
        service = service_with(SHORT, TWO_STANZAS)
        old = AgentStub(old=True)
        request = fake_request(service)
        with patch("app.display_agent.call", old):
            await songs.show_slide(request, "a.foil", 1)
        self.assertIn(("192.168.188.11", "no_metrics"), service.agent_memory)

        class Updated(AgentStub):
            async def __call__(self, cfg, ip, path, payload, timeout=3.0):
                answer = await super().__call__(cfg, ip, path, payload, timeout)
                return {**answer, "started_at": 2000.0}

        updated = Updated()
        with patch("app.display_agent.call", updated):
            await songs.song_next(request)                  # the new agent answers with its start time
            self.assertNotIn(("192.168.188.11", "no_metrics"), service.agent_memory)
            await songs.song_next(request)                  # now: measured, laid out, sent with layout
        self.assertIn("layout", updated.shows()[-1])
        self.assertTrue(any(path == "/song/metrics" for _ip, path, _p in updated.calls))

    async def test_a_refused_value_does_not_demote_the_agent(self):
        service = service_with(SHORT)

        async def picky(cfg, ip, path, payload, timeout=3.0):
            if path == "/song/metrics":
                return {"ok": True, "width": 1920, "height": 1080, "ref": 100, "widths": [60] * len(payload["chars"])}
            raise songs.AgentError("unprocessable", 502, agent_status=422)

        with patch("app.display_agent.call", picky):
            await songs.show_slide(fake_request(service), "a.foil", 1)
        self.assertNotIn(("192.168.188.11", "song_level"), service.agent_memory)

    async def test_a_new_window_size_lays_the_song_out_again(self):
        service = service_with(SHORT, TWELVE)
        calls = []

        async def agent(cfg, ip, path, payload, timeout=3.0):
            calls.append((path, payload))
            if path == "/song/metrics":
                return {"ok": True, "width": 1920, "height": 1080, "ref": 100, "widths": [60] * len(payload["chars"])}
            return {"ok": True, "action": payload["action"], "window": [payload["monitor"], 1280, 1024]}

        request = fake_request(service)
        with patch("app.display_agent.call", agent):
            await songs.show_slide(request, "a.foil", 1)
            self.assertEqual(calls[-1][1]["layout"]["screen"], [1920, 1080])
            await songs.song_next(request)
        self.assertEqual(calls[-1][1]["layout"]["screen"], [1280, 1024])   # the agent's real window now
        self.assertEqual(sum(1 for path, _ in calls if path == "/song/metrics"), 1)   # widths kept

    async def test_all_screens_down_each_screen_is_reset_to_its_own_request(self):
        service = service_with(SHORT, TWO_STANZAS, targets=[songs.SongTarget(ip="192.168.188.11", monitor=2),
                                                         songs.SongTarget(ip="192.168.188.12", monitor=1)])
        sizes = {"192.168.188.11": (1920, 1080), "192.168.188.12": (1024, 768)}
        down = {"value": False}

        async def agents(cfg, ip, path, payload, timeout=3.0):
            if path == "/song/metrics":
                w, h = sizes[ip]
                return {"ok": True, "width": w, "height": h, "ref": 100, "widths": [60] * len(payload["chars"])}
            if down["value"]:
                raise songs.AgentError("display agent unavailable", 502)
            return {"ok": True}

        request = fake_request(service)
        with patch("app.display_agent.call", agents):
            await songs.show_slide(request, "a.foil", 2)
            down["value"] = True
            await songs.song_next(request)
            for ip, (w, h) in sizes.items():
                cfg_payload = service.senders[ip].latest
                self.assertIsNotNone(cfg_payload)
                self.assertEqual(cfg_payload[1]["layout"]["screen"], [w, h])   # its own layout, not the other's
            service.close()
        self.assertEqual(service.live["slide"], 2)


if __name__ == "__main__":
    unittest.main()
