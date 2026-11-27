"""Cameras: the stream list (MediaMTX relay), hold-to-move PTZ, and VISCA
presets by number (recall any number, save the current position)."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Request

from ..actions import press_native
from ..camera_preset_usage import camera_preset_usage
from ..camera_streams import CameraStreamError
from ..companion_buttons import CATALOG_PRESET_IDS, clean_label, saved_preset_id
from ..models import (CameraPanTiltRequest, CameraPresetRecallRequest, CameraPresetSaveRequest,
                      CameraStreamRequest, CameraZoomRequest)
from ..native_controls import VISCA_CAMERAS, NativeControlError
from ..responses import fail, reply

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


def _preset_name(label: str) -> str:
    """ "CAM1 Kanzel" -> "Kanzel" (the camera section shows the name only)."""
    words = label.split(" ", 1)
    return words[1] if len(words) == 2 and words[0].lower().startswith("cam") else label


@router.post("/cameras/{camera}/preset/recall")
async def camera_preset_recall(camera: str, body: CameraPresetRecallRequest, request: Request):
    """Drive to a preset by number, also one without a name in the app. Runs
    exactly as a preset key (POST /companion/buttons/{id}/press)."""
    if camera not in VISCA_CAMERAS:
        return fail("unknown camera", 404)
    button_id = CATALOG_PRESET_IDS.get((camera, body.preset), saved_preset_id(camera, body.preset))
    action = {"type": "visca-preset", "camera": camera, "preset": body.preset}
    result, status = await press_native(request, button_id, action)
    return reply({**result, "camera": camera, "preset": body.preset}, status)


@router.get("/cameras/{camera}/presets/usage")
async def camera_preset_usage_route(camera: str, request: Request):
    """Which numbers program rows, shortcuts, templates and Steuerung keys
    use: {"used": {"3": ["Taste: Musik Klavier", ...]}}. The save dialog
    suggests a number nothing uses."""
    if camera not in VISCA_CAMERAS:
        return fail("unknown camera", 404)
    used = await camera_preset_usage(request.app.state, camera)
    return {"ok": True, "camera": camera, "used": {str(n): labels for n, labels in used.items()}}


def _preset_lock(request: Request, camera: str) -> asyncio.Lock:
    locks = request.app.state.camera_preset_locks
    return locks.setdefault(camera, asyncio.Lock())


@router.post("/cameras/{camera}/presets")
async def camera_preset_save(camera: str, body: CameraPresetSaveRequest, request: Request):
    """Store the camera's current position as preset number N, then name it
    in the app. A number that has a name here or that a program row,
    shortcut, template or Steuerung key uses is only overwritten with
    "overwrite": true; otherwise 409 says its name and who uses it. The camera
    may still hold a position at a number nothing here knows (Companion,
    remote control): the dialog says "in der App ohne Namen", not "frei"."""
    store = request.app.state.companion_button_store
    if store is None:
        return fail("control keys are not available", 503)
    if camera not in VISCA_CAMERAS:
        return fail("unknown camera", 404)
    try:
        name = clean_label(body.name)
    except ValueError as exc:
        return fail(str(exc), 400)
    if not name:
        return fail("a preset needs a name", 400)
    # Check, store on the camera and name it as one step per camera: two
    # saves at once cannot both pass the check.
    async with _preset_lock(request, camera):
        existing = await store.camera_preset(camera, body.preset)
        users = (await camera_preset_usage(request.app.state, camera)).get(body.preset, [])
        if (existing is not None or users) and not body.overwrite:
            return fail(f"preset {body.preset} is in use", 409, exists=True, preset=body.preset,
                        label=existing["label"] if existing else None,
                        name=_preset_name(existing["label"]) if existing else None, users=users)
        try:  # the name first: a refused name must not move the camera's preset
            await store.check_preset_label(camera, body.preset, f"{camera.upper()} {name}")
        except ValueError as exc:
            return fail(str(exc), 400)
        try:
            await request.app.state.native_controls.store_visca_preset(camera, body.preset)
        except NativeControlError as exc:
            log.warning("Camera preset store failed camera=%s preset=%s reason=%s", camera, body.preset, exc)
            return fail(str(exc), 503, camera=camera, preset=body.preset)
        try:
            button = await store.save_camera_preset(camera, body.preset, f"{camera.upper()} {name}")
        except ValueError as exc:
            return fail(str(exc), 400)
    overwritten = existing is not None or bool(users)
    log.info("Camera preset stored camera=%s preset=%s name=%r overwrite=%s",
             camera, body.preset, name, overwritten)
    return {"ok": True, "camera": camera, "preset": body.preset, "button": button,
            "overwritten": overwritten}
