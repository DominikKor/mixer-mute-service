"""WebRTC camera pictures for every device, signalled through the service port.

Low delay needs WebRTC; HLS (app/camera_hls.py) buffers whole parts and lags
seconds behind. WebRTC has two halves:

* Signalling (WHEP: one HTTP POST with the browser's SDP offer, optional
  PATCH for late ICE candidates, DELETE on close). MediaMTX serves it on
  127.0.0.1 only (see camera_streams._relay_config); the routes below pass it
  through the service's own port 8765, which every device on the LAN reaches.
  No firewall rule for TCP 8889 is needed any more.
* Media (RTP over ICE): MediaMTX sends it from port 8189, over UDP and, where
  UDP is dropped, over TCP. That one port needs the Windows firewall rules
  from install-service.ps1. If it is not reachable anyway, the player page
  (static/camera-player.html) falls back to HLS by itself.

Only the WHEP endpoints of configured cameras are passed on.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from typing import Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import camera_streams

router = APIRouter()
log = logging.getLogger("mixer.camera_webrtc")

PREFIX = "/camera-webrtc"
_CAMERA_RE = re.compile(r"^cam-[0-9a-f]{12}$")
_SESSION_RE = re.compile(r"^[0-9A-Za-z-]{1,64}$")
_MAX_BODY = 64 * 1024  # an SDP offer is a few kB
# MediaMTX answers the offer only once the camera's RTSP source is running
# (sourceOnDemand), which can take a few seconds after the first viewer.
_TIMEOUT = 15
_PASS_HEADERS = ("content-type", "etag", "accept-patch", "link", "cache-control")
_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="camera-webrtc")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


_opener = urllib.request.build_opener(_NoRedirect)


def _fetch(method: str, url: str, headers: dict, body: Optional[bytes]) -> Tuple[int, list, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with _opener.open(request, timeout=_TIMEOUT) as response:
            return response.status, response.headers.items(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.items() if exc.headers else [], exc.read() if exc.fp else b""


async def _forward(request: Request, camera_id: str, upstream_path: str) -> Response:
    if not _CAMERA_RE.match(camera_id):
        return JSONResponse({"ok": False, "reason": "unknown camera path"}, 404)
    cameras = await request.app.state.camera_streams.public_snapshot()
    if camera_id not in {camera["id"] for camera in cameras}:
        return JSONResponse({"ok": False, "reason": "unknown camera"}, 404)
    relay = request.app.state.camera_relay
    if not relay.ready:
        return JSONResponse({"ok": False, "reason": relay.reason or "camera relay not ready"}, 503)
    body = None
    if request.method in ("POST", "PATCH"):
        length = request.headers.get("content-length")
        if length is not None and (not length.isdigit() or int(length) > _MAX_BODY):
            return JSONResponse({"ok": False, "reason": "request too large"}, 413)
        body = await request.body()
        if len(body) > _MAX_BODY:
            return JSONResponse({"ok": False, "reason": "request too large"}, 413)
    headers = {
        name: request.headers[name]
        for name in ("content-type", "if-match")
        if name in request.headers
    }
    url = f"http://127.0.0.1:{camera_streams.WEBRTC_PORT}{upstream_path}"
    try:
        status, upstream_headers, payload = await asyncio.get_running_loop().run_in_executor(
            _POOL, _fetch, request.method, url, headers, body
        )
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("Camera WebRTC signalling unavailable: %s", exc)
        return JSONResponse({"ok": False, "reason": "camera relay unavailable"}, 502)
    response = Response(payload, status_code=status)
    for name, value in upstream_headers:
        lower = name.lower()
        if lower in _PASS_HEADERS:
            response.headers[name] = value
        elif lower == "location" and value.startswith(f"/{camera_id}/whep/"):
            # MediaMTX names the session "/<camera>/whep/<id>"; it lives under
            # /camera-webrtc here. Any other location is not passed on.
            response.headers["Location"] = PREFIX + value
    return response


@router.post("/camera-webrtc/{camera_id}/whep")
async def whep_offer(camera_id: str, request: Request):
    return await _forward(request, camera_id, f"/{camera_id}/whep")


@router.api_route("/camera-webrtc/{camera_id}/whep/{session}", methods=["PATCH", "DELETE"])
async def whep_session(camera_id: str, session: str, request: Request):
    if not _SESSION_RE.match(session):
        return JSONResponse({"ok": False, "reason": "unknown session"}, 404)
    return await _forward(request, camera_id, f"/{camera_id}/whep/{session}")
