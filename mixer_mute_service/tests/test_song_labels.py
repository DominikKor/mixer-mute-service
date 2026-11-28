"""Song labels with the songbook number in front (js/songs.js songLabel): "GHS 123 · Titel",
the title alone for a song without a number; the song picker finds songs by number
("123", "GHS 123", "ghs123"); the row title that is the song's title gets the number
too, the places that show a song use songLabel (quickjs; skipped where quickjs is
missing, an error in CI with REQUIRE_TK=1).

Run from mixer_mute_service:  python -m tests.test_song_labels
"""

import json
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

JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js"
SONGS = (JS / "songs.js").read_text(encoding="utf-8")
PROGRAM = (JS / "program.js").read_text(encoding="utf-8")

LIBRARY = [
    {"id": "a.foil", "title": "Aus meines Herzens Grunde", "books": [{"name": "Glauben Hoffen Singen", "number": "123"}]},
    {"id": "b.foil", "title": "Above all", "books": [{"name": "Feiert Jesus", "number": "45"},
                                                      {"name": "Glauben Hoffen Singen", "number": "371"}]},
    {"id": "c.foil", "title": "Ohne Nummer", "books": []},
    {"id": "d.foil", "title": "Nur anderes Buch", "books": [{"name": "Feiert Jesus", "number": "12"}]},
    {"id": "e.foil", "title": "Eins", "books": [{"name": "GHS", "number": "12"}]},
    {"id": "f.foil", "title": "Zwölf Apostel", "books": [{"name": "Glauben Hoffen Singen", "number": "112"}]},
    {"id": "g.foil", "title": "Leere Nummer", "books": [{"name": "Glauben Hoffen Singen", "number": " "}]},
]


def function_body(source: str, name: str) -> str:
    return re.search(rf"function {name}\(.*?\n\}}\n", source, re.S).group(0)


@unittest.skipIf(quickjs is None, "quickjs not installed")
class SongLabelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = quickjs.Context()
        cls.js.eval("const SONG_INDEX = new Map(); let SONG_LIST = [];")
        for name in ("bookAbbr", "bookNumber", "isPreferredBook", "songBookList", "songMainBook", "bookText",
                     "songNumberText", "songLabel", "songBooks", "songMatches"):
            cls.js.eval(function_body(SONGS, name))
        cls.js.eval(function_body(PROGRAM, "programRowTitle"))
        cls.js.eval(f"SONG_LIST = {json.dumps(LIBRARY)}; for (const s of SONG_LIST) SONG_INDEX.set(s.id, s);")

    def label(self, song_id: str) -> str:
        return self.js.eval(f"songLabel(SONG_INDEX.get({json.dumps(song_id)}))")

    def found(self, query: str) -> list:
        return json.loads(self.js.eval(f"JSON.stringify(songMatches({json.dumps(query)}).map(s => s.id))"))

    def test_number_in_front_of_the_title(self):
        self.assertEqual(self.label("a.foil"), "GHS 123 · Aus meines Herzens Grunde")
        self.assertEqual(self.label("e.foil"), "GHS 12 · Eins")  # a book that is called "GHS" already

    def test_ghs_comes_first_when_the_song_is_in_several_books(self):
        self.assertEqual(self.label("b.foil"), "GHS 371 · Above all")
        self.assertEqual(self.js.eval('songBooks(SONG_INDEX.get("b.foil"))'), "FJ 45")  # the others stay in the picker's side text
        self.assertEqual(self.js.eval('songBooks(SONG_INDEX.get("a.foil"))'), "")

    def test_another_book_when_there_is_no_ghs_number(self):
        self.assertEqual(self.label("d.foil"), "FJ 12 · Nur anderes Buch")

    def test_no_number_means_title_only(self):
        for song_id, title in (("c.foil", "Ohne Nummer"), ("g.foil", "Leere Nummer")):
            self.assertEqual(self.label(song_id), title, song_id)  # no dangling separator
        self.assertEqual(self.js.eval('songLabel({id: "x", title: "Unbekannt"})'), "Unbekannt")  # not in the library

    def test_live_song_without_books_takes_them_from_the_library(self):
        self.assertEqual(self.js.eval('songLabel({id: "a.foil", title: "Aus meines Herzens Grunde"})'),
                         "GHS 123 · Aus meines Herzens Grunde")

    def test_row_title_gets_the_number_only_when_it_is_the_songs_title(self):
        row = lambda title: json.dumps({"title": title, "song": "a.foil"})
        self.assertEqual(self.js.eval(f"programRowTitle({row('aus meines herzens grunde')})"), "GHS 123 · Aus meines Herzens Grunde")
        self.assertEqual(self.js.eval(f"programRowTitle({row('Lied 1')})"), "Lied 1")
        self.assertEqual(self.js.eval('programRowTitle({title: "Aus meines Herzens Grunde"})'), "Aus meines Herzens Grunde")

    def test_picker_finds_songs_by_number(self):
        self.assertEqual(self.found("123")[0], "a.foil")
        self.assertEqual(self.found("GHS 123"), ["a.foil"])
        self.assertEqual(self.found("ghs123"), ["a.foil"])
        self.assertEqual(self.found("fj 45"), ["b.foil"])
        self.assertEqual(self.found("Feiert Jesus 12"), ["d.foil"])

    def test_picker_lists_the_exact_number_before_longer_ones(self):
        exact = self.found("12")
        self.assertEqual(set(exact[:2]), {"d.foil", "e.foil"})
        self.assertLess(exact.index("e.foil"), exact.index("f.foil"))  # 12 before 112

    def test_picker_still_finds_by_title(self):
        self.assertEqual(self.found("herzens"), ["a.foil"])


class SongLabelWiringTest(unittest.TestCase):
    def test_places_that_show_a_song_use_the_label(self):
        dock = (JS / "song-dock.js").read_text(encoding="utf-8")
        templates = (JS / "templates.js").read_text(encoding="utf-8")
        self.assertIn("songLabel(known)", PROGRAM)  # the Liedtext step chip
        self.assertIn("songLabel(song)", function_body(PROGRAM, "programChips"))  # the row chip
        self.assertIn("programRowTitle(item)", function_body(PROGRAM, "renderProgramSummary"))
        self.assertIn("programRowTitle(next)", PROGRAM)  # the "Als Nächstes" cue
        self.assertIn("songLabel(known)", dock)
        self.assertIn("songLabel(song)", function_body(SONGS, "renderSongLive"))
        self.assertIn("songLabel(song)", function_body(SONGS, "syncSongPicker"))
        self.assertIn("songLabel(song)", SONGS[SONGS.index("function openSongSearch"):])
        self.assertIn("songLabel(song)", templates)


if __name__ == "__main__":
    unittest.main(verbosity=2)
