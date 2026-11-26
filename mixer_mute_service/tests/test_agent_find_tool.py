"""How the display agent finds mpv and yt-dlp (desktop_agent find_tool).

After a self-update the agent is started through WMI without the user's
environment, so it did not find a winget-installed mpv any more ("mpv was not
found" on the Anzeige PC). The search now also takes the user's and the
machine's variables from the registry and looks in the usual install places.
The environment, the registry and the folders are faked here."""

import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tests.helpers import require_tk

# None without tkinter (tests skip); with REQUIRE_TK=1 (CI) that is an error.
tkinter = require_tk()

if tkinter is not None:
    from desktop_agent import show_image_agent as agent


def touch(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    return path


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class FindToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        # An empty agent folder: nothing is found next to the agent.
        patcher = patch.object(agent, "AGENT_DIR", self.root / "agent")
        patcher.start()
        self.addCleanup(patcher.stop)

    def env(self, **extra):
        base = {
            "PATH": str(self.root / "empty"),
            "LOCALAPPDATA": str(self.root / "Local"),
            "USERPROFILE": str(self.root / "User"),
            "PROGRAMFILES": str(self.root / "PF"),
            "PROGRAMFILES(X86)": str(self.root / "PF86"),
            "PROGRAMDATA": str(self.root / "PD"),
        }
        return {**base, **extra}

    def find(self, env, **kwargs):
        return agent.find_tool("mpv", "MPV_PATH", "mpv", "mpv", env=env, exe="mpv.exe", **kwargs)

    def test_install_places(self):
        places = {
            "WinGet links (user)": "Local/Microsoft/WinGet/Links/mpv.exe",
            "WinGet links (machine)": "PF/WinGet/Links/mpv.exe",
            "WinGet packages (user)": "Local/Microsoft/WinGet/Packages/shinchiro.mpv_Microsoft.Winget.Source_8wekyb3d8bbwe/mpv.exe",
            "WinGet packages (machine)": "PF/WinGet/Packages/shinchiro.mpv_X/sub/mpv.exe",
            "scoop shims": "User/scoop/shims/mpv.exe",
            "scoop app": "User/scoop/apps/mpv/current/mpv.exe",
            "scoop global": "PD/scoop/shims/mpv.exe",
            "Chocolatey bin": "PD/chocolatey/bin/mpv.exe",
            "Chocolatey lib": "PD/chocolatey/lib/mpvio.install/tools/mpv/mpv.exe",
            "Program Files": "PF/mpv/mpv.exe",
            "Program Files (x86)": "PF86/mpv/mpv.exe",
            "LocalAppData Programs": "Local/Programs/mpv/mpv.exe",
        }
        for label, relative in places.items():
            with self.subTest(label):
                expected = touch(self.root / relative)
                found, _ = self.find(self.env())
                self.assertEqual(Path(found), expected)
                expected.unlink()

    def test_variables_and_path_come_first(self):
        custom = touch(self.root / "Tools" / "mpv-custom.exe")
        touch(self.root / "PF" / "mpv" / "mpv.exe")
        found, searched = self.find(self.env(MPV_PATH=str(custom)))
        self.assertEqual((found, searched), (str(custom), ["MPV_PATH"]))
        on_path = touch(self.root / "bin" / "mpv")
        on_path.chmod(0o755)
        found, _ = agent.find_tool("mpv", "MPV_PATH", "mpv", "mpv", env=self.env(PATH=str(on_path.parent)))
        self.assertEqual(Path(found), on_path)

    def test_near_folder_for_ytdlp(self):
        mpv_dir = self.root / "PF" / "mpv"
        ytdlp = touch(mpv_dir / "yt-dlp.exe")
        found, _ = agent.find_tool("yt-dlp", "YTDLP_PATH", "yt-dlp", "yt-dlp", near=(mpv_dir,),
                                   env=self.env(), exe="yt-dlp.exe")
        self.assertEqual(Path(found), ytdlp)

    def test_not_found_names_the_places(self):
        found, searched = self.find(self.env())
        self.assertIsNone(found)
        message = agent.missing_tool_message("mpv", "MPV_PATH", searched, "winget install mpv")
        for place in ("PATH (incl. the user's PATH from the registry)", "WinGet links (user)", "scoop",
                      "Chocolatey", "Program Files (x86)", "the agent folder"):
            self.assertIn(place, message)
        self.assertTrue(message.startswith("mpv was not found on this PC (searched: "))
        self.assertTrue(message.endswith("install it (winget install mpv) or set MPV_PATH"))


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class WindowsEnvironmentTests(unittest.TestCase):
    """What a WMI-started process lacks is filled in from the registry."""

    def test_user_variables_and_path_from_the_registry(self):
        # Paths without drive letters: os.pathsep is ":" where the tests run.
        sep = os.pathsep
        process = {"Path": "/Windows/system32", "LOCALAPPDATA": "/Windows/system32/config/systemprofile/AppData/Local",
                   "DESKTOP_AGENT_TOKEN": "from-process"}
        registry = {
            "machine": {"PATH": f"/Windows{sep}/Program Files/Tools", "DESKTOP_AGENT_TOKEN": "machine"},
            "user": {"PATH": f"%LOCALAPPDATA%/Microsoft/WinGet/Links{sep}/windows",
                     "MPV_PATH": "%localappdata%/mpv/mpv.exe"},
        }
        folders = {"LOCALAPPDATA": "/Users/beamer/AppData/Local", "USERPROFILE": "/Users/beamer"}
        env = agent._windows_environment(process, registry, folders)
        self.assertEqual(env["LOCALAPPDATA"], "/Users/beamer/AppData/Local", "the system profile is replaced")
        self.assertEqual(env["MPV_PATH"], "/Users/beamer/AppData/Local/mpv/mpv.exe", "user variable, expanded")
        self.assertEqual(env["DESKTOP_AGENT_TOKEN"], "from-process", "what the process has is kept")
        self.assertEqual(env["PATH"].split(sep), [
            "/Windows/system32", "/Windows", "/Program Files/Tools",
            "/Users/beamer/AppData/Local/Microsoft/WinGet/Links",
        ], "all entries, each once (case-insensitive), the user's expanded")

    def test_nothing_to_restore_outside_windows(self):
        with patch.object(agent.os, "name", "posix"):
            self.assertEqual(agent.restore_user_environment(), [])
            self.assertEqual(agent._registry_environment(), {"machine": {}, "user": {}})


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class InfoTests(unittest.TestCase):
    def test_info_reports_players_and_restored_variables(self):
        class FakeHandler:
            path = "/info"

            def __init__(self):
                self.wfile = io.BytesIO()
                self.status = None
                self.headers = {"X-Display-Token": "t", "Content-Length": "2"}
                self.rfile = io.BytesIO(b"{}")

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

        handler = FakeHandler()
        with patch.dict(os.environ, {"DESKTOP_AGENT_TOKEN": "t"}), \
                patch.object(agent, "_find_mpv", return_value=r"C:\Program Files\mpv\mpv.exe"), \
                patch.object(agent, "_find_ytdlp", return_value=None), \
                patch.object(agent, "RESTORED_ENV", ["PATH", "MPV_PATH"]):
            agent.DisplayHandler.do_POST(handler)
        answer = json.loads(handler.wfile.getvalue())
        self.assertEqual(handler.status, 200)
        self.assertEqual((answer["mpv"], answer["ytdlp"], answer["restored_env"]),
                         (r"C:\Program Files\mpv\mpv.exe", None, ["PATH", "MPV_PATH"]))


if __name__ == "__main__":
    unittest.main()
