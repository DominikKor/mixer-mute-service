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

    def __init__(self, command):
        super().__init__()
        self.volumes = []
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