"""Focused tests for mpv audio playback in the desktop agent."""

import tempfile
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
        self.assertEqual(options["creationflags"], getattr(agent.subprocess, "CREATE_NO_WINDOW", 0))
        self.assertIs(agent._audio_process, process)

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
                self.assertIs(agent._audio_process, second)
                self.assertTrue(agent._stop_audio())
                self.assertTrue(second.terminated)
                self.assertIsNone(agent._audio_process)


if __name__ == "__main__":
    unittest.main()