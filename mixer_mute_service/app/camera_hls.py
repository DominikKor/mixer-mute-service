"""Camera pictures for devices that cannot reach the WebRTC ports.

The web UI shows the cameras through MediaMTX. Its WebRTC player needs TCP
8889 and UDP 8189 on the streaming PC, which that PC's firewall may block for
everyone but itself. MediaMTX therefore also serves the cameras as HLS, bound
to 127.0.0.1 only (see _relay_config), and GET /camera-hls/<camera id>/...
passes those requests through the service's own port, which every device on
the LAN reaches. Only paths of configured cameras are passed on.

MediaMTX answers a first playlist request with a redirect to
"?cookieCheck=1" (and a cookie it does not insist on). Redirects are passed
to the browser, with the path moved under /camera-hls/, instead of being
followed here; cookies go both ways.
"""

from __future__ import annotations

import asyncio
import logging
import re
import urllib.error
import urllib.request
from typing import Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from . import camera_streams

router = APIRouter()
log = logging.getLogger("mixer.camera_hls")

_PATH_RE = re.compile(r"^(cam-[0-9a-f]{12})(?:/[A-Za-z0-9._\-/]*)?$")
PREFIX = "/camera-hls"
# Low-latency HLS holds a playlist request open until the next part exists.
_TIMEOUT = 20


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None  # hand the redirect to the browser


_opener = urllib.request.build_opener(_NoRedirect)


def _fetch(url: str, cookie: str) -> Tuple[int, list, bytes]:
    request = urllib.request.Request(url, headers={"Cookie": cookie} if cookie else {})
    try:
        with _opener.open(request, timeout=_TIMEOUT) as response:
            return response.status, response.headers.items(), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.headers.items() if exc.headers else [], exc.read() if exc.fp else b""


def _rewrite_path(value: str) -> str:
    """Absolute MediaMTX paths ("/cam-.../...") live under /camera-hls here."""
    return PREFIX + value if value.startswith("/") else value


@router.get("/camera-hls/{path:path}")
async def camera_hls(path: str, request: Request):
    match = _PATH_RE.match(path)
    if not match or ".." in path or "//" in path:
        return JSONResponse({"ok": False, "reason": "unknown camera path"}, 404)
    cameras = await request.app.state.camera_streams.public_snapshot()
    if match.group(1) not in {camera["id"] for camera in cameras}:
        return JSONResponse({"ok": False, "reason": "unknown camera"}, 404)
    relay = request.app.state.camera_relay
    if not relay.ready:
        return JSONResponse({"ok": False, "reason": relay.reason or "camera relay not ready"}, 503)
    query = request.url.query
    url = f"http://127.0.0.1:{camera_streams.HLS_PORT}/{path}" + (f"?{query}" if query else "")
    try:
        status, headers, body = await asyncio.to_thread(_fetch, url, request.headers.get("cookie", ""))
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("Camera HLS relay unavailable: %s", exc)
        return JSONResponse({"ok": False, "reason": "camera relay unavailable"}, 502)
    response = Response(body, status_code=status)
    for name, value in headers:
        if name.lower() in ("content-type", "cache-control"):
            response.headers[name] = value
        elif name.lower() == "location":
            response.headers["Location"] = _rewrite_path(value)
        elif name.lower() == "set-cookie":
            response.headers.append("Set-Cookie", re.sub(
                r"(?i)(;\s*path=)(/[^;]*)", lambda m: m.group(1) + _rewrite_path(m.group(2)), value))
    return response
