"""Focused tests for mpv audio playback in the desktop agent (mpv is faked;
the fade tests answer mpv's JSON IPC on a Unix socket)."""

import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import require_tk

# None without tkinter (tests skip); with REQUIRE_TK=1 (CI) that is an error.
tkinter = require_tk()

if tkinter is not None:
    # Pillow missing is a real error (requirements-dev.txt has it).
    from desktop_agent import show_image_agent as agent


class FakeProcess:
    pid = 1234

    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def wait(self, timeout=None):
        return self.returncode

    def kill(self):
        self.killed = True
        self.returncode = -9


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class AudioPlaybackTests(unittest.TestCase):
    def tearDown(self):
        agent._stop_audio()

    def test_play_starts_mpv_without_a_window(self):
        process = FakeProcess()
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "track.mp3"
            audio_path.touch()
            with patch.object(agent, "_find_mpv", return_value=r"C:\tools\mpv.exe"), \
                    patch.object(agent.subprocess, "Popen", return_value=process) as popen:
                agent._play_audio("track", str(audio_path))

        command = popen.call_args.args[0]
        options = popen.call_args.kwargs
        self.assertEqual(command[0], r"C:\tools\mpv.exe")
        self.assertTrue({
            "--no-config", "--no-video", "--vo=null", "--audio-display=no",
            "--force-window=no", "--no-terminal",
        }.issubset(command))
        self.assertIn("--really-quiet", command)
        self.assertEqual(command[-2:], ["--", str(audio_path)])
        self.assertFalse(any(arg.startswith(("--volume", "--input-ipc-server")) for arg in command))
        self.assertEqual(options["creationflags"], getattr(agent.subprocess, "CREATE_NO_WINDOW", 0))
        if agent.os.name == "nt":
            self.assertIsNotNone(options["startupinfo"])
            self.assertEqual(options["startupinfo"].wShowWindow, agent.subprocess.SW_HIDE)
            self.assertTrue(options["startupinfo"].dwFlags & agent.subprocess.STARTF_USESHOWWINDOW)
        else:
            self.assertIsNone(options["startupinfo"])
        self.assertIs(agent._playback.process, process)

    def test_player_that_ends_at_once_is_reported(self):
        ended = FakeProcess()
        ended.returncode = 1
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "track.mp3"
            audio_path.touch()
            with patch.object(agent, "_find_mpv", return_value="mpv"), \
                    patch.object(agent, "mpv_log_path", return_value=Path(directory) / "none.log"), \
                    patch.object(agent.subprocess, "Popen", return_value=ended) as popen:
                with self.assertRaises(agent.AudioStartError) as caught:
                    agent._play_audio("track", str(audio_path))
        self.assertIn("mpv ended at once (exit code 1)", str(caught.exception))
        self.assertIn(f"--log-file={Path(directory) / 'none.log'}", popen.call_args.args[0])
        self.assertIsNone(agent._playback)

    def test_handler_returns_the_reason(self):
        class FakeHandler:
            def __init__(self):
                self.wfile = io.BytesIO()
                self.status = None

            def send_response(self, status):
                self.status = status

            def send_header(self, *_args):
                pass

            def end_headers(self):
                pass

            def send_error(self, status, *_args):
                self.status = status

        handler = FakeHandler()
        error = agent.AudioStartError("audio player could not be started: mpv ended at once (exit code 2)")
        with patch.object(agent, "_play_audio", side_effect=error):
            agent.DisplayHandler._handle_audio(handler, {"id": "music", "action": "play", "path": "C:\\a.mp3"})
        self.assertEqual(handler.status, 503)
        self.assertEqual(json.loads(handler.wfile.getvalue())["reason"], str(error))

    def test_play_replaces_and_stop_terminates_only_owned_process(self):
        first = FakeProcess()
        second = FakeProcess()
        with tempfile.TemporaryDirectory() as directory:
            audio_path = Path(directory) / "track.wav"
            audio_path.touch()
            with patch.object(agent, "_find_mpv", return_value="mpv"), \
                    patch.object(agent.subprocess, "Popen", side_effect=[first, second]):
                agent._play_audio("first", str(audio_path))
                agent._play_audio("second", str(audio_path))
                self.assertTrue(first.terminated)
                self.assertIs(agent._playback.process, second)
                self.assertTrue(agent._stop_audio())
                self.assertTrue(second.terminated)
                self.assertIsNone(agent._playback)


class FakeMpv(FakeProcess):
    """Listens on the --input-ipc-server address like mpv and records every
    volume it is set to."""

    # A stream (YouTube) answers "playback-time" only after this many asks.
    loading_polls = 0

    def __init__(self, command):
        super().__init__()
        self.volumes = []
        self.command = command
        self.polls = 0
        address = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--input-ipc-server="))
        self.initial_volume = next(arg.split("=", 1)[1] for arg in command if arg.startswith("--volume="))
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(address)
        self.server.listen(1)
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        connection, _ = self.server.accept()
        with connection, connection.makefile("rwb", buffering=0) as stream:
            stream.write(b'{"event":"file-loaded"}\n')   # unrelated lines are skipped
            for line in iter(stream.readline, b""):
                request = json.loads(line)
                if request["command"][:2] == ["set_property", "volume"]:
                    self.volumes.append(request["command"][2])
                if request["command"] == ["get_property", "playback-time"]:
                    self.polls += 1
                    if self.polls <= self.loading_polls:
                        stream.write(json.dumps({"request_id": request["request_id"], "error": "property unavailable"}).encode() + b"\n")
                        continue
                    stream.write(json.dumps({"request_id": request["request_id"], "error": "success", "data": 0.25}).encode() + b"\n")
                    continue
                stream.write(json.dumps({"request_id": request["request_id"], "error": "success", "data": None}).encode() + b"\n")

    def terminate(self):
        super().terminate()
        self.server.close()


@unittest.skipIf(tkinter is None or os.name == "nt", "needs tkinter; the fake mpv uses a Unix socket")
class AudioFadeTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.audio_path = Path(self.directory.name) / "track.mp3"
        self.audio_path.touch()
        self.socket_dir = tempfile.mkdtemp(dir="/tmp")
        self.count = 0

        def address():
            self.count += 1
            return f"{self.socket_dir}/mpv{self.count}.sock"

        self.processes = []

        def popen(command, **_kwargs):
            process = FakeMpv(command)
            self.processes.append(process)
            return process

        self.patches = [
            patch.object(agent, "_find_mpv", return_value="mpv"),
            patch.object(agent, "_ipc_address", side_effect=address),
            patch.object(agent.subprocess, "Popen", side_effect=popen),
            patch.object(agent, "FADE_STEP_SECONDS", 0.01),
        ]
        for item in self.patches:
            item.start()

    def tearDown(self):
        agent._stop_audio()
        for item in self.patches:
            item.stop()
        self.directory.cleanup()

    def wait_for(self, condition, timeout=3.0):
        deadline = time.monotonic() + timeout
        while not condition():
            if time.monotonic() > deadline:
                self.fail("condition not reached")
            time.sleep(0.01)

    def test_db_to_mpv_volume(self):
        self.assertAlmostEqual(agent.db_to_mpv_volume(0), 100.0)
        self.assertAlmostEqual(agent.db_to_mpv_volume(-30), 31.6227766, places=5)
        self.assertAlmostEqual(agent.db_to_mpv_volume(-60), 10.0)

    def test_fade_in_then_fade_out_on_stop(self):
        fade = {"db": -30, "seconds": 0.2}
        agent._play_audio("music", str(self.audio_path), fade)
        mpv = self.processes[0]
        self.assertEqual(mpv.initial_volume, "0")
        self.wait_for(lambda: mpv.volumes and abs(mpv.volumes[-1] - agent.db_to_mpv_volume(-30)) < 1e-9)
        self.assertEqual(mpv.volumes, sorted(mpv.volumes))          # only rising
        self.assertGreaterEqual(len(mpv.volumes), 10)                # in small steps
        self.assertLess(max(mpv.volumes), agent.db_to_mpv_volume(-30) + 1e-9)

        before = len(mpv.volumes)
        self.assertTrue(agent._stop_audio(fade))                     # returns at once
        self.assertFalse(mpv.terminated)
        self.wait_for(lambda: mpv.terminated)
        down = mpv.volumes[before:]
        self.assertEqual(down, sorted(down, reverse=True))           # only falling
        self.assertAlmostEqual(down[-1], agent.db_to_mpv_volume(agent.FADE_FLOOR_DB))
        self.assertIsNone(agent._playback)

    def test_stop_during_fade_in_fades_out_from_current_level(self):
        fade = {"db": -10, "seconds": 2.0}
        agent._play_audio("music", str(self.audio_path), fade)
        mpv = self.processes[0]
        self.wait_for(lambda: len(mpv.volumes) >= 5)
        agent._stop_audio(fade)
        self.wait_for(lambda: mpv.terminated, timeout=5)
        self.assertLess(max(mpv.volumes), agent.db_to_mpv_volume(-10))

    def test_stop_without_fade_ends_at_once(self):
        agent._play_audio("music", str(self.audio_path), {"db": -30, "seconds": 0.2})
        mpv = self.processes[0]
        self.assertTrue(agent._stop_audio())
        self.assertTrue(mpv.terminated)

    def test_new_track_ends_one_still_fading_out(self):
        fade = {"db": -30, "seconds": 5.0}
        agent._play_audio("first", str(self.audio_path), fade)
        first = self.processes[0]
        self.wait_for(lambda: len(first.volumes) >= 3)
        agent._stop_audio(fade)
        agent._play_audio("second", str(self.audio_path), fade)
        self.assertTrue(first.terminated)
        self.assertEqual(agent._playback.profile_id, "second")
        self.assertEqual(agent._fading_out, set())

    def test_unreachable_ipc_does_not_leave_a_silent_player(self):
        with patch.object(agent, "IPC_CONNECT_SECONDS", 0.1), \
                patch.object(agent, "_ipc_address", return_value=f"{self.socket_dir}/missing.sock"), \
                patch.object(agent.subprocess, "Popen", return_value=FakeProcess()) as popen:
            with self.assertRaises(OSError):
                agent._play_audio("music", str(self.audio_path), {"db": -30, "seconds": 1})
        self.assertTrue(popen.return_value.terminated)
        self.assertIsNone(agent._playback)

    def test_start_failure_names_the_reason(self):
        """"audio player could not be started" alone did not say why."""
        log = Path(self.socket_dir) / "mpv-last.log"
        log.write_text(
            "[   0.010][v][cplayer] mpv 0.39.0\n"
            "[   0.420][e][ao/wasapi] Could not open audio device\n"
            "[   0.421][i][cplayer] Exiting... (Errors when loading file)\n", encoding="utf-8")
        ended = FakeProcess()
        ended.returncode = 2
        with patch.object(agent, "IPC_CONNECT_SECONDS", 0.1), patch.object(agent, "mpv_log_path", return_value=log), \
                patch.object(agent, "_ipc_address", return_value=f"{self.socket_dir}/missing.sock"), \
                patch.object(agent.subprocess, "Popen", return_value=ended):
            with self.assertRaises(agent.AudioStartError) as caught:
                agent._play_audio("music", str(self.audio_path), {"db": 0, "seconds": 1})
        self.assertEqual(str(caught.exception), "audio player could not be started: mpv ended at once "
                                                "(exit code 2): Could not open audio device")
        log.unlink()
        with patch.object(agent, "IPC_CONNECT_SECONDS", 0.1), patch.object(agent, "mpv_log_path", return_value=log), \
                patch.object(agent, "_ipc_address", return_value=f"{self.socket_dir}/missing.sock"), \
                patch.object(agent.subprocess, "Popen", return_value=FakeProcess()):
            with self.assertRaises(agent.AudioStartError) as caught:
                agent._play_audio("music", str(self.audio_path), {"db": 0, "seconds": 1})
        self.assertIn("did not open its control channel within 0.1 s", str(caught.exception))

    def test_cold_start_gets_time(self):
        # mpv on Windows often needs several seconds on a cold start.
        self.assertGreaterEqual(agent.IPC_CONNECT_SECONDS, 10)

    def test_youtube_fade_waits_for_the_first_sound(self):
        url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
        with patch.object(FakeMpv, "loading_polls", 3), patch.object(agent, "_find_ytdlp", return_value="/opt/yt-dlp"):
            agent._play_audio("yt", url, {"db": 0, "seconds": 0.2})
            mpv = self.processes[0]
            self.assertEqual(mpv.command[-2:], ["--", url])
            self.assertIn("--script-opts=ytdl_hook-ytdl_path=/opt/yt-dlp", mpv.command)
            self.wait_for(lambda: mpv.volumes and abs(mpv.volumes[-1] - 100.0) < 1e-9)
        # No volume step before the stream played (3 "unavailable" answers first).
        self.assertGreaterEqual(mpv.polls, 4)
        self.assertEqual(mpv.volumes, sorted(mpv.volumes))

    def test_youtube_stopped_while_loading_never_fades_in(self):
        url = "https://youtu.be/dQw4w9WgXcQ"
        with patch.object(FakeMpv, "loading_polls", 10_000), patch.object(agent, "_find_ytdlp", return_value="/opt/yt-dlp"):
            agent._play_audio("yt", url, {"db": 0, "seconds": 0.2})
            mpv = self.processes[0]
            self.wait_for(lambda: mpv.polls >= 2)
            self.assertTrue(agent._stop_audio())
        self.assertTrue(mpv.terminated)
        self.assertEqual(mpv.volumes, [])

    def test_fade_request_validation(self):
        self.assertTrue(agent._valid_fade({"db": -30, "seconds": 5}))
        self.assertTrue(agent._valid_fade({"db": -12.5, "seconds": 0.5}))
        for fade in ({"db": 3, "seconds": 5}, {"db": -61, "seconds": 5}, {"db": -30, "seconds": 0},
                     {"db": True, "seconds": 5}, {"db": "-30", "seconds": 5}, {"db": -30}, None):
            self.assertFalse(agent._valid_fade(fade), fade)


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class AudioHandlerTests(unittest.TestCase):
    class FakeHandler:
        def __init__(self):
            self.wfile = io.BytesIO()
            self.status = None
            self.errors = []

        def send_response(self, status):
            self.status = status

        def send_header(self, name, value):
            pass

        def end_headers(self):
            pass

        def send_error(self, status, *_args):
            self.errors.append(status)

    def handle(self, body):
        handler = self.FakeHandler()
        agent.DisplayHandler._handle_audio(handler, body)
        return handler

    def test_fade_is_passed_through(self):
        fade = {"db": -30, "seconds": 5}
        with patch.object(agent, "_play_audio") as play, patch.object(agent, "_stop_audio", return_value=True) as stop:
            self.assertEqual(self.handle({"id": "music", "action": "play", "path": r"C:\a.mp3", "fade": fade}).status, 200)
            self.assertEqual(self.handle({"id": "music", "action": "stop", "path": None, "fade": fade}).status, 200)
            self.assertEqual(self.handle({"id": "music", "action": "play", "path": r"C:\a.mp3"}).status, 200)
            self.assertEqual(self.handle({"id": "music", "action": "stop", "path": None}).status, 200)
        self.assertEqual([c.args for c in play.call_args_list], [("music", r"C:\a.mp3", fade), ("music", r"C:\a.mp3", None)])
        self.assertEqual([c.args for c in stop.call_args_list], [(fade,), (None,)])

    def test_invalid_fade_or_extra_keys_are_rejected(self):
        with patch.object(agent, "_play_audio") as play:
            for body in (
                {"id": "music", "action": "play", "path": r"C:\a.mp3", "fade": {"db": 6, "seconds": 5}},
                {"id": "music", "action": "play", "path": r"C:\a.mp3", "volume": 50},
            ):
                self.assertEqual(self.handle(body).errors, [400])
        play.assert_not_called()


if __name__ == "__main__":
    unittest.main()

@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class YoutubeAudioTests(unittest.TestCase):
    def tearDown(self):
        agent._stop_audio()

    def test_youtube_links_recognised_and_others_not(self):
        for url in ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "https://youtu.be/dQw4w9WgXcQ?t=42",
                    "https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RD1", "https://m.youtube.com/shorts/abcdefghij",
                    "https://www.youtube.com/watch?list=PL1&v=dQw4w9WgXcQ", "https://www.youtube.com/live/abcdefghij"):
            self.assertTrue(agent.is_youtube_url(url), url)
        for value in ("https://evil.example/youtube.com/watch?v=dQw4w9WgXcQ", "https://www.youtube.com.evil.example/watch?v=dQw4w9WgXcQ",
                      "https://youtube.com@evil.example/watch?v=dQw4w9WgXcQ", "https://www.youtube.com/@kanal",
                      "https://www.youtube.com/watch?v=dQw4 w9WgXcQ", "C:\\Musik\\a.mp3", "ftp://youtu.be/dQw4w9WgXcQ",
                      "https://youtu.be/" + "a" * 600, None):
            self.assertFalse(agent.is_youtube_url(value), value)

    def test_youtube_link_plays_sound_only_through_yt_dlp(self):
        process = FakeProcess()
        url = "https://youtu.be/dQw4w9WgXcQ"
        with patch.object(agent, "_find_mpv", return_value=r"C:\tools\mpv.exe"), \
                patch.object(agent, "_find_ytdlp", return_value=r"C:\tools\yt-dlp.exe") as find, \
                patch.object(agent.subprocess, "Popen", return_value=process) as popen:
            agent._play_audio("yt", url)
        command = popen.call_args.args[0]
        self.assertEqual(command[-2:], ["--", url])
        self.assertTrue({"--no-video", "--ytdl=yes", "--ytdl-format=bestaudio/best", "--ytdl-raw-options=no-playlist=",
                         r"--script-opts=ytdl_hook-ytdl_path=C:\tools\yt-dlp.exe"}.issubset(command))
        find.assert_called_once_with(r"C:\tools\mpv.exe")
        self.assertTrue(agent._playback.stream)

    def test_without_yt_dlp_a_clear_error_and_nothing_started(self):
        with patch.object(agent, "_find_mpv", return_value="mpv"), \
                patch.object(agent, "_find_ytdlp", return_value=None), \
                patch.object(agent.subprocess, "Popen") as popen:
            with self.assertRaisesRegex(FileNotFoundError, "yt-dlp"):
                agent._play_audio("yt", "https://youtu.be/dQw4w9WgXcQ")
        popen.assert_not_called()

    def test_other_links_are_not_streamed(self):
        with patch.object(agent, "_find_mpv", return_value="mpv"), patch.object(agent.subprocess, "Popen") as popen:
            with self.assertRaises(ValueError):
                agent._play_audio("x", "https://example.com/song.mp3")
        popen.assert_not_called()

    def test_yt_dlp_found_next_to_mpv(self):
        with tempfile.TemporaryDirectory() as directory:
            mpv = Path(directory) / "mpv.exe"
            ytdlp = Path(directory) / "yt-dlp.exe"
            mpv.touch()
            ytdlp.touch()
            with patch.dict(os.environ, {"YTDLP_PATH": ""}), patch.object(agent.shutil, "which", return_value=None):
                self.assertEqual(agent._find_ytdlp(str(mpv)), str(ytdlp))
                with patch.dict(os.environ, {"LOCALAPPDATA": directory + "/none", "ProgramFiles": directory + "/none"}), \
                        patch.object(agent, "__file__", str(Path(directory) / "sub" / "agent.py")):
                    self.assertIsNone(agent._find_ytdlp(None))
