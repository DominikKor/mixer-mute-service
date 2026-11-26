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
import threading
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


    def test_winget_shinchiro_mpv_in_program_files_mpv_player(self):
        """winget's shinchiro.mpv is an Inno installer for the machine: it
        installs to %ProgramFiles%\\MPV Player and does not touch PATH (the
        Anzeige PC found no mpv)."""
        for base in ("PF", "PF86"):
            with self.subTest(base):
                expected = touch(self.root / base / "MPV Player" / "mpv.exe")
                found, _ = self.find(self.env())
                self.assertEqual(Path(found), expected)
                expected.unlink()

    def test_install_location_from_the_registry(self):
        """Robust against a new folder name: the installer's InstallLocation."""
        expected = touch(self.root / "Somewhere" / "Video Player" / "mpv.exe")
        with patch.object(agent, "_installed_locations", return_value=[str(expected.parent)]) as locations:
            found, searched = self.find(self.env())
        self.assertEqual(Path(found), expected)
        locations.assert_called_with("mpv")
        self.assertIn("installed programs (registry)", searched)

    def test_no_registry_outside_windows(self):
        with patch.object(agent.os, "name", "posix"):
            self.assertEqual(agent._installed_locations("mpv"), [])

    def test_real_mpv_before_mpv_net(self):
        """MPV_PATH named MPV.NET's mpvnet.exe on the streaming PC: slow to
        start, and its control channel never answered. A real mpv.exe wins."""
        mpvnet = touch(self.root / "WindowsApps" / "MPV.NETMediaPlayer_7.1" / "MpvNet.Windows" / "mpvnet.exe")
        real = touch(self.root / "PF" / "MPV Player" / "mpv.exe")
        trace = []
        found, _ = self.find(self.env(MPV_PATH=str(mpvnet)), trace=trace)
        self.assertEqual(Path(found), real)
        first = trace[0]
        self.assertEqual((first["place"], Path(first["found"])), ("MPV_PATH", mpvnet))
        self.assertIn("not mpv itself", first["note"])
        self.assertEqual(Path(trace[-1]["found"]), real)

    def test_mpv_net_only_when_nothing_else_is_there(self):
        mpvnet = touch(self.root / "WindowsApps" / "MpvNet.Windows" / "mpvnet.exe")
        found, _ = self.find(self.env(MPV_PATH=str(mpvnet)))
        self.assertEqual(Path(found), mpvnet)

    def test_mpv_exe_instead_of_its_console_wrapper(self):
        """mpv.com starts mpv.exe as a child, which would outlive a stop."""
        folder = self.root / "bin"
        touch(folder / "mpv.exe")
        wrapper = touch(folder / "mpv.com")
        found, _ = self.find(self.env(MPV_PATH=str(wrapper)))
        self.assertEqual(Path(found), folder / "mpv.exe")

    def test_trace_names_every_place_looked_at(self):
        trace = []
        found, _ = self.find(self.env(), trace=trace)
        self.assertIsNone(found)
        places = [entry["place"] for entry in trace]
        for place in ("PATH", "the agent folder", "WinGet links (user)", "Program Files", "Program Files (x86)"):
            self.assertIn(place, places)
        self.assertTrue(any("MPV Player" in entry["looked"] or "[mM][pP][vV]" in entry["looked"]
                            for entry in trace))
        self.assertTrue(all(entry["found"] is None for entry in trace))


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


def info():
    handler = FakeHandler()
    with patch.dict(os.environ, {"DESKTOP_AGENT_TOKEN": "t"}):
        agent.DisplayHandler.do_POST(handler)
    return handler.status, json.loads(handler.wfile.getvalue())


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class InfoTests(unittest.TestCase):
    """/info is asked every 30 s with a 2 s timeout: it must always answer
    with the version, and never search in the request."""

    def setUp(self):
        saved = dict(agent._tool_cache)
        self.addCleanup(lambda: agent._tool_cache.update(saved))
        agent._tool_refreshing.clear()
        # /info reports mpv's last start from its log: never the real one.
        log = patch.object(agent, "mpv_log_path", return_value=Path(tempfile.gettempdir()) / "no-such-mpv.log")
        log.start()
        self.addCleanup(log.stop)

    def test_reports_the_cached_players_and_restored_variables(self):
        search = [{"place": "MPV_PATH", "looked": "x", "found": None}]
        agent._tool_cache.update(at=agent.time.monotonic(), mpv=r"C:\Program Files\mpv\mpv.exe", ytdlp=None,
                                 mpv_search=search)
        with patch.object(agent, "_find_mpv", side_effect=AssertionError("searched in the request")), \
                patch.object(agent, "RESTORED_ENV", ["PATH", "MPV_PATH"]), \
                patch.dict(os.environ, {"LOCALAPPDATA": r"C:\Users\Beamer\AppData\Local"}), \
                patch.object(agent.getpass, "getuser", return_value="Beamer"):
            status, answer = info()
        self.assertEqual(status, 200)
        self.assertEqual((answer["mpv"], answer["ytdlp"], answer["restored_env"]),
                         (r"C:\Program Files\mpv\mpv.exe", None, ["PATH", "MPV_PATH"]))
        self.assertEqual(answer["mpv_search"], search)
        self.assertEqual((answer["agent_user"], answer["agent_localappdata"]),
                         ("Beamer", r"C:\Users\Beamer\AppData\Local"))

    def test_nothing_said_before_the_first_search_is_done(self):
        """Right after a restart the service asks /info before the background
        search ends: "mpv: null" there showed a false "mpv nicht gefunden"."""
        agent._tool_cache.update(at=0.0, mpv=None, ytdlp=None)
        release = threading.Event()
        with patch.object(agent, "_find_mpv", side_effect=lambda *_: release.wait(5) and r"C:\mpv\mpv.exe"), \
                patch.object(agent, "_find_ytdlp", return_value=None):
            status, answer = info()
            self.assertEqual(status, 200)
            self.assertFalse({"mpv", "ytdlp"} & set(answer), "unknown yet: not reported")
            release.set()
            for _ in range(100):
                if agent._tool_cache["at"]:
                    break
                agent.time.sleep(0.02)
        status, answer = info()
        self.assertEqual((answer["mpv"], answer["ytdlp"]), (r"C:\mpv\mpv.exe", None))

    def test_version_comes_back_even_when_the_diagnosis_fails(self):
        with patch.object(agent, "cached_tools", side_effect=RuntimeError("registry gone")):
            status, answer = info()
        self.assertEqual((status, answer["ok"], answer["version"]), (200, True, agent.agent_version()))
        self.assertNotIn("mpv", answer)

    def test_old_search_is_renewed_in_the_background(self):
        agent._tool_cache.update(at=0.0, mpv=None, ytdlp=None)
        found = threading.Event()

        def slow_find(*_trace):
            found.set()
            return r"C:\mpv\mpv.exe"

        with patch.object(agent, "_find_mpv", side_effect=slow_find), \
                patch.object(agent, "_find_ytdlp", return_value=None):
            self.assertEqual(agent.cached_tools(), {}, "answers at once, nothing known yet")
            self.assertTrue(found.wait(5))
            for _ in range(100):
                if agent._tool_cache["mpv"]:
                    break
                agent.time.sleep(0.02)
        self.assertEqual(agent.cached_tools()["mpv"], r"C:\mpv\mpv.exe")

    def test_a_failing_search_leaves_nothing_found(self):
        with patch.object(agent, "_find_mpv", side_effect=OSError("access denied")):
            agent.refresh_tool_cache()
        self.assertEqual((agent._tool_cache["mpv"], agent._tool_cache["ytdlp"]), (None, None))


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class RestoreTests(unittest.TestCase):
    def test_odd_registry_names_never_stop_the_start(self):
        """A "(Standard)" value under HKCU\\Environment has an empty name;
        os.environ refuses it (ValueError) and the agent never started."""
        full = {"": "x", "A=B": "y", "BAD": "a\0b", "MPV_PATH": r"C:\mpv\mpv.exe"}
        with patch.object(agent.os, "name", "nt"), patch.object(agent, "_windows_environment", return_value=full), \
                patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MPV_PATH", None)
            self.assertEqual(agent.restore_user_environment(), ["MPV_PATH"])
            self.assertEqual(os.environ["MPV_PATH"], r"C:\mpv\mpv.exe")

    def test_machine_scope_never_gives_the_user_name(self):
        env = agent._windows_environment(
            {"Path": "/x"}, {"machine": {"USERNAME": "SYSTEM", "USERPROFILE": "/sys", "TEMPDIR": "/t"},
                             "user": {"USERNAME": "beamer"}}, {})
        self.assertEqual(env["USERNAME"], "beamer", "the user's own value")
        self.assertNotIn("USERPROFILE", env, "not from the machine")
        self.assertEqual(env["TEMPDIR"], "/t", "other machine variables are filled in")
        env = agent._windows_environment({"Path": "/x"}, {"machine": {"USERNAME": "SYSTEM"}, "user": {}}, {})
        self.assertNotIn("USERNAME", env)


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class RestartEndsMusicTests(unittest.TestCase):
    """Every press of the Update key restarts the agent: a player left
    running would be unknown to the new agent (no Stop, a second Play)."""

    class FakeProcess:
        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    class FakeIpc:
        def __init__(self):
            self.volumes = []
            self.closed = False

        def command(self, *args):
            if args[:2] == ("set_property", "volume"):
                self.volumes.append(args[2])

        def close(self):
            self.closed = True

    def tearDown(self):
        agent._playback = None
        agent._fading_out.clear()

    def test_short_fade_out_then_end(self):
        process, ipc = self.FakeProcess(), self.FakeIpc()
        agent._playback = agent._Playback("music", process, ipc, {"db": 0.0, "seconds": 5})
        with patch.object(agent, "FADE_STEP_SECONDS", 0.005):
            self.assertTrue(agent.stop_audio_for_restart(0.05))
        self.assertTrue(process.terminated and ipc.closed)
        self.assertEqual(ipc.volumes, sorted(ipc.volumes, reverse=True), "only falling")
        self.assertAlmostEqual(ipc.volumes[-1], agent.db_to_mpv_volume(agent.FADE_FLOOR_DB))
        self.assertIsNone(agent._playback)

    def test_without_control_channel_at_once(self):
        process = self.FakeProcess()
        fading = agent._Playback("old", self.FakeProcess(), None, None)
        agent._fading_out.add(fading)
        agent._playback = agent._Playback("music", process, None, None)
        self.assertTrue(agent.stop_audio_for_restart())
        self.assertTrue(process.terminated and fading.process.terminated)
        self.assertEqual(agent._fading_out, set())
        self.assertFalse(agent.stop_audio_for_restart(), "nothing left")

    def test_restart_ends_the_music_first(self):
        order = []
        with patch.object(agent, "stop_audio_for_restart", side_effect=lambda: order.append("music")), \
                patch.object(agent.os, "name", "posix"), \
                patch.object(agent.subprocess, "Popen", side_effect=lambda *a, **k: order.append("start")), \
                patch.object(agent.logging, "shutdown"), \
                patch.object(agent.os, "_exit", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                agent._restart_agent()
        self.assertEqual(order, ["music", "start"])

    def test_restart_happens_even_if_ending_the_music_fails(self):
        started = []
        with patch.object(agent, "stop_audio_for_restart", side_effect=RuntimeError("mpv hangs")), \
                patch.object(agent.os, "name", "posix"), \
                patch.object(agent.subprocess, "Popen", side_effect=lambda *a, **k: started.append(True)), \
                patch.object(agent.logging, "shutdown"), \
                patch.object(agent.os, "_exit", side_effect=SystemExit):
            with self.assertRaises(SystemExit):
                agent._restart_agent()
        self.assertEqual(started, [True])


if __name__ == "__main__":
    unittest.main()
