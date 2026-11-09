"""Camera HLS through the service's port (app/camera_hls.py), MediaMTX faked.

Verifies: the relay config serves HLS on 127.0.0.1 only (WebRTC unchanged),
requests for configured cameras are passed on with their query (low-latency
HLS) and content type, MediaMTX's cookie-check redirect goes to the browser
under /camera-hls/ (not followed here), cookies pass both ways, upstream
errors pass through, and unknown cameras,
odd paths and a relay that is not ready are refused without asking MediaMTX.
"""

import json
import os
import pathlib
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="mixer_camhls_"))
(_tmp / "config.yaml").write_text("mixer:\n  max_channels: 8\n")
os.environ["MIXER_CONFIG"] = str(_tmp / "config.yaml")
CAM = "cam-0123456789ab"
(_tmp / "camera_streams.json").write_text(json.dumps({"cameras": [
    {"id": CAM, "name": "Cam1", "rtsp_url": "rtsp://192.168.188.51:554/stream1", "username": "", "password": ""},
]}))

from fastapi.testclient import TestClient  # noqa: E402

import app.camera_streams as camera_streams  # noqa: E402
from app.main import app  # noqa: E402

PASS = 0
FAIL = 0
SEEN = []


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


class FakeMediaMTX(BaseHTTPRequestHandler):
    def do_GET(self):
        SEEN.append((self.path, self.headers.get("Cookie")))
        if self.path == f"/{CAM}/index.m3u8":
            # what MediaMTX 1.21 does on a first playlist request
            self.send_response(302)
            self.send_header("Location", f"/{CAM}/index.m3u8?cookieCheck=1")
            self.send_header("Set-Cookie", f"cookieCheck=1; Path=/{CAM}/; HttpOnly; Secure; SameSite=None")
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        if self.path.startswith(f"/{CAM}/index.m3u8"):
            body, kind, status = b"#EXTM3U\n", "application/vnd.apple.mpegurl", 200
        elif self.path == f"/{CAM}/":
            body, kind, status = b"<html>player</html>", "text/html", 200
        else:
            body, kind, status = b"not found", "text/plain", 404
        self.send_response(status)
        self.send_header("Content-Type", kind)
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def main():
    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMediaMTX)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    camera_streams.HLS_PORT = server.server_address[1]

    print("== relay config ==")
    config = camera_streams._relay_config([{"id": CAM, "rtsp_url": "rtsp://192.168.188.51:554/stream1"}])
    check("HLS on localhost only", config["hls"] is True and config["hlsAddress"].startswith("127.0.0.1:"))
    check("WebRTC still on, signalling on localhost", config["webrtc"] is True and config["webrtcAddress"] == "127.0.0.1:8889")
    check("HLS parts of 200 ms in 1 s segments", config["hlsPartDuration"] == "200ms" and config["hlsSegmentDuration"] == "1s")

    with TestClient(app) as client:
        relay = app.state.camera_relay
        relay._has_cameras = False  # no MediaMTX binary in tests: treat the relay as ready
        print("== passed on ==")
        r = client.get(f"/camera-hls/{CAM}/")
        check("player page", r.status_code == 200 and r.text == "<html>player</html>" and r.headers["content-type"].startswith("text/html"))
        r = client.get(f"/camera-hls/{CAM}/index.m3u8", follow_redirects=False)
        check("cookie-check redirect handed to the browser under /camera-hls/",
              r.status_code == 302 and r.headers["location"] == f"/camera-hls/{CAM}/index.m3u8?cookieCheck=1"
              and f"Path=/camera-hls/{CAM}/" in r.headers["set-cookie"])
        r = client.get(f"/camera-hls/{CAM}/index.m3u8?cookieCheck=1", headers={"Cookie": "cookieCheck=1"})
        check("cookie passed on to MediaMTX", r.status_code == 200 and SEEN[-1] == (f"/{CAM}/index.m3u8?cookieCheck=1", "cookieCheck=1"))
        r = client.get(f"/camera-hls/{CAM}/index.m3u8?_HLS_msn=12&_HLS_part=3")
        check("playlist with low-latency query", r.status_code == 200 and r.text == "#EXTM3U\n"
              and SEEN[-1][0] == f"/{CAM}/index.m3u8?_HLS_msn=12&_HLS_part=3"
              and r.headers["content-type"] == "application/vnd.apple.mpegurl" and r.headers["cache-control"] == "no-cache")
        r = client.get(f"/camera-hls/{CAM}/missing.mp4")
        check("upstream 404 passed through", r.status_code == 404 and r.text == "not found")

        import app.camera_hls as camera_hls
        threads = []
        real_fetch = camera_hls._fetch
        camera_hls._fetch = lambda *args: (threads.append(threading.current_thread().name), real_fetch(*args))[1]
        client.get(f"/camera-hls/{CAM}/index.m3u8?_HLS_msn=1")
        camera_hls._fetch = real_fetch
        check("runs on its own threads (never blocks the lights/OBS pool)",
              bool(threads) and threads[0].startswith("camera-hls"))

        print("== refused ==")
        before = len(SEEN)
        check("unknown camera", client.get("/camera-hls/cam-ffffffffffff/").status_code == 404)
        check("not a camera path", client.get("/camera-hls/v3/config/global/get").status_code == 404)
        check("parent path", client.get(f"/camera-hls/{CAM}/..%2F..%2Fsecret").status_code == 404)
        check("MediaMTX not asked for refused paths", len(SEEN) == before)
        relay._has_cameras = True  # cameras configured but no MediaMTX process
        r = client.get(f"/camera-hls/{CAM}/")
        check("relay not ready -> 503", r.status_code == 503 and r.json()["reason"])
    server.shutdown()

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
