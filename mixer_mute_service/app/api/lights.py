"""Licht: stage zones, room light, scenes (app/lights.py has the logic).

GET    /lights                       stage, circuits (on/off/null), scenes
POST   /lights/stage {zones, warm, cold, fade}   absolute values, faded
                                     unless fade is false (slider drags)
PUT    /lights/settings {start, steps, fade_ms}
POST   /lights/circuits {states: {target: bool}} room light on/off
GET    /lights/scenes                    only the scenes (no device asked)
POST   /lights/scenes {name, replace}            save the current light
DELETE /lights/scenes/{id}
POST   /lights/scenes/{id}/apply
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Request

from ..lights import SceneError, apply_scene
from ..native_controls import ARTNET_ZONE_LABELS, SHELLY_HOSTS, SHELLY_LABELS, NativeControlError
from ..responses import fail, reply

router = APIRouter()
log = logging.getLogger("mixer.lights")


def _native(request: Request):
    return request.app.state.native_controls


def _unavailable():
    return fail("light control is not available", 503)


async def _view(request: Request) -> dict:
    native = _native(request)
    circuits = await request.app.state.light_circuits.get(native)
    scenes = request.app.state.light_scenes
    store = request.app.state.names
    zone_names = store.get("light-zone") if store else {}
    circuit_names = store.get("light-circuit") if store else {}
    return {
        "ok": True,
        "stage": native.stage_state(),
        # The user's names (app/names.py), else the built-in ones.
        "zone_labels": {k: zone_names.get(k) or v for k, v in ARTNET_ZONE_LABELS.items()},
        "zone_defaults": ARTNET_ZONE_LABELS,
        "circuits": [{"id": k, "label": circuit_names.get(k) or SHELLY_LABELS.get(k, k),
                      "default_label": SHELLY_LABELS.get(k, k), "on": circuits.get(k)} for k in SHELLY_HOSTS],
        "scenes": await scenes.list() if scenes is not None else [],
    }


@router.get("/lights")
async def lights(request: Request):
    if _native(request) is None:
        return _unavailable()
    return await _view(request)


@router.post("/lights/stage")
async def lights_stage(request: Request, body: dict = Body(...)):
    native = _native(request)
    if native is None:
        return _unavailable()
    zones = body.get("zones")
    if zones is not None and not isinstance(zones, dict):
        return fail("zones must be {zone: level}", 400)
    try:
        stage = await native.set_stage(zones, body.get("warm"), body.get("cold"), fade=body.get("fade") is not False)
    except NativeControlError as exc:
        return fail(str(exc), 400 if "must be" in str(exc) or "unknown" in str(exc) else 502)
    except OSError as exc:
        return fail(f"Art-Net send failed: {exc}", 502)
    return {"ok": True, "stage": stage}


@router.put("/lights/settings")
async def lights_settings(request: Request, body: dict = Body(...)):
    native = _native(request)
    if native is None:
        return _unavailable()
    try:
        stage = await native.set_light_settings(body)
    except NativeControlError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "stage": stage}


@router.post("/lights/circuits")
async def lights_circuits(request: Request, body: dict = Body(...)):
    native = _native(request)
    if native is None:
        return _unavailable()
    states = body.get("states")
    if not isinstance(states, dict):
        return fail("states must be {circuit: true|false}", 400)
    try:
        errors = await native.set_circuits(states)
    except NativeControlError as exc:
        return fail(str(exc), 400)
    request.app.state.light_circuits.update({k: v for k, v in states.items() if k not in errors})
    if errors:
        return fail("room light not switched everywhere", 502, errors=errors)
    return {"ok": True}


@router.get("/lights/scenes")
async def lights_scenes(request: Request):
    """Only the scenes (program rows): no device is asked."""
    scenes = request.app.state.light_scenes
    return {"ok": True, "scenes": await scenes.list() if scenes is not None else []}


@router.post("/lights/scenes")
async def lights_scene_save(request: Request, body: dict = Body(...)):
    native, scenes = _native(request), request.app.state.light_scenes
    if native is None or scenes is None:
        return _unavailable()
    request.app.state.light_circuits.forget()  # save what the room light is now
    view = await _view(request)
    stage = view["stage"]
    entry = {
        "name": body.get("name"),
        "stage": {"zones": stage["zones"], "warm": stage["warm"], "cold": stage["cold"]},
        "circuits": {c["id"]: c["on"] for c in view["circuits"] if isinstance(c["on"], bool)},
    }
    try:
        scene = await scenes.save(entry, replace=body.get("replace") is True)
    except KeyError:
        return fail("a light scene with this name already exists", 409)
    except SceneError as exc:
        return fail(str(exc), 400)
    unknown = [c["id"] for c in view["circuits"] if c["on"] is None]
    log.info("Light scene %r saved (%s room circuits unknown)", scene["name"], len(unknown))
    return {"ok": True, "scene": scene, "unknown_circuits": unknown}


@router.delete("/lights/scenes/{scene_id}")
async def lights_scene_delete(scene_id: str, request: Request):
    scenes = request.app.state.light_scenes
    if scenes is None or not await scenes.delete(scene_id):
        return fail("unknown light scene", 404)
    return {"ok": True}


@router.post("/lights/scenes/{scene_id}/apply")
async def lights_scene_apply(scene_id: str, request: Request):
    return reply(*await apply_scene(request, scene_id))
