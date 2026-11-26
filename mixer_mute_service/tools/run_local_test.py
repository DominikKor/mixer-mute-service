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
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import threading
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
        self.scene = "Cam 1"
        self.agent_started = {}  # "ip:port" of a fake display agent -> its start time
        self.scenes = ["Willkommen (neu)", "Cam 1", "Cam 2", "PC2", "BeamerVonVorne", "Ende (neu)", "Preview Scene"]

    def shelly_rpc(self, host, method, params):
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
        pass  # sent every 40 ms during a fade: not logged

    def artnet_status(self, host=None):
        return {"ok": True, "short_name": "Fake node", "long_name": "Local test", "merging": False}

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
            return {"sceneItems": [{"sceneItemId": 1, "sourceName": source, "sceneItemEnabled": True,
                                    "isGroup": False}]}
        if request_type == "GetCurrentSceneTransition":
            return {"transitionDuration": 300}
        log.info("FAKE OBS %s %s", request_type, data)
        return {}

    def agent(self, url, token, payload, timeout):
        path = "/" + url.split("/", 3)[3]
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
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args.data.mkdir(parents=True, exist_ok=True)
    config = args.data / "config.yaml"
    if not config.exists():
        config.write_text(CONFIG_TEMPLATE.format(mixer_port=args.mixer_port, port=args.port), encoding="utf-8")
    os.environ["MIXER_CONFIG"] = str(config)
    os.environ.setdefault("DESKTOP_AGENT_TOKEN", "local-test")

    install_fakes()
    start_fake_mixer(args.mixer_port)

    import uvicorn
    from app.main import app

    log.info("local test service: http://127.0.0.1:%s (data in %s)", args.port, args.data)
    uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="info")


if __name__ == "__main__":
    main()
