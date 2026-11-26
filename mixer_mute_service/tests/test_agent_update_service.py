"""Update key, display agent step: the service finds the agent of the PC whose
browser asked (127.0.0.1 for its own PC), compares its version with the agent
files it ships, pushes them through the agent's self-update and waits until
the new version answers. Agents from before the self-update need the .cmd."""

import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch


class FakeAgent:
    """display_agent._post stand-in: one agent per address."""

    def __init__(self):
        self.agents = {}   # ip -> version, or None for an agent without /info
        self.players = {}  # ip -> extra /info fields (mpv, ytdlp, restored_env) of newer agents
        self.calls = []

    def __call__(self, url, token, payload, timeout):
        ip, path = url[len("http://"):].split(":", 1)[0], "/" + url.split("/", 3)[3]
        self.calls.append((ip, path, payload))
        if ip not in self.agents:
            raise urllib.error.URLError("no route")
        if self.agents[ip] is None:
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        if path == "/info":
            return {"ok": True, "version": self.agents[ip], **self.players.get(ip, {})}
        if path == "/update":
            self.agents[ip] = payload["version"]  # comes back with it after the restart
            return {"ok": True, "version": payload["version"], "restarting": True}
        raise AssertionError(path)


class AgentUpdateServiceTests(unittest.TestCase):
    def test_check_and_update_the_agent_of_this_pc(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                "server:\n  host: 127.0.0.1\n  port: 8765\n"
                "desktop_display:\n  enabled: true\n",
                encoding="utf-8",
            )
            fake = FakeAgent()
            env = {"MIXER_CONFIG": str(config_path), "DESKTOP_AGENT_TOKEN": "t", "MIXER_BUILD_ID": "v9"}
            with patch.dict(os.environ, env), patch("app.display_agent._post", fake), \
                    patch("app.agent_update.RESTART_WAIT_SECONDS", 4):
                from fastapi.testclient import TestClient
                from app.main import app

                # The service's own PC: its browser comes from 127.0.0.1.
                with TestClient(app, client=("127.0.0.1", 50000)) as here:
                    s = here.get("/update/agent").json()
                    self.assertEqual((s["present"], s["pc"], s["latest"]), (False, "127.0.0.1", "v9"))

                    fake.agents["127.0.0.1"] = "v8"
                    s = here.get("/update/agent").json()
                    self.assertEqual((s["present"], s["version"], s["current"], s["self_update"]),
                                     (True, "v8", False, True))
                    r = here.post("/update/agent")
                    self.assertEqual(r.status_code, 200, r.text)
                    self.assertEqual((r.json()["updated"], r.json()["version"]), (True, "v9"))
                    sent = next(p for ip, path, p in fake.calls if path == "/update")
                    self.assertEqual(sent["version"], "v9")
                    self.assertIn("show_image_agent.py", sent["files"])
                    self.assertIn("restart-agent.ps1", sent["files"])
                    self.assertNotIn("version.txt", sent["files"])

                    # Already current: nothing is sent.
                    n = len(fake.calls)
                    r = here.post("/update/agent")
                    self.assertEqual(r.json()["updated"], False)
                    self.assertFalse(any(path == "/update" for _, path, _ in fake.calls[n:]))

                # The Anzeige PC: an agent from before the self-update.
                with TestClient(app, client=("192.168.188.11", 50000)) as there:
                    fake.agents["192.168.188.11"] = None
                    s = there.get("/update/agent").json()
                    self.assertEqual((s["present"], s["self_update"]), (True, False))
                    r = there.post("/update/agent")
                    self.assertEqual(r.status_code, 409)
                    self.assertIn("too old", r.json()["reason"])

                # An iPad: no agent there, the step is skipped.
                with TestClient(app, client=("192.168.188.50", 50000)) as ipad:
                    self.assertFalse(ipad.get("/update/agent").json()["present"])
                    self.assertEqual(ipad.post("/update/agent").status_code, 404)

                # An agent that never comes back with the new version.
                with TestClient(app, client=("192.168.188.12", 50000)) as slow:
                    fake.agents["192.168.188.12"] = "v7"
                    with patch.object(FakeAgent, "__call__", lambda self, url, token, payload, timeout: (
                            {"ok": True, "version": "v7"} if url.endswith("/info") else {"ok": True})):
                        r = slow.post("/update/agent")
                    self.assertEqual(r.status_code, 504)


class AllAgentsTests(unittest.TestCase):
    def test_update_key_updates_every_pc(self):
        """The Update key on the streaming PC also updates the Anzeige PC,
        whose browser never presses it."""
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                "desktop_display:\n  enabled: true\n  profiles:\n"
                "    uhr:\n      title: Uhr\n      kind: file\n      path: C:/Uhr.url\n      target_ip: 192.168.188.12\n",
                encoding="utf-8",
            )
            fake = FakeAgent()
            fake.agents.update({"192.168.188.20": "v8", "192.168.188.11": "v8", "192.168.188.12": None})
            env = {"MIXER_CONFIG": str(config_path), "DESKTOP_AGENT_TOKEN": "t", "MIXER_BUILD_ID": "v9"}
            with patch.dict(os.environ, env), patch("app.display_agent._post", fake), \
                    patch("app.agent_update.RESTART_WAIT_SECONDS", 4):
                from fastapi.testclient import TestClient
                from app.main import app

                with TestClient(app, client=("127.0.0.1", 50000)) as here:
                    before = {a["pc"]: a for a in here.get("/update/agents").json()["agents"]}
                    self.assertEqual(sorted(before), ["192.168.188.11", "192.168.188.12", "192.168.188.20"])
                    self.assertEqual(before["192.168.188.11"]["name"], "Anzeige PC")
                    self.assertEqual((before["192.168.188.11"]["version"], before["192.168.188.11"]["current"]),
                                     ("v8", False))
                    self.assertNotIn("mpv", before["192.168.188.11"], "older agents do not say")
                    r = here.post("/update/agents").json()
                    after = {a["pc"]: a for a in r["agents"]}
                    self.assertTrue(r["ok"])
                    self.assertEqual(after["192.168.188.11"]["version"], "v9")
                    self.assertTrue(after["192.168.188.11"]["updated"] and after["192.168.188.20"]["updated"])
                    # Too old for the self-update: listed, nothing sent to it.
                    self.assertFalse(after["192.168.188.12"]["self_update"])
                    self.assertFalse(any(ip == "192.168.188.12" and path == "/update" for ip, path, _ in fake.calls))
                    # A second press has nothing to do.
                    n = len(fake.calls)
                    here.post("/update/agents")
                    self.assertFalse(any(path == "/update" for _, path, _ in fake.calls[n:]))
                    # Remote diagnosis: which mpv / yt-dlp each agent finds.
                    search = [{"place": "Program Files", "looked": "C:/Program Files/MPV Player/mpv.exe", "found": None}]
                    fake.players["192.168.188.11"] = {"mpv": None, "ytdlp": None, "restored_env": ["PATH"],
                                                      "mpv_search": search, "agent_user": "Beamer",
                                                      "agent_localappdata": r"C:\Users\Beamer\AppData\Local"}
                    fake.players["192.168.188.20"] = {"mpv": r"C:\Program Files\mpv\mpv.exe", "ytdlp": None,
                                                      "restored_env": []}
                    now = {a["pc"]: a for a in here.get("/update/agents").json()["agents"]}
                    self.assertEqual((now["192.168.188.11"]["mpv"], now["192.168.188.11"]["restored_env"]),
                                     (None, ["PATH"]))
                    # Where it looked, and as which Windows account.
                    self.assertEqual((now["192.168.188.11"]["mpv_search"], now["192.168.188.11"]["agent_user"]),
                                     (search, "Beamer"))
                    self.assertEqual(now["192.168.188.20"]["mpv"], r"C:\Program Files\mpv\mpv.exe")


class RestartFakeAgent(FakeAgent):
    """Newer agents: /info says restart is there and when the process
    started; /restart (and /update) start a new process."""

    def __init__(self):
        super().__init__()
        self.started = {}     # ip -> start time
        self.can_restart = set()

    def __call__(self, url, token, payload, timeout):
        ip, path = url[len("http://"):].split(":", 1)[0], "/" + url.split("/", 3)[3]
        if ip in self.agents and self.agents[ip] is not None:
            if path == "/info":
                self.calls.append((ip, path, payload))
                extra = {"restart": True, "started_at": self.started[ip]} if ip in self.can_restart else {}
                return {"ok": True, "version": self.agents[ip], **extra}
            if path == "/restart":
                self.calls.append((ip, path, payload))
                self.started[ip] += 100.0
                return {"ok": True, "version": self.agents[ip], "restarting": True}
            if path == "/update":
                self.started[ip] += 100.0
                self.can_restart.add(ip)  # the new files have /restart
        return super().__call__(url, token, payload, timeout)


class AgentRestartTests(unittest.TestCase):
    def test_update_key_restarts_current_agents_too(self):
        """Wish: the Update key always restarts the Display Services, also
        when they already run the newest version."""
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
                "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                "desktop_display:\n  enabled: true\n  profiles:\n"
                "    uhr:\n      title: Uhr\n      kind: file\n      path: C:/Uhr.url\n      target_ip: 192.168.188.12\n"
                "    tafel:\n      title: Tafel\n      kind: file\n      path: C:/T.url\n      target_ip: 192.168.188.13\n",
                encoding="utf-8",
            )
            fake = RestartFakeAgent()
            # .20 current with /restart, .11 behind, .12 too old, .13 not reachable
            fake.agents.update({"192.168.188.20": "v9", "192.168.188.11": "v8", "192.168.188.12": None})
            fake.started.update({"192.168.188.20": 1000.0, "192.168.188.11": 2000.0})
            fake.can_restart.add("192.168.188.20")
            env = {"MIXER_CONFIG": str(config_path), "DESKTOP_AGENT_TOKEN": "t", "MIXER_BUILD_ID": "v9"}
            with patch.dict(os.environ, env), patch("app.display_agent._post", fake), \
                    patch("app.agent_update.RESTART_WAIT_SECONDS", 4):
                from fastapi.testclient import TestClient
                from app.main import app

                with TestClient(app, client=("127.0.0.1", 50000)) as here:
                    status = {a["pc"]: a for a in here.get("/update/agents").json()["agents"]}
                    self.assertTrue(status["192.168.188.20"]["restart"])
                    self.assertFalse(status["192.168.188.11"]["restart"], "an older agent has no /restart")
                    r = here.post("/update/agents").json()
                    after = {a["pc"]: a for a in r["agents"]}
                    self.assertTrue(r["ok"], r)
                    self.assertTrue(after["192.168.188.20"].get("restarted"), "current agent restarted")
                    self.assertNotIn("updated", after["192.168.188.20"])
                    self.assertTrue(after["192.168.188.11"].get("updated"), "behind: self-update as before")
                    self.assertFalse(any(ip == "192.168.188.11" and p == "/restart" for ip, p, _ in fake.calls),
                                     "an agent without /restart is only updated")
                    self.assertFalse(after["192.168.188.12"]["self_update"])
                    self.assertFalse(after["192.168.188.13"]["present"], "not reachable: reported, nothing sent")
                    # A second press restarts both again (both have /restart now).
                    n = len(fake.calls)
                    r = here.post("/update/agents").json()
                    restarted = sorted(ip for ip, p, _ in fake.calls[n:] if p == "/restart")
                    self.assertEqual(restarted, ["192.168.188.11", "192.168.188.20"])
                    self.assertTrue(all(a.get("restarted") for a in r["agents"] if a["pc"] in restarted))

    def test_agent_that_does_not_come_back_is_reported(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text("mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                                   "desktop_display:\n  enabled: true\n", encoding="utf-8")
            fake = RestartFakeAgent()
            fake.agents["192.168.188.20"] = "v9"
            fake.started["192.168.188.20"] = 1000.0
            fake.can_restart.add("192.168.188.20")
            original = fake.__call__

            def stuck(url, token, payload, timeout):  # accepts /restart, never restarts
                if url.endswith("/restart"):
                    return {"ok": True, "restarting": True}
                return original(url, token, payload, timeout)

            env = {"MIXER_CONFIG": str(config_path), "DESKTOP_AGENT_TOKEN": "t", "MIXER_BUILD_ID": "v9"}
            with patch.dict(os.environ, env), patch("app.display_agent._post", stuck), \
                    patch("app.agent_update.RESTART_WAIT_SECONDS", 2):
                from fastapi.testclient import TestClient
                from app.main import app

                with TestClient(app, client=("127.0.0.1", 50000)) as here:
                    r = here.post("/update/agents").json()
                    agent = next(a for a in r["agents"] if a["pc"] == "192.168.188.20")
                    self.assertFalse(r["ok"])
                    self.assertEqual(agent["error"], "display agent did not come back after the restart")

    def test_info_carries_the_audio_sources_of_each_pc(self):
        """Each agent gets the audio sources of its own PC's audio profiles
        (it keeps local copies of them, so the music starts at once)."""
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            profile = "    {id}:\n      title: {id}\n      kind: {kind}\n      path: '{path}'\n      target_ip: {ip}\n"
            config_path.write_text(
                "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                "desktop_display:\n  enabled: true\n  profiles:\n"
                + profile.format(id="musik", kind="audio", path=r"I:\GoogleDrive\Musik.mp3", ip="192.168.188.20")
                + profile.format(id="musik2", kind="audio", path=r"I:\GoogleDrive\Musik.mp3", ip="192.168.188.11")
                + profile.format(id="lied", kind="audio", path="https://youtu.be/dQw4w9WgXcQ", ip="192.168.188.11")
                + profile.format(id="bild", kind="image", path=r"I:\Bild.png", ip="192.168.188.11"),
                encoding="utf-8",
            )
            fake = FakeAgent()
            fake.agents.update({"192.168.188.20": "v9", "192.168.188.11": "v9"})
            env = {"MIXER_CONFIG": str(config_path), "DESKTOP_AGENT_TOKEN": "t", "MIXER_BUILD_ID": "v9"}
            with patch.dict(os.environ, env), patch("app.display_agent._post", fake):
                from fastapi.testclient import TestClient
                from app.main import app

                with TestClient(app, client=("127.0.0.1", 50000)) as here:
                    here.get("/update/agents")
        sent = {ip: payload for ip, path, payload in fake.calls if path == "/info"}
        self.assertEqual(sent["192.168.188.20"], {"audio": [r"I:\GoogleDrive\Musik.mp3"]})
        self.assertEqual(sent["192.168.188.11"],
                         {"audio": [r"I:\GoogleDrive\Musik.mp3", "https://youtu.be/dQw4w9WgXcQ"]})

    def test_audio_sources_stay_within_the_agents_request_limit(self):
        from types import SimpleNamespace

        from app.agent_update import MAX_AUDIO_SOURCES, MAX_AUDIO_SOURCES_BYTES, audio_sources

        def request(paths):
            profiles = {str(i): SimpleNamespace(kind="audio", target_ip="10.0.0.1", path=p, mission_spotlight=False)
                        for i, p in enumerate(paths)}
            cfg = SimpleNamespace(desktop_display=SimpleNamespace(profiles=profiles))
            return SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(cfg=cfg)))

        many = audio_sources(request([f"C:/m/{i}.mp3" for i in range(50)]), "10.0.0.1")
        self.assertEqual(len(many), MAX_AUDIO_SOURCES)
        long = audio_sources(request(["C:/" + "x" * 900 + f"{i}.mp3" for i in range(15)]), "10.0.0.1")
        self.assertLessEqual(len(json.dumps(long)), MAX_AUDIO_SOURCES_BYTES)
        self.assertGreater(len(long), 0)


if __name__ == "__main__":
    unittest.main()
