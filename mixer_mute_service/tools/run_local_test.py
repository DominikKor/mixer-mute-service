"""Run the whole service locally for browser checks, with NO real device.

    cd mixer_mute_service
    python -m tools.run_local_test            # http://127.0.0.1:8770

The service code in app/ has the real LAN hosts built in (Shellys, Art-Net
node, beamer, VISCA cameras, OBS, display agents). Started plainly, a test
service talks to them: with the Licht section open it asks every Shelly for
its state every 3 s. CLAUDE.md forbids that, so this runner:

1. installs the network guard of the tests (importing ``tests``): any
   connect/sendto to a non-loopback address raises, whatever code tries it;
2. replaces every device function with a fake that answers plausibly
   (lights keep their state, OBS has scenes, display agents accept and
   report the current version), so the whole UI can be clicked through;
3. starts tools/fake_mixer.py on 127.0.0.1 and points the service at it;
4. keeps its data (config.yaml, groups, program, ...) in local-test/,
   which git ignores; delete the folder to start from scratch.

Device actions are logged as "FAKE ..." lines, so a check can see what
would have been sent.

    python -m tools.run_local_test --dead-shelly light-front --shelly-delay 0.3

plays a room light circuit that is off the network (every call hangs for
the connect timeout, then fails) and a delay for the others' answers.

    python -m tools.run_local_test --dead-agent 192.168.188.20

plays a PC whose display agent is off: every call to it hangs until its
timeout (song lyrics on several screens: the others must not wait).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import tests  # noqa: E402,F401  (installs the network guard)

log = logging.getLogger("fake_devices")

CONFIG_TEMPLATE = """\
# Local test setup (tools/run_local_test.py): fake mixer and fake devices.
mixer:
  ip: "127.0.0.1"
  port: {mixer_port}
  max_channels: 16
  mode: "direct"
safety:
  freshness_mode: "latest"
server:
  host: "127.0.0.1"
  port: {port}
desktop_display:
  enabled: true
  profiles:
    qr-code:
      title: QR Gemeindegaben
      kind: image
      path: I:\\Googledrive\\Streamingbilder\\Gemeindegaben QR.png
      target_ip: 192.168.188.11
    hintergrundmusik:
      title: Hintergrundmusik
      kind: audio
      path: I:\\GoogleDrive\\Musikbeitraege\\Hintergrundmusik.mp3
      target_ip: 192.168.188.20
      fade: true
groups:
  Predigt:
    channels: [1]
  Band:
    channels: [3, 4, 5, 6, 7, 8]
"""


class FakeDevices:
    """Answers for every device the service talks to."""

    def __init__(self, native):
        self.native = native
        self.shelly = {host: target == "light-tech" for target, host in native.SHELLY_HOSTS.items()}
        self.dead_hosts = set()   # Shellys that never answer
        self.shelly_delay = 0.0   # seconds every other Shelly takes to answer
        self.scene = "Cam 1"
        self.preview = "Cam 2"   # studio mode, as on the streaming PC
        self.transition_ms = 300
        self.bible = {}          # scene -> BibleShow visible
        self.agent_started = {}  # "ip:port" of a fake display agent -> its start time
        self.dead_agents = set()  # IPs whose display agent never answers
        self.artnet_frames = 0
        self.artnet_last = [0] * 16
        self.scenes = ["Willkommen (neu)", "Cam 1", "Cam 2", "PC2", "BeamerVonVorne", "Ende (neu)", "Preview Scene"]

    def shelly_rpc(self, host, method, params):
        if host in self.dead_hosts:
            # No connection, as a switched-off Shelly (switching tries twice).
            attempts = 1 if method.endswith(".GetStatus") else self.native.SHELLY_SWITCH_CONNECT_ATTEMPTS
            time.sleep(self.native.SHELLY_CONNECT_TIMEOUT * attempts)
            raise self.native.NativeControlError(f"Shelly {host} unreachable")
        time.sleep(self.shelly_delay)
        if method == "Switch.GetStatus":
            return {"id": 0, "output": self.shelly.get(host, False)}
        if method == "Switch.Set":
            was = self.shelly.get(host, False)
            self.shelly[host] = bool(params.get("on"))
            log.info("FAKE Shelly %s on=%s", host, self.shelly[host])
            return {"was_on": was}
        if method == "Switch.Toggle":
            self.shelly[host] = not self.shelly.get(host, False)
            log.info("FAKE Shelly %s toggled -> %s", host, self.shelly[host])
            return {"was_on": not self.shelly[host]}
        return {}

    def artnet(self, host, universe, values):
        # Sent every 40 ms during a fade and every second as keepalive: not
        # logged, only counted (the fake node's report shows the count).
        self.artnet_frames += 1
        self.artnet_last = list(values[:16])

    def artnet_status(self, host=None):
        # Same shape as the real ArtPollReply parser (_parse_poll_reply).
        return {"short_name": "Fake node", "long_name": "Local test",
                "report": f"{self.artnet_frames} frames, ch1-16 {self.artnet_last}",
                "outputs": [{"sending_dmx": self.artnet_frames > 0, "merging": False, "merge_mode": "HTP"}]}

    def beamer_form(self, field, value):
        log.info("FAKE beamer %s=%s", field, value)

    def visca(self, host, port, payload):
        log.info("FAKE VISCA %s %s", host, payload.hex())

    def obs(self, host, port, request_type, request_data=None):
        data = request_data or {}
        if request_type == "GetCurrentProgramScene":
            return {"currentProgramSceneName": self.scene, "sceneName": self.scene}
        if request_type == "SetCurrentProgramScene":
            self.scene = data.get("sceneName", self.scene)
            log.info("FAKE OBS scene -> %s", self.scene)
            return {}
        if request_type == "GetSceneList":
            return {"currentProgramSceneName": self.scene,
                    "scenes": [{"sceneName": n, "sceneIndex": i} for i, n in enumerate(reversed(self.scenes))]}
        if request_type in ("GetSceneItemList", "GetGroupSceneItemList"):
            scene = data.get("sceneName", "")
            source = {"Cam 1": "Cam1", "Cam 2": "Cam2"}.get(scene, "Capture_PC2")
            items = [{"sceneItemId": 1, "sourceName": source, "sceneItemEnabled": True, "isGroup": False}]
            if scene in ("Cam 1", "Cam 2"):  # the Bible text (Companion's "Bibel Cam N Beamer")
                items.append({"sceneItemId": 2, "sourceName": "BibleShow", "isGroup": False,
                              "sceneItemEnabled": self.bible.get(scene, False)})
            return {"sceneItems": items}
        if request_type == "SetSceneItemEnabled" and data.get("sceneItemId") == 2:
            self.bible[data["sceneName"]] = data["sceneItemEnabled"]
            log.info("FAKE OBS BibleShow in %s -> %s", data["sceneName"], data["sceneItemEnabled"])
            return {}
        if request_type == "GetCurrentSceneTransition":
            return {"transitionName": "Fade", "transitionFixed": False, "transitionDuration": self.transition_ms}
        if request_type == "SetCurrentSceneTransitionDuration":
            self.transition_ms = data["transitionDuration"]
            log.info("FAKE OBS transition -> %s ms", self.transition_ms)
            return {}
        if request_type == "GetStudioModeEnabled":
            return {"studioModeEnabled": True}
        if request_type == "GetCurrentPreviewScene":
            return {"currentPreviewSceneName": self.preview}
        if request_type == "SetCurrentPreviewScene":
            self.preview = data.get("sceneName", self.preview)
            return {}
        log.info("FAKE OBS %s %s", request_type, data)
        return {}

    def agent(self, url, token, payload, timeout):
        path = "/" + url.split("/", 3)[3]
        if url.split("/")[2].split(":")[0] in self.dead_agents:
            # A PC that is off: no answer at all until the timeout (not a quick refusal).
            time.sleep(timeout)
            raise TimeoutError("fake display agent is off")
        if path == "/info":
            from app.agent_download import agent_dir
            version_file = agent_dir() / "version.txt"
            version = version_file.read_text(encoding="utf-8").strip() if version_file.exists() else "dev"
            # The Anzeige PC's agent finds no mpv (to see the warning in the update dialog).
            no_mpv = "192.168.188.11" in url
            return {"ok": True, "version": os.environ.get("MIXER_BUILD_ID", version),
                    "mpv": None if no_mpv else r"C:\Program Files\mpv\mpv.exe",
                    "ytdlp": None if no_mpv else r"C:\Program Files\mpv\yt-dlp.exe", "restored_env": [],
                    "restart": True, "started_at": self.agent_started.setdefault(url.split("/")[2], 1000.0)}
        if path in ("/restart", "/update"):
            # A new agent process: its start time changes (the service waits for that).
            host = url.split("/")[2]
            self.agent_started[host] = self.agent_started.get(host, 1000.0) + 1.0
        if path == "/songs/library":
            return {"ok": True, "files": []}
        host = url.split("/")[2]
        documents = self.__dict__.setdefault("documents", {})  # host -> the document it shows
        if path == "/display" and payload.get("kind") == "file":
            from app.documents import is_document
            if is_document(payload.get("path", "")):
                # A 12 page document, shown at once (desktop_agent/documents.py).
                if not (payload.get("keep_if_open") and documents.get(host, {}).get("path") == payload["path"]):
                    documents[host] = {"id": payload["id"], "path": payload["path"], "page": 1, "pages": 12,
                                       "title": payload["path"].replace("/", "\\").rsplit("\\", 1)[-1]}
                log.info("FAKE display agent %s shows document %s", host, payload["path"])
                return {"ok": True, "profile": payload["id"], "shown": True}
        if path == "/close" and documents.get(host, {}).get("id") == payload.get("id"):
            documents.pop(host)
        if path == "/document":
            shown = documents.get(host)
            if not shown:
                import urllib.error
                raise urllib.error.HTTPError(url, 409, "no document shown", {}, None)
            from desktop_agent.documents import step
            shown["page"] = step(shown["page"] - 1, shown["pages"], payload["action"], payload.get("page")) + 1
            log.info("FAKE display agent %s document page %s/%s", host, shown["page"], shown["pages"])
            return {"ok": True, **{k: v for k, v in shown.items() if k != "path"}}
        if path == "/song/metrics":
            # A 1920 x 1080 monitor and Verdana-like widths (the service's estimate).
            from app.slide_layout import estimate_metrics
            widths = estimate_metrics()
            return {"ok": True, "width": 1920, "height": 1080, "ref": 100,
                    "widths": [widths.ref_width(c) for c in payload["chars"]]}
        log.info("FAKE display agent %s %s", url, {k: v for k, v in payload.items() if k != "files"})
        return {"ok": True, "action": payload.get("action"), "restarting": path == "/update"}


def install_fakes() -> FakeDevices:
    import app.camera_program as camera_program
    import app.camera_streams as camera_streams
    import app.display_agent as display_agent
    import app.native_controls as native

    fakes = FakeDevices(native)
    native._shelly_rpc = fakes.shelly_rpc
    native._send_artnet_frame = fakes.artnet
    native.artnet_status = fakes.artnet_status
    native._post_beamer_form = fakes.beamer_form
    native._visca_tcp = fakes.visca
    native._obs_request = fakes.obs

    def obs_batch(host, port, requests):
        answers = []
        for request_type, data in requests:
            try:
                answers.append(fakes.obs(host, port, request_type, data))
            except native.NativeControlError:
                answers.append(None)
        return answers

    native._obs_batch = obs_batch
    camera_program._obs_request = fakes.obs
    display_agent._post = fakes.agent
    camera_streams.MediaMTXRelay._binary = staticmethod(lambda: None)
    return fakes


def start_fake_mixer(port: int) -> None:
    from tools.fake_mixer import serve

    loop = asyncio.new_event_loop()

    def run():
        asyncio.set_event_loop(loop)
        loop.run_until_complete(serve(port=port))
        loop.run_forever()

    threading.Thread(target=run, name="fake-mixer", daemon=True).start()


def main() -> None:
    parser = argparse.ArgumentParser(description="Local test service with a fake mixer and fake devices.")
    parser.add_argument("--port", type=int, default=8770, help="service port (default 8770)")
    parser.add_argument("--mixer-port", type=int, default=16500, help="fake mixer port (default 16500)")
    parser.add_argument("--data", type=Path, default=ROOT / "local-test", help="data folder (default local-test/)")
    parser.add_argument("--dead-shelly", action="append", default=[], metavar="CIRCUIT",
                        help="room light circuit that never answers, e.g. light-front (repeatable)")
    parser.add_argument("--dead-agent", action="append", default=[], metavar="IP",
                        help="PC whose display agent never answers, e.g. 192.168.188.20 (repeatable)")
    parser.add_argument("--shelly-delay", type=float, default=0.0, metavar="SECONDS",
                        help="time every other fake Shelly takes to answer")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args.data.mkdir(parents=True, exist_ok=True)
    config = args.data / "config.yaml"
    if not config.exists():
        config.write_text(CONFIG_TEMPLATE.format(mixer_port=args.mixer_port, port=args.port), encoding="utf-8")
    os.environ["MIXER_CONFIG"] = str(config)
    os.environ.setdefault("DESKTOP_AGENT_TOKEN", "local-test")

    fakes = install_fakes()
    import app.native_controls as native
    unknown = [c for c in args.dead_shelly if c not in native.SHELLY_HOSTS]
    if unknown:
        parser.error(f"unknown circuit {unknown}; known: {', '.join(native.SHELLY_HOSTS)}")
    fakes.dead_hosts = {native.SHELLY_HOSTS[c] for c in args.dead_shelly}
    fakes.shelly_delay = max(0.0, args.shelly_delay)
    fakes.dead_agents = set(args.dead_agent)
    start_fake_mixer(args.mixer_port)

    import uvicorn
    from app.main import app

    log.info("local test service: http://127.0.0.1:%s (data in %s)", args.port, args.data)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
