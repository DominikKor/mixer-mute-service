"""Cameras: the stream list (MediaMTX relay) and hold-to-move PTZ."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from ..camera_streams import CameraStreamError
from ..models import CameraPanTiltRequest, CameraStreamRequest, CameraZoomRequest
from ..native_controls import VISCA_CAMERAS, NativeControlError
from ..responses import fail

router = APIRouter()
log = logging.getLogger("mixer.api.cameras")


async def _relay_status(request: Request) -> dict:
    """Reconfigure the relay after a change; its state goes into the answer."""
    status = await request.app.state.camera_relay.configure(
        await request.app.state.camera_streams.relay_snapshot()
    )
    return {"relay_ready": status["ready"], "reason": status["reason"]}


@router.get("/camera-streams")
async def camera_streams(request: Request):
    relay = request.app.state.camera_relay
    return {
        "ok": True,
        "relay_ready": relay.ready,
        "reason": relay.reason,
        "cameras": await request.app.state.camera_streams.public_snapshot(),
    }


@router.post("/camera-streams")
async def camera_stream_create(body: CameraStreamRequest, request: Request):
    try:
        camera = await request.app.state.camera_streams.add(body.name, body.rtsp_url, body.username, body.password)
    except CameraStreamError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "camera": camera, **await _relay_status(request)}


@router.put("/camera-streams/{camera_id}")
async def camera_stream_update(camera_id: str, body: CameraStreamRequest, request: Request):
    try:
        camera = await request.app.state.camera_streams.update(
            camera_id, body.name, body.rtsp_url, body.username, body.password
        )
    except KeyError:
        return fail("unknown camera", 404)
    except CameraStreamError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "camera": camera, **await _relay_status(request)}


@router.delete("/camera-streams/{camera_id}")
async def camera_stream_delete(camera_id: str, request: Request):
    try:
        await request.app.state.camera_streams.delete(camera_id)
    except KeyError:
        return fail("unknown camera", 404)
    return {"ok": True, "deleted": True, **await _relay_status(request)}


@router.post("/cameras/{camera}/zoom")
async def camera_zoom(camera: str, body: CameraZoomRequest, request: Request):
    """Hold-to-zoom for the camera section: the catalog's zoom keys only start
    a zoom, which then runs to the end stop; the UI sends "stop" on release."""
    if camera not in VISCA_CAMERAS:
        return fail("unknown camera", 404)
    action = {"type": "visca-zoom", "camera": camera, "direction": body.direction}
    try:
        result = await request.app.state.native_controls.press(action)
    except NativeControlError as exc:
        log.warning("Camera zoom failed camera=%s direction=%s reason=%s", camera, body.direction, exc)
        return fail(str(exc), 503, camera=camera)
    return {**result, "camera": camera, "direction": body.direction}


@router.post("/cameras/{camera}/pan-tilt")
async def camera_pan_tilt(camera: str, body: CameraPanTiltRequest, request: Request):
    """Hold-to-move for the arrow keys of the camera section. The UI repeats
    the direction as a keep-alive and sends "stop" on release; without either
    the service stops the camera after a few seconds (app/camera_ptz.py)."""
    if camera not in VISCA_CAMERAS:
        return fail("unknown camera", 404)
    try:
        result = await request.app.state.camera_ptz.drive(camera, body.direction)
    except NativeControlError as exc:
        log.warning("Camera pan-tilt failed camera=%s direction=%s reason=%s", camera, body.direction, exc)
        return fail(str(exc), 503, camera=camera)
    return {**result, "camera": camera, "direction": body.direction}
