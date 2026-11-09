"""Song parts of the desktop agent: reading the library and checking /song requests.

Drawing needs a screen and Windows, so only the pure parts are tested here.
"""

import tempfile
import unittest
from pathlib import Path

try:
    import tkinter  # noqa: F401
except ImportError:  # some Python builds (e.g. Homebrew) ship without Tk
    tkinter = None

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


if __name__ == "__main__":
    unittest.main()
