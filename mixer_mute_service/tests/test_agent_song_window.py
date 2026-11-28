"""The display agent's song window does not flicker between slides.

Every slide used to hide the window (withdraw), set its frame and topmost
again, show it and place it with SWP_FRAMECHANGED: for a moment the app
behind it showed. Now a slide change only redraws the canvas (the new items
first, then the old ones go) while the window stays where and as it is;
it is hidden and placed again only when the monitor, full screen or size
change, or after it was hidden. A full-screen window is checked against its
real place on every slide (Windows moves it away when the beamer drops its
signal: placed again) and made topmost again without moving or redrawing. The same for images that follow one another
(and their windows on further monitors). Checked with the real Tk window and
spies around the Tk and Windows calls; skips without tkinter or a display.
"""

import ctypes
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from tests.helpers import require_tk

# None without tkinter (tests skip); with REQUIRE_TK=1 (CI) that is an error.
tkinter = require_tk()

if tkinter is not None:
    from PIL import Image

    from desktop_agent import show_image_agent as agent

STYLE = {"font": "Verdana", "font_size": 72, "text_color": "#ffffff", "background_color": "#000000"}
SLIDE = {"action": "show", "monitor": 1, "fullscreen": True, "style": STYLE,
         "text": "Erste Zeile\nZweite Zeile", "title": "Titel", "copyright": "(c) Test"}
WINDOW_CALLS = ("withdraw", "overrideredirect", "attributes", "geometry", "deiconify", "update_idletasks", "lift")


def song_window(monitor, fullscreen):
    if not fullscreen:
        return 640, 360, 100 + 1000 * (monitor - 1), 100
    return (800, 600, 1000 * (monitor - 1), 0)


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class SongWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root, self.label, self.canvas = agent.create_root()
        except tkinter.TclError as exc:
            self.skipTest(f"no display: {exc}")
        self.addCleanup(self.root.destroy)
        agent._window_style.clear()
        agent._close_mirrors()
        patcher = patch.object(agent, "_song_place", None)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.calls = []
        for name in WINDOW_CALLS:
            original = getattr(self.root, name)
            setattr(self.root, name, self._spy(name, original))
        # Windows: placing the window puts it there; GetWindowRect reports where it is.
        self.actual = None
        self.placed = Mock(side_effect=self._place)
        self.windll = Mock()
        self.windll.user32.GetParent.return_value = 0
        self.windll.user32.GetWindowRect.side_effect = self._window_rect
        for target, value in (("_song_window", song_window), ("_place_song_window", self.placed)):
            patcher = patch.object(agent, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(ctypes, "windll", self.windll, create=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _place(self, _root, x, y, width, height, _restyled):
        self.actual = (x, y, x + width, y + height)

    def _window_rect(self, _hwnd, pointer):
        if self.actual is None:
            return 0
        rect = pointer._obj
        rect.left, rect.top, rect.right, rect.bottom = self.actual
        return 1

    def topmost_calls(self):
        return [c.args[1:] for c in self.windll.user32.SetWindowPos.call_args_list]

    def _spy(self, name, original):
        def spy(*args, **kwargs):
            self.calls.append(name)
            return original(*args, **kwargs)
        return spy

    def show(self, **changes):
        agent._show_song(self.root, self.canvas, {**SLIDE, **changes})

    def texts(self):
        return [self.canvas.itemcget(item, "text") for item in self.canvas.find_all()]

    def test_next_slides_leave_the_window_alone(self):
        self.show()
        self.assertIn("withdraw", self.calls)               # the first slide sets the window up
        self.placed.assert_called_once_with(self.root, 0, 0, 800, 600, True)
        self.assertEqual(self.root.state(), "normal")
        self.calls.clear()
        self.placed.reset_mock()
        self.show(text="Dritte Zeile")
        self.show(text="Vierte Zeile")
        self.assertEqual(self.calls, [])                    # no withdraw, frame, topmost, geometry, update, raise
        self.placed.assert_not_called()                     # not placed again, no SWP_FRAMECHANGED
        # Only made topmost again: HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE.
        self.assertEqual(self.topmost_calls(), [(-1, 0, 0, 0, 0, 0x0013)] * 2)
        self.assertIn("Vierte Zeile", self.texts())
        self.assertNotIn("Dritte Zeile", self.texts())
        self.assertEqual(self.root.state(), "normal")

    def test_the_new_slide_exists_before_the_old_one_is_deleted(self):
        self.show(text="Alt")
        old = set(self.canvas.find_all())
        deleted = []
        original = self.canvas.delete

        def delete(*items):
            deleted.append((items, set(self.canvas.find_all())))
            return original(*items)

        self.canvas.delete = delete
        self.show(text="Neu")
        self.assertEqual(len(deleted), 1)
        items, present = deleted[0]
        self.assertNotIn("all", items)
        self.assertEqual(set(items), old)                   # only the old items go
        self.assertTrue(present - old)                      # and the new ones were already there
        self.assertIn("Neu", self.texts())

    def test_black_is_seamless_too_and_hide_still_hides(self):
        self.show()
        self.calls.clear()
        self.show(action="blank", text="")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.canvas.find_all(), ())        # black: nothing on the canvas
        self.show(action="hide")
        self.assertEqual(self.root.state(), "withdrawn")
        self.calls.clear()
        self.show(text="Wieder da")                         # after hiding: set up again
        self.assertIn("deiconify", self.calls)
        self.assertEqual(self.root.state(), "normal")

    def test_the_window_is_placed_again_only_when_its_place_changes(self):
        self.show()
        self.calls.clear()
        self.placed.reset_mock()
        self.show(monitor=2)                                # another monitor: placed, the frame stays
        self.placed.assert_called_once_with(self.root, 1000, 0, 800, 600, False)
        self.assertNotIn("withdraw", self.calls)
        self.calls.clear()
        self.show(monitor=2, fullscreen=False)              # framed: the frame changes, so hidden for that
        self.assertIn("withdraw", self.calls)
        self.assertIn("overrideredirect", self.calls)
        self.calls.clear()
        self.show(monitor=2, fullscreen=False, text="Weiter")
        self.assertEqual(self.calls, ["lift"])              # framed: only raised, never hidden or moved

    def test_a_window_windows_moved_away_is_placed_again(self):
        self.show(monitor=2)
        self.placed.reset_mock()
        self.actual = (0, 0, 800, 600)                      # the beamer dropped out: moved to the main monitor
        self.calls.clear()
        self.show(monitor=2, text="Weiter")
        self.placed.assert_called_once_with(self.root, 1000, 0, 800, 600, False)
        self.assertNotIn("withdraw", self.calls)            # placed, not hidden
        self.assertIn("Weiter", self.texts())
        self.placed.reset_mock()
        self.show(monitor=2, text="Danach")                 # back in place: the quick way again
        self.placed.assert_not_called()

    def test_an_unknown_window_place_is_placed_again(self):
        self.show()
        self.placed.reset_mock()
        self.windll.user32.GetWindowRect.side_effect = lambda *_args: 0
        self.show(text="Weiter")
        self.placed.assert_called_once()

    def test_a_framed_window_is_not_checked_or_made_topmost(self):
        self.show(fullscreen=False)
        self.windll.reset_mock()
        self.show(fullscreen=False, text="Weiter")
        self.windll.user32.GetWindowRect.assert_not_called()
        self.windll.user32.SetWindowPos.assert_not_called()

    def test_an_image_in_between_sets_the_song_window_up_again(self):
        self.show()
        self.root.pack_slaves()[0].pack_forget()
        self.label.pack(fill="both", expand=True)           # the window showed an image meanwhile
        self.calls.clear()
        self.show(text="Danach")
        self.assertEqual(self.root.pack_slaves(), [self.canvas])
        self.assertIn("geometry", self.calls)
        self.assertNotIn("withdraw", self.calls)            # same frame: still not hidden


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class ImageWindowTests(unittest.TestCase):
    def setUp(self):
        try:
            self.root, self.label, _canvas = agent.create_root()
        except tkinter.TclError as exc:
            self.skipTest(f"no display: {exc}")
        self.addCleanup(self.root.destroy)
        agent._window_style.clear()
        self.addCleanup(agent._close_mirrors)
        folder = Path(tempfile.mkdtemp())
        self.images = []
        for index, colour in enumerate(("red", "blue")):
            path = folder / f"bild{index}.png"
            Image.new("RGB", (64, 36), colour).save(path)
            self.images.append(str(path))
        self.windll = Mock()
        self.windll.user32.SetWindowPos.return_value = 1
        for patcher in (patch.object(ctypes, "windll", self.windll, create=True),
                        patch.object(agent, "monitor_bounds", side_effect=lambda m: (1000 * (m - 1), 0, 1000 * m, 600))):
            patcher.start()
            self.addCleanup(patcher.stop)

    def profile(self, index, **changes):
        return {"id": f"bild{index}", "kind": "image", "path": self.images[index], "monitor": 1,
                "fullscreen": True, "position": "center", **changes}

    def test_the_next_image_does_not_hide_the_window(self):
        agent._show_image(self.root, self.label, self.profile(0, monitors=[1, 2]))
        mirror = agent._mirrors[0]
        flags = [c.args[6] for c in self.windll.user32.SetWindowPos.call_args_list]
        self.assertEqual(flags, [0x0060, 0x0060])           # first time: frame taken away
        self.windll.user32.SetWindowPos.reset_mock()
        withdrawn = []
        for window in (self.root, mirror):
            original = window.withdraw
            window.withdraw = lambda original=original, window=window: (withdrawn.append(window), original())
        agent._show_image(self.root, self.label, self.profile(1, monitors=[1, 2]))
        self.assertEqual(withdrawn, [])                     # neither window disappears
        self.assertIs(agent._mirrors[0], mirror)            # the further monitor keeps its window
        flags = [c.args[6] for c in self.windll.user32.SetWindowPos.call_args_list]
        self.assertEqual(flags, [0x0040, 0x0040])           # no SWP_FRAMECHANGED
        self.assertEqual(self.root.state(), "normal")


if __name__ == "__main__":
    unittest.main()
