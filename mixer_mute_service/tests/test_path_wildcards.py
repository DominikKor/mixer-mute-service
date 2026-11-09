""""*" in a path stands for an unknown part of a name. The display agent fills
it in on the target PC (the newest match wins; programs only match .exe), and
the service accepts such paths without ever allowing a program on a share."""

import json
import os
import queue
import socket
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests.helpers import require_tk

from app.actions import network_program
from app.config import DesktopProfileConfig
from app.desktop_profiles import ProfileEditError, validate_profile
from app.file_actions import file_action_error

# None without tkinter (agent tests skip); with REQUIRE_TK=1 (CI) that is an error.
tkinter = require_tk()

if tkinter is not None:
    from desktop_agent import show_image_agent as agent


def touch(path: Path, age: float = 0.0) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("x")
    stamp = time.time() - age
    os.utime(path, (stamp, stamp))
    return path


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class AgentWildcardTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def test_runnable_file_from_a_share_is_never_started(self):
        self.assertTrue(agent.runs_from_network_share(Path("\\\\server\\share\\abc.exe")))
        self.assertTrue(agent.runs_from_network_share(Path("//server/share/run.BAT")))
        self.assertFalse(agent.runs_from_network_share(Path("\\\\server\\share\\folie.pdf")))
        self.assertFalse(agent.runs_from_network_share(Path("C:\\Tools\\run.exe")))

    def test_resolve(self):
        old = touch(self.root / "Musik" / "Ansage_2026-11-01.mp3", age=3600)
        new = touch(self.root / "Musik" / "Ansage_2026-11-09.mp3")
        touch(self.root / "Musik" / "Intro.mp3")
        plain = str(self.root / "Musik" / "Intro.mp3")
        self.assertEqual(agent.resolve_path(plain), Path(plain))  # no "*": unchanged
        self.assertEqual(agent.resolve_path(str(self.root / "Musik" / "Ansage_*.mp3")), new)
        os.utime(old, None)  # now the other one is newest
        self.assertEqual(agent.resolve_path(str(self.root / "Musik" / "Ansage_*")), old)
        # A folder level, and folders themselves (the song library).
        exe = touch(self.root / "Program Files" / "obs-studio 31.0" / "bin" / "obs64.exe")
        self.assertEqual(agent.resolve_path(str(self.root / "Program Files" / "obs-studio*" / "bin" / "obs64.exe")), exe)
        library = self.root / "Users" / "Beamer" / "foilpresenter Bibliothek"
        library.mkdir(parents=True)
        self.assertEqual(agent.resolve_path(str(self.root / "Users" / "*" / "foilpresenter Bibliothek"), folder=True), library)
        self.assertEqual(agent._songs_folder(str(self.root / "Users" / "*" / "foilpresenter*")), library)
        # Programs: only .exe matches.
        touch(self.root / "Tools" / "start.bat")
        start = touch(self.root / "Tools" / "start.exe", age=60)
        self.assertEqual(agent.resolve_path(str(self.root / "Tools" / "start*"), suffixes=(".exe",)), start)
        # "?" and "[" are part of a name, not wildcards.
        odd = touch(self.root / "Odd" / "Lied [live] ?.mp3")
        self.assertEqual(agent.resolve_path(str(self.root / "Odd" / "Lied [live] *")), odd)
        # Nothing matches, or the pattern is not a full path.
        with self.assertRaises(FileNotFoundError):
            agent.resolve_path(str(self.root / "Musik" / "Predigt*"))
        with self.assertRaises(FileNotFoundError):
            agent.resolve_path("Musik/*.mp3")

    def test_display_request_opens_the_resolved_file(self):
        image = touch(self.root / "Bilder" / "QR_neu.png")
        body = {"id": "qr", "title": "QR", "kind": "image", "path": str(self.root / "Bilder" / "QR_*.png"),
                "args": [], "fullscreen": True, "monitor": 1, "position": "top-left"}
        jobs = queue.Queue()
        with patch.dict(os.environ, {"DESKTOP_AGENT_TOKEN": "t"}), patch.object(agent, "requests", jobs):
            server = ThreadingHTTPServer(("127.0.0.1", 0), agent.DisplayHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                self.assertEqual(self._post(server, body), 202)
                self.assertEqual(jobs.get_nowait()["path"], str(image))
                self.assertEqual(self._post(server, {**body, "path": str(self.root / "Bilder" / "Fehlt*.png")}), 503)
            finally:
                server.shutdown()
                server.server_close()

    def _post(self, server, body):
        data = json.dumps(body).encode()
        with socket.create_connection(server.server_address, timeout=5) as sock:
            sock.sendall(b"POST /display HTTP/1.1\r\nHost: x\r\nX-Display-Token: t\r\n"
                         b"Content-Type: application/json\r\nContent-Length: " + str(len(data)).encode()
                         + b"\r\nConnection: close\r\n\r\n" + data)
            raw = b""
            while chunk := sock.recv(4096):
                raw += chunk
        return int(raw.split()[1])


class ServiceWildcardTests(unittest.TestCase):
    def test_profiles_and_file_actions_accept_a_star(self):
        program = DesktopProfileConfig(title="OBS", kind="program", path=r"C:\Program Files\obs*\bin\64bit\obs*")
        validate_profile(program)  # ends in "*": the agent only takes an .exe
        validate_profile(program.model_copy(update={"path": r"C:\Tools\obs*.exe"}))
        with self.assertRaises(ProfileEditError):
            validate_profile(program.model_copy(update={"path": r"C:\Tools\start.bat"}))
        self.assertIsNone(file_action_error("file:192.168.188.11|I:\\Musik\\Ansage_*.mp3"))
        # Never a program from a share, also not through a "*".
        self.assertTrue(network_program("program", r"\\server\share\tools\obs*"))
        self.assertTrue(network_program("program", r"\\server\share\tools\obs*.exe"))
        self.assertFalse(network_program("image", r"\\server\share\bilder\qr*"))
        self.assertIsNotNone(file_action_error("file:192.168.188.11|\\\\server\\share\\run*"))

    def test_runnable_files_on_a_share_are_refused_for_every_kind(self):
        # Opening an .exe or .bat "with its default app" runs it, too.
        for path in ("\\\\server\\share\\run.bat", "\\\\server\\share\\setup.msi", "//server/share/x.cmd"):
            self.assertTrue(network_program("file", path), path)
            self.assertIsNotNone(file_action_error(f"file:192.168.188.11|{path}"), path)
        self.assertFalse(network_program("file", "\\\\server\\share\\Predigt*.pdf"))
        self.assertFalse(network_program("program", "C:\\Tools\\run*"))


if __name__ == "__main__":
    unittest.main()
