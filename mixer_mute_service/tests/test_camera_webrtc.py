"""WebRTC camera signalling through the service's port (app/camera_webrtc.py).

MediaMTX is faked. Verifies: the relay config keeps the WebRTC signalling on
127.0.0.1 and offers media over UDP and TCP 8189; a WHEP offer for a
configured camera reaches MediaMTX unchanged (body, content type) and its
answer comes back with the session location moved under /camera-webrtc/;
PATCH (with If-Match) and DELETE reach that session; upstream errors pass
through; unknown cameras, odd session ids, oversized offers, other methods
and a relay that is not ready are refused without asking MediaMTX. The
player page loads from the service, points at these routes, falls back to
HLS, and its script parses; index.html uses it for every device.
"""

import json
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from tests.helpers import use_app_config  # noqa: E402

# Mixer pointed at a closed local port: the test never reaches the console.
_tmp = use_app_config("mixer_camwebrtc_")
CAM = "cam-0123456789ab"
SESSION = "5b6c7d8e-1111-2222-3333-444455556666"
(_tmp / "camera_streams.json").write_text(json.dumps({"cameras": [
    {"id": CAM, "name": "Cam1", "rtsp_url": "rtsp://192.168.5.51:554/stream1", "username": "", "password": ""},
]}))

from fastapi.testclient import TestClient  # noqa: E402

import app.camera_streams as camera_streams  # noqa: E402
from app.main import STATIC_DIR, app  # noqa: E402

PASS = 0
FAIL = 0
SEEN = []
OFFER = "v=0\r\no=- 1 2 IN IP4 127.0.0.1\r\ns=-\r\nm=video 9 UDP/TLS/RTP/SAVPF 96\r\n"
ANSWER = "v=0\r\no=- 3 4 IN IP4 127.0.0.1\r\ns=-\r\na=candidate:1 1 UDP 1 192.168.188.20 8189 typ host\r\n"


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


class FakeMediaMTX(BaseHTTPRequestHandler):
    def _record(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode() if length else ""
        SEEN.append((self.command, self.path, self.headers.get("Content-Type"), self.headers.get("If-Match"), body))
        return body

    def _reply(self, status, body=b"", headers=()):
        self.send_response(status)
        for name, value in headers:
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        self._record()
        if self.path == f"/{CAM}/whep":
            self._reply(201, ANSWER.encode(), [
                ("Content-Type", "application/sdp"),
                ("Location", f"/{CAM}/whep/{SESSION}"),
                ("ETag", "*"),
                ("Accept-Patch", "application/trickle-ice-sdpfrag"),
                ("Set-Cookie", "upstream=1"),
            ])
        else:
            self._reply(404, b"not found", [("Content-Type", "text/plain")])

    def do_PATCH(self):
        self._record()
        self._reply(204 if self.path == f"/{CAM}/whep/{SESSION}" else 404)

    def do_DELETE(self):
        self._record()
        self._reply(200 if self.path == f"/{CAM}/whep/{SESSION}" else 404)

    def log_message(self, *args):
        pass


def main():
    print("== relay config ==")
    config = camera_streams._relay_config([{"id": CAM, "rtsp_url": "rtsp://192.168.5.51:554/stream1"}])
    check("signalling on localhost only", config["webrtcAddress"] == "127.0.0.1:8889")
    check("media from the LAN over UDP and TCP 8189",
          config["webrtcLocalUDPAddress"] == ":8189" and config["webrtcLocalTCPAddress"] == ":8189")
    check("HLS fallback still on localhost", config["hls"] is True and config["hlsAddress"].startswith("127.0.0.1:"))

    server = ThreadingHTTPServer(("127.0.0.1", 0), FakeMediaMTX)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    camera_streams.WEBRTC_PORT = server.server_address[1]

    with TestClient(app) as client:
        relay = app.state.camera_relay
        relay._has_cameras = False  # no MediaMTX binary in tests: treat the relay as ready
        print("== passed on ==")
        r = client.post(f"/camera-webrtc/{CAM}/whep", content=OFFER, headers={"Content-Type": "application/sdp"})
        check("offer answered (201, SDP)", r.status_code == 201 and r.text == ANSWER
              and r.headers["content-type"] == "application/sdp")
        check("offer reached MediaMTX unchanged",
              SEEN[-1] == ("POST", f"/{CAM}/whep", "application/sdp", None, OFFER))
        check("session location under /camera-webrtc/",
              r.headers.get("location") == f"/camera-webrtc/{CAM}/whep/{SESSION}")
        check("ETag and Accept-Patch passed on", r.headers.get("etag") == "*"
              and r.headers.get("accept-patch") == "application/trickle-ice-sdpfrag")
        check("upstream cookies not passed on", "set-cookie" not in r.headers)

        fragment = "a=ice-ufrag:abcd\r\na=candidate:1 1 udp 1 192.168.188.40 50000 typ host\r\n"
        r = client.patch(f"/camera-webrtc/{CAM}/whep/{SESSION}", content=fragment,
                         headers={"Content-Type": "application/trickle-ice-sdpfrag", "If-Match": "*"})
        check("trickle PATCH reaches the session", r.status_code == 204 and SEEN[-1] == (
            "PATCH", f"/{CAM}/whep/{SESSION}", "application/trickle-ice-sdpfrag", "*", fragment))
        r = client.delete(f"/camera-webrtc/{CAM}/whep/{SESSION}")
        check("DELETE closes the session", r.status_code == 200 and SEEN[-1][:2] == ("DELETE", f"/{CAM}/whep/{SESSION}"))
        r = client.delete(f"/camera-webrtc/{CAM}/whep/aaaaaaaa-0000")
        check("upstream 404 passed through", r.status_code == 404)

        import app.camera_webrtc as camera_webrtc
        threads = []
        real_fetch = camera_webrtc._fetch
        camera_webrtc._fetch = lambda *args: (threads.append(threading.current_thread().name), real_fetch(*args))[1]
        client.post(f"/camera-webrtc/{CAM}/whep", content=OFFER, headers={"Content-Type": "application/sdp"})
        camera_webrtc._fetch = real_fetch
        check("runs on its own threads", bool(threads) and threads[0].startswith("camera-webrtc"))

        print("== refused ==")
        before = len(SEEN)
        check("unknown camera", client.post("/camera-webrtc/cam-ffffffffffff/whep", content=OFFER).status_code == 404)
        check("not a camera id", client.post("/camera-webrtc/v3/whep", content=OFFER).status_code == 404)
        check("odd session id", client.delete(f"/camera-webrtc/{CAM}/whep/..%2F..%2Fv3").status_code in (404, 405))
        check("session id with dots", client.delete(f"/camera-webrtc/{CAM}/whep/a.b").status_code == 404)
        check("oversized offer -> 413",
              client.post(f"/camera-webrtc/{CAM}/whep", content="x" * (70 * 1024)).status_code == 413)
        check("GET of the WHEP endpoint not offered", client.get(f"/camera-webrtc/{CAM}/whep").status_code in (404, 405))
        check("MediaMTX not asked for refused requests", len(SEEN) == before)
        relay._has_cameras = True  # cameras configured but no MediaMTX process
        r = client.post(f"/camera-webrtc/{CAM}/whep", content=OFFER)
        check("relay not ready -> 503", r.status_code == 503 and r.json()["reason"])
        check("MediaMTX still not asked", len(SEEN) == before)
        relay._has_cameras = False

        print("== player page ==")
        r = client.get(f"/camera-player.html?cam={CAM}")
        page = r.text
        check("served by the service, revalidated", r.status_code == 200 and r.headers.get("cache-control") == "no-cache")
        check("offers via /camera-webrtc/, falls back to /camera-hls/",
              '"/camera-webrtc/"' in page and '"/whep"' in page and '"/camera-hls/"' in page)
        check("muted inline autoplay (iPad Safari)", re.search(r"<video[^>]*\bmuted\b[^>]*\bautoplay\b[^>]*\bplaysinline\b", page) is not None)
        index = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
        check("index.html uses the player for every device",
              "/camera-player.html?cam=" in index and ":8889/" not in index)

    script = re.search(r"<script>(.*?)</script>", page, re.S).group(1)
    try:
        import quickjs
    except ImportError:
        print("  skip player script syntax (quickjs not installed)")
    else:
        context = quickjs.Context()
        try:
            # Compile only: wrap in a function that is never called.
            context.eval("(function () {" + script + "\n});")
            parsed = True
        except quickjs.JSException as exc:
            print("   ", exc)
            parsed = False
        check("player script parses", parsed)
    server.shutdown()

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
