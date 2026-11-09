"""Song stepping under load: two quick "next" presses step two slides, and a
big library is parsed outside the event loop."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app import songs
from app.songs import SongService


def fake_request(service):
    cfg = SimpleNamespace(desktop_display=SimpleNamespace(enabled=True, token_env="DESKTOP_AGENT_TOKEN"))
    return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cfg=cfg, songs=service)))


class SongNextTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_quick_nexts_step_two_slides(self):
        service = SongService(Path(tempfile.mkdtemp()))
        service.songs = {"a.foil": {"title": "A", "copyright": "", "books": [],
                                    "slides": [{"key": str(i), "text": f"Zeile {i}"} for i in range(1, 6)]}}
        service.live = {"song_id": "a.foil", "slide": 0, "blank": False}
        shown = []

        async def slow_agent(cfg, ip, path, payload, timeout=3.0):
            await asyncio.sleep(0.02)  # the display PC takes a moment
            shown.append(payload)
            return {"ok": True}

        request = fake_request(service)
        with patch("app.display_agent.call", slow_agent):
            await asyncio.gather(songs.song_next(request), songs.song_next(request))
        self.assertEqual(service.live["slide"], 2)
        self.assertEqual(len(shown), 2)


class SongLiveTests(unittest.IsolatedAsyncioTestCase):
    async def test_song_left_out_when_the_page_has_it(self):
        service = SongService(Path(tempfile.mkdtemp()))
        service.songs = {"a.foil": {"title": "A", "copyright": "", "books": [],
                                    "slides": [{"key": "1", "text": "Zeile"}]}}
        service.live = {"song_id": "a.foil", "slide": 0, "blank": False}
        request = fake_request(service)
        full = await songs.song_live(request)
        self.assertEqual(full["song"]["title"], "A")
        slim = await songs.song_live(request, have="a.foil")
        self.assertIsNone(slim["song"])
        self.assertTrue(slim["song_unchanged"])
        other = await songs.song_live(request, have="b.foil")
        self.assertEqual(other["song"]["title"], "A")


class LibraryParseTests(unittest.IsolatedAsyncioTestCase):
    async def test_library_is_parsed_in_a_worker_thread(self):
        service = SongService(Path(tempfile.mkdtemp()))
        threads = []

        def parse(files):
            import threading
            threads.append(threading.current_thread() is threading.main_thread())
            return {}, []

        async def agent(cfg, ip, path, payload, timeout=3.0):
            return {"ok": True, "files": []}

        service.settings = service.settings.model_copy(update={"library_path": "C:\\\\Lieder"})
        with patch("app.display_agent.call", agent), patch.object(SongService, "parse_library", staticmethod(parse)):
            result = await songs.reload_song_library(fake_request(service))
        self.assertEqual(threads, [False])
        self.assertTrue(result["ok"])


if __name__ == "__main__":
    unittest.main()
