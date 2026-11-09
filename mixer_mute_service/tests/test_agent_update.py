"""Self-update of the desktop agent: request checks, installing the files
(a broken upload leaves the agent untouched), version reporting, and the
/info and /update endpoints over HTTP (the restart itself is replaced)."""

import base64
import json
import os
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from tests.helpers import require_tk

# None without tkinter (tests skip); with REQUIRE_TK=1 (CI) that is an error.
tkinter = require_tk()

if tkinter is not None:
    from desktop_agent import show_image_agent as agent


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


GOOD = {"version": "v1.2.1-99-gabc", "files": {"show_image_agent.py": b64("print('new agent')\n"),
                                                "requirements.txt": b64("Pillow\n")}}


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class AgentUpdateTests(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp())
        (self.dir / "show_image_agent.py").write_text("print('old agent')\n")
        patches = [patch.object(agent, "AGENT_DIR", self.dir),
                   patch.object(agent, "VERSION_FILE", self.dir / "version.txt")]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def test_update_requests(self):
        self.assertTrue(agent._valid_update_request(GOOD))
        self.assertFalse(agent._valid_update_request({**GOOD, "extra": 1}))
        self.assertFalse(agent._valid_update_request({**GOOD, "version": "1 2"}))
        self.assertFalse(agent._valid_update_request({**GOOD, "files": {"requirements.txt": b64("x")}}))
        for name in ("../evil.py", "sub/x.py", "x.exe", "x.py.bat", ".hidden.py"):
            files = {**GOOD["files"], name: b64("x")}
            self.assertFalse(agent._valid_update_request({**GOOD, "files": files}), name)

    def test_install_writes_files_and_version(self):
        self.assertEqual(agent.agent_version(), "unknown")
        agent._install_update(GOOD)
        self.assertEqual((self.dir / "show_image_agent.py").read_text(), "print('new agent')\n")
        self.assertEqual((self.dir / "requirements.txt").read_text(), "Pillow\n")
        self.assertEqual(agent.agent_version(), "v1.2.1-99-gabc")
        self.assertEqual(sorted(p.name for p in self.dir.glob(".*")), [])  # no leftovers

    def test_broken_upload_leaves_the_agent_untouched(self):
        broken = {"version": "v2", "files": {"show_image_agent.py": b64("def (:\n"), "requirements.txt": b64("x")}}
        with self.assertRaises(SyntaxError):
            agent._install_update(broken)
        not_base64 = {"version": "v2", "files": {"show_image_agent.py": "***"}}
        with self.assertRaises(ValueError):
            agent._install_update(not_base64)
        self.assertEqual((self.dir / "show_image_agent.py").read_text(), "print('old agent')\n")
        self.assertFalse((self.dir / "requirements.txt").exists())
        self.assertEqual(agent.agent_version(), "unknown")

    def _post(self, server, path, body, token="t"):
        data = json.dumps(body).encode()
        with socket.create_connection(server.server_address, timeout=5) as sock:
            sock.sendall(f"POST {path} HTTP/1.1\r\nHost: x\r\nX-Display-Token: {token}\r\n"
                         f"Content-Type: application/json\r\nContent-Length: {len(data)}\r\n"
                         "Connection: close\r\n\r\n".encode() + data)
            raw = b""
            while chunk := sock.recv(65536):
                raw += chunk
        head, _, payload = raw.partition(b"\r\n\r\n")
        status = int(head.split()[1])
        return status, (json.loads(payload) if payload.startswith(b"{") else None)

    def test_info_and_update_over_http(self):
        restarted = threading.Event()
        with patch.dict(os.environ, {"DESKTOP_AGENT_TOKEN": "t"}), \
                patch.object(agent, "_restart_agent", restarted.set):
            server = ThreadingHTTPServer(("127.0.0.1", 0), agent.DisplayHandler)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            try:
                self.assertEqual(self._post(server, "/info", {}), (200, {"ok": True, "version": "unknown"}))
                self.assertEqual(self._post(server, "/info", {}, token="wrong")[0], 401)
                self.assertEqual(self._post(server, "/update", {"version": "v2"})[0], 400)
                status, answer = self._post(server, "/update", GOOD)
                self.assertEqual((status, answer["version"], answer["restarting"]), (202, GOOD["version"], True))
                self.assertTrue(restarted.wait(5), "the agent did not restart")
                self.assertEqual(self._post(server, "/info", {})[1]["version"], GOOD["version"])
            finally:
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
