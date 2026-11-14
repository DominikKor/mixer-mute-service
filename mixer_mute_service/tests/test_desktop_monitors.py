"""Desktop profiles on up to five monitors, the same image on several of them,
and a framed image window that may be closed without ending the display agent.
Agent stubbed; no device is contacted."""

import json
import os
import queue
import socket
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

from pydantic import ValidationError

from tests.helpers import app_client, require_tk

from app.config import DesktopProfileConfig
from app.desktop_profiles import ProfileEditError, validate_profile

tkinter = require_tk()
if tkinter is not None:
    from desktop_agent import show_image_agent as agent

PROFILES = """desktop_display:
  enabled: true
  profiles:
    qr:
      title: QR
      kind: image
      path: 'C:\\Bilder\\qr.png'
      monitors: [3, 1]
    eins:
      title: Eins
      kind: image
      path: 'C:\\Bilder\\eins.png'
      monitor: 2
"""


class ServiceMonitorTests(unittest.TestCase):
    def test_monitors_are_normalised(self):
        profile = DesktopProfileConfig(title="QR", kind="image", path=r"C:\qr.png", monitors=[5, 2, 5])
        self.assertEqual((profile.monitor, profile.monitors), (2, [2, 5]))
        single = DesktopProfileConfig(title="QR", kind="image", path=r"C:\qr.png", monitor=4, monitors=[3])
        self.assertEqual((single.monitor, single.monitors), (3, []))
        with self.assertRaises(ValidationError):
            DesktopProfileConfig(title="QR", kind="image", path=r"C:\qr.png", monitors=[1, 6])

    def test_validation(self):
        image = DesktopProfileConfig(title="QR", kind="image", path=r"C:\qr.png", monitor=5)
        validate_profile(image)
        with self.assertRaises(ProfileEditError):
            validate_profile(image.model_copy(update={"monitor": 6}))
        program = DesktopProfileConfig(title="OBS", kind="program", path=r"C:\obs.exe", monitors=[1, 2])
        with self.assertRaises(ProfileEditError):
            validate_profile(program)

    def test_payload_and_editing(self):
        with app_client(PROFILES, env={"DESKTOP_AGENT_TOKEN": "t"}) as (client, folder):
            with patch("app.display_agent._post", return_value={"ok": True}) as forward:
                self.assertEqual(client.post("/desktop/qr/open").status_code, 200)
                sent = forward.call_args.args[2]
                self.assertEqual((sent["monitor"], sent["monitors"]), (1, [1, 3]))
                client.post("/desktop/eins/open")
                self.assertNotIn("monitors", forward.call_args.args[2])  # older agents still work

            listed = {p["id"]: p for p in client.get("/desktop/profiles").json()["profiles"]}
            self.assertEqual(listed["qr"]["monitors"], [1, 3])
            self.assertEqual(listed["eins"]["monitors"], [])

            body = {"id": "obs", "title": "OBS", "kind": "program", "path": r"C:\obs.exe",
                    "monitor": 1, "monitors": [1, 2]}
            r = client.post("/desktop/profiles", json=body)
            self.assertEqual(r.status_code, 400)
            self.assertIn("only possible for images", r.json()["reason"])

            update = {**listed["eins"], "monitors": [2, 4]}
            self.assertEqual(client.put("/desktop/profiles/eins", json=update).status_code, 200)
            self.assertIn("monitors: [2, 4]", (folder / "config.yaml").read_text(encoding="utf-8"))
            update = {**listed["eins"], "monitor": 5, "monitors": []}
            self.assertEqual(client.put("/desktop/profiles/eins", json=update).status_code, 200)
            listed = {p["id"]: p for p in client.get("/desktop/profiles").json()["profiles"]}
            self.assertEqual((listed["eins"]["monitor"], listed["eins"]["monitors"]), (5, []))


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class AgentMonitorTests(unittest.TestCase):
    def test_valid_monitors(self):
        body = {"kind": "image", "monitor": 1, "monitors": [1, 3]}
        self.assertTrue(agent._valid_monitors(body))
        for bad in ({"monitors": [1]}, {"monitors": [1, 1]}, {"monitors": [1, 6]}, {"monitors": [2, 1]},
                    {"monitors": [1, True]}, {"monitors": "1,2"}, {"kind": "program"}):
            self.assertFalse(agent._valid_monitors({**body, **bad}), bad)

    def test_display_request_with_monitors(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "qr.png"
            image.write_bytes(b"x")
            body = {"id": "qr", "title": "QR", "kind": "image", "path": str(image), "args": [],
                    "fullscreen": False, "monitor": 1, "monitors": [1, 2], "position": "center"}
            jobs = queue.Queue()
            with patch.dict(os.environ, {"DESKTOP_AGENT_TOKEN": "t"}), patch.object(agent, "requests", jobs):
                server = ThreadingHTTPServer(("127.0.0.1", 0), agent.DisplayHandler)
                threading.Thread(target=server.serve_forever, daemon=True).start()
                try:
                    self.assertEqual(self._post(server, body), 202)
                    self.assertEqual(jobs.get_nowait()["monitors"], [1, 2])
                    self.assertEqual(self._post(server, {**body, "monitors": [1, 9]}), 400)
                    self.assertEqual(self._post(server, {**body, "extra": 1}), 400)
                finally:
                    server.shutdown()
                    server.server_close()

    def test_same_image_on_several_monitors_and_close_keeps_agent(self):
        try:
            root, label, _canvas = agent.create_root()
        except tkinter.TclError as exc:
            self.skipTest(f"no display: {exc}")
        self.addCleanup(root.destroy)
        profile = {"id": "qr", "kind": "image", "monitor": 1, "monitors": [1, 2, 4]}
        placed = []
        with patch.object(agent, "monitor_bounds", side_effect=lambda m: (0, 0, 100, 100)), \
                patch.object(agent, "_show_image_on", side_effect=lambda w, _l, _p, m: placed.append((w, m))):
            agent._show_image(root, label, profile)
            self.assertEqual([m for _w, m in placed], [1, 2, 4])
            self.assertIs(placed[0][0], root)
            self.assertEqual(len(agent._mirrors), 2)

            # A single-monitor image replaces the mirrors.
            agent._show_image(root, label, {**profile, "monitors": None})
            self.assertEqual(len(agent._mirrors), 0)

        placed.clear()
        with patch.object(agent, "monitor_bounds", side_effect=RuntimeError("Display 4 not found")), \
                patch.object(agent, "_show_image_on", side_effect=lambda w, _l, _p, m: placed.append(m)):
            with self.assertRaises(RuntimeError):
                agent._show_image(root, label, profile)
        self.assertEqual(placed, [])  # a missing monitor: nothing shown

        # The window's close button only hides it; the agent's window lives on.
        root.tk.call(root.protocol("WM_DELETE_WINDOW"))
        self.assertTrue(root.winfo_exists())
        self.assertEqual(root.state(), "withdrawn")

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


if __name__ == "__main__":
    unittest.main()
