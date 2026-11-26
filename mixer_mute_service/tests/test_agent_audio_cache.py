"""Local copies of audio sources in the desktop agent: the music lives on a
Google Drive drive that fetches a file only when it is opened, so mpv waited
about a minute before the first sound. The agent now plays a local copy,
made in the background (sources sent with /info, sources known at agent
start, and after a play that found none). mpv and yt-dlp are faked; nothing
leaves this machine."""

import io
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import require_tk

tkinter = require_tk()

if tkinter is not None:
    from desktop_agent import show_image_agent as agent

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class FakeProcess:
    pid = 4321

    def __init__(self):
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self):
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.returncode = -9


def wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def wait_idle():
    """Until no background copy is running."""
    assert wait_until(lambda: not agent._audio_cache_busy and not agent._prefetching_sources.is_set())


class CacheTestCase(unittest.TestCase):
    def setUp(self):
        self._dirs = tempfile.TemporaryDirectory()
        base = Path(self._dirs.name)
        self.cache = base / "cache"
        self.music = base / "I" / "GoogleDrive"
        self.music.mkdir(parents=True)
        self.source = self.music / "Hintergrundmusik.mp3"
        self.source.write_bytes(b"ID3" + os.urandom(300_000))
        patches = [
            patch.object(agent, "audio_cache_directory", return_value=self.cache),
            patch.object(agent, "_find_mpv", return_value="/fake/mpv"),
            patch.object(agent, "START_CHECK_SECONDS", 0.0),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def tearDown(self):
        agent._stop_audio()
        wait_idle()
        self._dirs.cleanup()

    def play(self, path, fade=None):
        """Start a play; returns (what mpv was given, the full command)."""
        with patch.object(agent.subprocess, "Popen", return_value=FakeProcess()) as popen:
            agent._play_audio("music", str(path), fade)
        command = popen.call_args.args[0]
        return command[-1], command


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class FileCopyTests(CacheTestCase):
    def test_first_play_uses_the_source_and_makes_a_copy_the_next_play_uses(self):
        played, _ = self.play(self.source)
        self.assertEqual(played, str(self.source), "no copy yet: the source plays at once")
        wait_idle()
        copy = agent.cached_audio(self.source)
        self.assertIsNotNone(copy)
        self.assertEqual(copy.read_bytes(), self.source.read_bytes())
        self.assertTrue(str(copy).startswith(str(self.cache)))

        played, _ = self.play(self.source)
        self.assertEqual(played, str(copy), "the second play is the local copy")

    def test_a_changed_source_never_plays_the_old_copy(self):
        agent._prefetch_now(self.source)
        old = agent.cached_audio(self.source)
        self.source.write_bytes(b"ID3" + os.urandom(200_000))
        self.assertIsNone(agent.cached_audio(self.source))
        played, _ = self.play(self.source)
        self.assertEqual(played, str(self.source))
        wait_idle()
        new = agent.cached_audio(self.source)
        self.assertNotEqual(new, old)
        self.assertEqual(new.read_bytes(), self.source.read_bytes())
        self.assertFalse(old.exists(), "the outdated copy is removed")

    def test_fade_is_kept_for_the_copy(self):
        agent._prefetch_now(self.source)
        played, command = self.play(self.source, {"db": 0, "seconds": 5})
        self.assertEqual(played, str(agent.cached_audio(self.source)))
        self.assertIn("--af=lavfi=[afade=t=in:d=5]", command)

    def test_play_returns_at_once_while_a_slow_drive_is_copied(self):
        # A Drive file that takes long to read must not delay the play: the
        # copy runs in the background.
        release = threading.Event()
        real_copy = agent._copy_file_to_cache

        def slow_copy(target):
            release.wait(5)
            return real_copy(target)

        with patch.object(agent, "_copy_file_to_cache", side_effect=slow_copy):
            started = time.monotonic()
            played, _ = self.play(self.source)
            elapsed = time.monotonic() - started
            release.set()
            wait_idle()
        self.assertEqual(played, str(self.source))
        self.assertLess(elapsed, 0.5)

    def test_missing_source_is_still_reported(self):
        with self.assertRaises(ValueError):
            self.play(self.music / "fehlt.mp3")

    def test_known_sources_are_refreshed_at_agent_start(self):
        agent._prefetch_now(self.source)
        self.source.write_bytes(b"ID3 neu" + os.urandom(1000))
        (self.cache / "leftover.mp3.part").write_bytes(b"x")
        agent.refresh_audio_cache()
        self.assertEqual(agent.cached_audio(self.source).read_bytes(), self.source.read_bytes())
        self.assertFalse((self.cache / "leftover.mp3.part").exists())

    def test_cache_keeps_to_its_size_dropping_the_least_recently_played(self):
        other = self.music / "Andere.mp3"
        other.write_bytes(os.urandom(300_000))
        agent._prefetch_now(self.source)
        first = agent.cached_audio(self.source)
        os.utime(first, (time.time() - 100, time.time() - 100))
        with patch.object(agent, "AUDIO_CACHE_MAX_BYTES", 400_000):
            agent._prefetch_now(other)
        self.assertFalse(first.exists())
        self.assertIsNotNone(agent.cached_audio(other))

    def test_too_large_files_play_from_the_source(self):
        with patch.object(agent, "AUDIO_CACHE_MAX_FILE_BYTES", 1000):
            agent._prefetch_now(self.source)
        self.assertIsNone(agent.cached_audio(self.source))


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class YoutubeCopyTests(CacheTestCase):
    def fake_ytdlp(self, command, **_kwargs):
        output = command[command.index("-o") + 1]
        Path(output.replace("%(ext)s", "webm")).write_bytes(b"opus audio")
        self.ytdlp_calls.append(command)
        return agent.subprocess.CompletedProcess(command, 0, b"", b"")

    def setUp(self):
        super().setUp()
        self.ytdlp_calls = []
        item = patch.object(agent, "_find_ytdlp", return_value="/fake/yt-dlp")
        item.start()
        self.addCleanup(item.stop)

    def test_link_is_downloaded_once_and_then_played_locally(self):
        with patch.object(agent.subprocess, "run", side_effect=self.fake_ytdlp):
            played, command = self.play(URL)
            self.assertEqual(played, URL, "no copy yet: mpv streams the link")
            self.assertIn("--ytdl=yes", command)
            wait_idle()
        self.assertEqual(len(self.ytdlp_calls), 1)
        self.assertIn("--no-playlist", self.ytdlp_calls[0])
        self.assertEqual(self.ytdlp_calls[0][-2:], ["--", URL])
        copy = agent.cached_audio(URL)
        self.assertEqual(copy.read_bytes(), b"opus audio")

        played, command = self.play(URL)
        self.assertEqual(played, str(copy))
        self.assertFalse(any(arg.startswith("--ytdl") for arg in command), "no yt-dlp on the way")

    def test_failed_download_leaves_nothing_and_keeps_streaming(self):
        failed = agent.subprocess.CompletedProcess([], 1, b"", b"ERROR: Video unavailable")
        with patch.object(agent.subprocess, "run", return_value=failed):
            agent._prefetch_now(URL)
            self.assertIsNone(agent.cached_audio(URL))
            self.assertEqual(list(self.cache.glob("*download*")), [])
            played, _ = self.play(URL)
            wait_idle()
        self.assertEqual(played, URL)


MPV_LOG = """\
[   0.002][v][cplayer] mpv v0.39.0 Copyright (C) 2000-2024 mpv/MPlayer/mplayer2 projects
[   0.010][v][cplayer] Running command line: mpv --no-config ...
[   0.031][v][file] Opening I:\\GoogleDrive\\Musik\\Hintergrundmusik.mp3
[  58.412][v][demux] Trying demuxers for level=normal.
[  58.430][v][cplayer] Opening done: I:\\GoogleDrive\\Musik\\Hintergrundmusik.mp3
[  58.511][i][cplayer] AO: [wasapi] 48000Hz stereo 2ch float
[  58.520][v][cplayer] starting audio playback
not a log line
[  60.000][v][cplayer] EOF code: 5
"""


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class MpvStartReportTests(unittest.TestCase):
    def test_report_names_the_stall_and_the_milestones(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "mpv-last.log"
            log.write_text(MPV_LOG, encoding="utf-8")
            report = agent.mpv_start_report(log)
        self.assertEqual((report["opened"], report["audio_out"], report["playing"]), (58.43, 58.511, 58.52))
        self.assertEqual(len(report["gaps"]), 2)
        stall = report["gaps"][0]
        self.assertEqual(stall["seconds"], 58.38)
        self.assertIn("Opening I:", stall["before"])
        self.assertIn("Trying demuxers", stall["after"])
        self.assertIn("written_at", report)

    def test_no_log_no_report(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertIsNone(agent.mpv_start_report(Path(directory) / "none.log"))


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class InfoPrefetchTests(CacheTestCase):
    def info(self, body):
        raw = json.dumps(body).encode()

        class Handler:
            path = "/info"
            headers = {"X-Display-Token": "t", "Content-Length": str(len(raw))}
            rfile = io.BytesIO(raw)
            wfile = io.BytesIO()
            status = None

            def send_response(self, status):
                self.status = status

            def send_header(self, *_args):
                pass

            def end_headers(self):
                pass

            def send_error(self, status, *_args):
                self.status = status

            def _send_json(self, status, result):
                agent.DisplayHandler._send_json(self, status, result)

        handler = Handler()
        with patch.dict(os.environ, {"DESKTOP_AGENT_TOKEN": "t"}):
            agent.DisplayHandler.do_POST(handler)
        return handler.status, json.loads(handler.wfile.getvalue())

    def test_sources_sent_with_info_are_copied_in_the_background(self):
        release = threading.Event()
        real_copy = agent._copy_file_to_cache

        def slow_copy(target):
            release.wait(5)
            return real_copy(target)

        with patch.object(agent, "_copy_file_to_cache", side_effect=slow_copy):
            started = time.monotonic()
            status, answer = self.info({"audio": [str(self.source), str(self.music / "fehlt.mp3"), 7]})
            elapsed = time.monotonic() - started
            release.set()
            wait_idle()
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertLess(elapsed, 0.5, "/info never waits for a copy")
        self.assertIsNotNone(agent.cached_audio(self.source))
        played, _ = self.play(self.source)
        self.assertEqual(played, str(agent.cached_audio(self.source)))

    def test_info_without_sources_still_answers(self):
        status, answer = self.info({})
        self.assertEqual((status, answer["ok"]), (200, True))
        self.assertFalse(self.cache.exists())


if __name__ == "__main__":
    unittest.main()
