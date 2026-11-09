"""The desktop agent's Windows "open file" dialog (POST /pick-file).

PowerShell is faked: the tests check the command (no start folder in its
text, only in the environment), the answers for picked / cancelled / failed /
timed out, and that a second dialog is refused while one is open."""

import io
import json
import os
import subprocess
import threading
import unittest
from unittest.mock import patch

from tests.helpers import require_tk

# None without tkinter (tests skip); with REQUIRE_TK=1 (CI) that is an error.
tkinter = require_tk()

if tkinter is not None:
    from desktop_agent import show_image_agent as agent


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

    def send_error(self, status, message=None):
        self.errors.append(status)

    def _send_json(self, status, result):
        agent.DisplayHandler._send_json(self, status, result)

    def answer(self):
        return json.loads(self.wfile.getvalue())


def handle(body):
    handler = FakeHandler()
    agent.DisplayHandler._handle_pick_file(handler, body)
    return handler


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class FileDialogTests(unittest.TestCase):
    def test_powershell_gets_the_start_folder_only_through_the_environment(self):
        start = "C:\\Musik'; Remove-Item C:\\ -Recurse; '"
        done = subprocess.CompletedProcess([], 0, stdout="\ufeffC:\\Musik\\Intro.mp3\r\n".encode("utf-8"), stderr=b"")
        with patch.object(agent.subprocess, "run", return_value=done) as run:
            self.assertEqual(agent._pick_file(start), "C:\\Musik\\Intro.mp3")
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["powershell.exe", "-NoProfile"])
        self.assertIn("-STA", command)
        self.assertIn("-EncodedCommand", command)
        self.assertNotIn(start, " ".join(command))
        self.assertEqual(run.call_args.kwargs["env"]["MIXER_PICK_START"], start)
        self.assertEqual(run.call_args.kwargs["timeout"], agent.PICK_TIMEOUT_SECONDS)
        self.assertNotIn("shell", run.call_args.kwargs)
        script = __import__("base64").b64decode(command[command.index("-EncodedCommand") + 1]).decode("utf-16-le")
        self.assertIn("OpenFileDialog", script)
        self.assertIn("$env:MIXER_PICK_START", script)

    def test_cancel_failure_and_timeout(self):
        cancelled = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with patch.object(agent.subprocess, "run", return_value=cancelled):
            self.assertIsNone(agent._pick_file(""))
        failed = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"Add-Type failed")
        with patch.object(agent.subprocess, "run", return_value=failed), self.assertRaises(OSError):
            agent._pick_file("")
        with patch.object(agent.subprocess, "run", side_effect=subprocess.TimeoutExpired("powershell.exe", 600)), \
                self.assertRaises(TimeoutError):
            agent._pick_file("")

    def test_handler_answers(self):
        with patch.object(agent.os, "name", "nt"):
            with patch.object(agent, "_pick_file", return_value="C:\\Musik\\Intro.mp3") as pick:
                handler = handle({"start": "C:\\Musik"})
            self.assertEqual((handler.status, handler.answer()), (200, {"ok": True, "path": "C:\\Musik\\Intro.mp3"}))
            pick.assert_called_once_with("C:\\Musik")
            with patch.object(agent, "_pick_file", return_value=None):
                self.assertEqual(handle({"start": ""}).answer(), {"ok": True, "path": None})
            with patch.object(agent, "_pick_file", side_effect=OSError("no desktop")):
                self.assertEqual(handle({"start": ""}).status, 503)
            with patch.object(agent, "_pick_file", side_effect=TimeoutError()):
                self.assertEqual(handle({"start": ""}).status, 408)
            for bad in ({}, {"start": 1}, {"start": "C:\\a\nb"}, {"start": "x" * 401}, {"start": "", "x": 1}):
                self.assertEqual(handle(bad).errors, [400], bad)
        self.assertFalse(agent._pick_lock.locked(), "the lock is released after every answer")

    def test_only_one_dialog_at_a_time(self):
        opened, release = threading.Event(), threading.Event()

        def slow_pick(_start):
            opened.set()
            release.wait(5)
            return "C:\\a.mp3"

        with patch.object(agent.os, "name", "nt"), patch.object(agent, "_pick_file", side_effect=slow_pick):
            first = threading.Thread(target=handle, args=({"start": ""},))
            first.start()
            self.assertTrue(opened.wait(5))
            second = handle({"start": ""})
            release.set()
            first.join(5)
        self.assertEqual(second.status, 409)
        self.assertFalse(agent._pick_lock.locked())

    def test_not_windows(self):
        with patch.object(agent.os, "name", "posix"):
            self.assertEqual(handle({"start": ""}).status, 501)


if __name__ == "__main__":
    unittest.main()
