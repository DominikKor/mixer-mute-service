"""Lyrics layout (display agent _fit_text/_wrap_lines/_preview_layout, the
same as renderSlide() in app/static/js/songs.js) and line breaks from real
FoilPresenter files.

Many FoilPresenter files keep a whole stanza on one line (FoilPresenter wraps
it on screen); such a stanza must be wrapped at a readable size, not shrunk
into one unreadable line. The text's own line breaks are kept. Measuring is
a fixed per-character width here, so no screen or Tk is needed.
"""

import unittest

from app.foil import parse_foil
from tests.helpers import require_tk

# None without tkinter (tests skip); with REQUIRE_TK=1 (CI) that is an error.
tkinter = require_tk()

if tkinter is not None:
    from desktop_agent import show_image_agent as agent


def measure(text, size):
    """Roughly Verdana: 0.6 em per character."""
    return len(text) * size * 0.6


# 1920 x 1080 without title/copyright: the box the agent uses.
W, H = 1920, 1080
BOX_W = W - 2 * 0.06 * W
BOX_H = H - 2 * 0.07 * H
MAX_PX, MIN_PX = 72.0, 40.0

ONE_LINE = ("Meine Seele singe, denn die Nacht ist vorbei. Mach dich auf und bringe deinem Gott Lob und Preis. "
            "Alle Schöpfung juble, wenn der Tag nun anbricht. Gottes Töchter und Söhne strahlen in seinem Licht. "
            "Der wahre Morgenstern, er ist aufgegangen, der Erlöser ist hier. Ich weiß, dass Jesus lebt, "
            "er ist auferstanden, und er lebt auch in mir, lebt auch in mir.")

# The structure FoilPresenter writes (CRLF, tabs), one stanza on one line and
# one with its own line breaks and an empty line inside.
FOIL = (
    '<?xml version="1.0" encoding="UTF-8"?>\r\n<foilpresenterfolie version="00300.000105">\r\n'
    "<titel>\r\n\t<titelstring>Meine Seele singe</titelstring>\r\n</titel>\r\n"
    "<reihenfolge>\r\n\t<name>Standard</name>\r\n\t<strophennummer>0</strophennummer>\r\n"
    "\t<strophennummer>1</strophennummer>\r\n</reihenfolge>\r\n<strophen>\r\n"
    "<strophe>\r\n\t\t<align>0</align>\r\n\t\t<font>Verdana</font>\r\n\t\t<textsize>14</textsize>\r\n"
    f"\t\t<key>1</key>\r\n\t\t<text>{ONE_LINE}</text>\r\n\t\t<sortnr>1</sortnr>\r\n\t</strophe>\r\n"
    "<strophe>\r\n\t\t<key>2</key>\r\n"
    "\t\t<text>|: Alles ist eitel, du aber bleibst\r\nund wen du ins Buch des\r\nLebens schreibst.\r\n\r\n"
    "Du aber bleibst. :|</text>\r\n\t\t<sortnr>2</sortnr>\r\n\t</strophe>\r\n"
    "</strophen>\r\n</foilpresenterfolie>\r\n"
).encode("utf-8")


class FoilLineBreakTests(unittest.TestCase):
    def test_breaks_arrive_as_in_the_file(self):
        slides = parse_foil(FOIL)["slides"]
        self.assertEqual(slides[0]["text"], ONE_LINE)            # no break in the file: none here
        self.assertEqual(slides[1]["text"],
                         "|: Alles ist eitel, du aber bleibst\nund wen du ins Buch des\nLebens schreibst.\n\n"
                         "Du aber bleibst. :|")              # CRLF -> LF, the empty line kept


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class SlideLayoutTests(unittest.TestCase):
    def test_stanza_on_one_line_is_wrapped_readably(self):
        px, lines = agent._fit_text(measure, ONE_LINE, MAX_PX, MIN_PX, BOX_W, BOX_H)
        self.assertGreaterEqual(px, MIN_PX)
        self.assertGreater(len(lines), 5)
        self.assertTrue(all(measure(line, px) <= BOX_W for line in lines))
        self.assertLessEqual(len(lines) * px * agent.SLIDE_LINE, BOX_H)
        self.assertEqual(" ".join(lines), ONE_LINE)           # only broken at spaces, nothing lost

    def test_own_line_breaks_are_kept(self):
        text = "Großer Gott, wir loben dich,\nHerr, wir preisen deine Stärke.\n\nVor dir neigt die Erde sich"
        px, lines = agent._fit_text(measure, text, MAX_PX, MIN_PX, BOX_W, BOX_H)
        self.assertEqual(px, MAX_PX)
        self.assertEqual(lines, text.split("\n"))

    def test_own_lines_kept_when_they_cost_at_most_15_percent(self):
        # 42 characters fit at 63.6 px, the wrapped text would get 72 px:
        # 88 % of it, so the line stays whole (a little smaller).
        line = "Herr, du bist mein Leben, Herr du bist mei"
        px, lines = agent._fit_text(measure, line, MAX_PX, MIN_PX, BOX_W, BOX_H)
        self.assertEqual(lines, [line])
        self.assertTrue(0.85 * MAX_PX <= px < MAX_PX)

    def test_own_lines_wrapped_when_they_would_be_much_smaller(self):
        # 55 characters would need 49.7 px whole, under 85 % of the 72 px the
        # wrapped text gets: wrapped, so the slides of a song keep one size.
        line = "Du bist meine Stärke, nichts sonst hab ich hier, Herr!!"
        self.assertEqual(len(line), 55)
        px, lines = agent._fit_text(measure, "Kurz\n" + line, MAX_PX, MIN_PX, BOX_W, BOX_H)
        self.assertEqual(px, MAX_PX)
        self.assertEqual(lines[0], "Kurz")
        self.assertEqual(" ".join(lines[1:]), line)
        self.assertGreater(len(lines), 2)

    def test_repeat_marks_stay_with_their_word(self):
        self.assertEqual(agent._word_units("|: Alles ist eitel, du aber bleibst :|"),
                         ["|: Alles", "ist", "eitel,", "du", "aber", "bleibst :|"])
        width = measure("|: Alles ist eitel", MAX_PX)
        lines = agent._wrap_lines(measure, ["|: Alles ist eitel, du aber bleibst :|"], MAX_PX, width)
        self.assertNotIn("|:", lines)
        self.assertNotIn(":|", lines)
        self.assertTrue(lines[0].startswith("|: Alles") and lines[-1].endswith("bleibst :|"))

    def test_text_taller_than_its_box_is_cut_at_the_bottom(self):
        # Box from 100 to 500, 40 px lines every 50 px: 100, 150, ... 450 end at 490.
        self.assertEqual(agent._visible_lines(20, 100, 40, 500), 8)
        self.assertEqual(agent._visible_lines(3, 100, 40, 500), 3)
        self.assertEqual(agent._visible_lines(3, 100, 40, 130), 0)

    def test_never_below_the_minimum(self):
        text = "\n".join([ONE_LINE] * 4)  # far too much text
        px, lines = agent._fit_text(measure, text, MAX_PX, MIN_PX, BOX_W, BOX_H)
        self.assertEqual(px, MIN_PX)
        self.assertTrue(all(measure(line, px) <= BOX_W for line in lines))

    def test_minimum_never_above_the_set_size(self):
        px, _ = agent._fit_text(measure, ONE_LINE, 30.0, MIN_PX, BOX_W, BOX_H)
        self.assertEqual(px, 30.0)

    def test_a_word_wider_than_the_line_is_split(self):
        word = "Halleluja" * 30
        lines = agent._wrap_lines(measure, [word], MIN_PX, BOX_W)
        self.assertEqual("".join(lines), word)
        self.assertTrue(all(measure(line, MIN_PX) <= BOX_W for line in lines))

    def test_double_spaces_and_empty_lines_survive(self):
        lines = agent._wrap_lines(measure, ["Fa la la  la la", ""], MAX_PX, BOX_W)
        self.assertEqual(lines, ["Fa la la  la la", ""])

    def test_next_slide_preview_wraps_instead_of_vanishing(self):
        px, lines = agent._fit_text(measure, "Kurz\nund gut", MAX_PX, MIN_PX, BOX_W, BOX_H)
        preview = agent._preview_layout(measure, lines, [ONE_LINE], px, BOX_W, BOX_H, H)
        self.assertIsNotNone(preview)
        pp, shown = preview
        self.assertGreaterEqual(pp, agent.PREVIEW_MIN)
        self.assertTrue(ONE_LINE.startswith(shown[0]))
        self.assertTrue(all(measure(line, pp) <= BOX_W for line in shown))


if __name__ == "__main__":
    unittest.main()
