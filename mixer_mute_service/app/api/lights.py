"""Licht: stage zones, room light, scenes (app/lights.py has the logic).

GET    /lights                       stage, circuits (on/off/null), scenes
POST   /lights/stage {zones, warm, cold, fade}   absolute values, faded
                                     unless fade is false (slider drags)
PUT    /lights/settings {start, steps, fade_ms}
POST   /lights/circuits {states: {target: bool}} room light on/off; answers
                                     with the circuits' states ({target: bool|null})
GET    /lights/scenes                    only the scenes (no device asked)
POST   /lights/scenes {name, replace, lights: {zones, color, circuits}}
                                     save the current light of the chosen
                                     lights (without lights: all of them)
DELETE /lights/scenes/{id}
POST   /lights/scenes/{id}/apply
GET    /lights/artnet                    ask the stage node (ArtPoll): name and
                                     whether it merges a second controller
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, Request

from ..lights import SceneError, apply_scene
from ..native_controls import (
    ARTNET_HOST, ARTNET_ZONE_LABELS, ARTNET_ZONES, SHELLY_HOSTS, SHELLY_LABELS, NativeControlError, artnet_status,
)
from ..responses import fail, reply

router = APIRouter()
log = logging.getLogger("mixer.lights")


def _native(request: Request):
    return request.app.state.native_controls


def _unavailable():
    return fail("light control is not available", 503)


async def _view(request: Request, fresh: bool = False) -> dict:
    native = _native(request)
    circuits = await request.app.state.light_circuits.get(native, fresh=fresh)
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


@router.get("/lights/artnet")
async def lights_artnet(request: Request):
    """Ask the stage's Art-Net node who it is and whether it merges: if
    another controller (an old Companion, a lighting desk) also sends to it,
    the stage jumps between both sources' values."""
    if _native(request) is None:
        return _unavailable()
    try:
        node = await asyncio.to_thread(artnet_status)
    except (NativeControlError, OSError) as exc:
        return fail(f"Art-Net node not asked: {exc}", 502, host=ARTNET_HOST)
    if node is None:
        return fail("Art-Net node did not answer the ArtPoll", 504, host=ARTNET_HOST)
    merging = any(output["merging"] for output in node["outputs"])
    if merging:
        log.warning("Art-Net node %s merges two sources: another controller sends to the stage", ARTNET_HOST)
    return {"ok": True, "host": ARTNET_HOST, "node": node, "merging": merging}


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
    cache = request.app.state.light_circuits
    cache.update({k: v for k, v in states.items() if k not in errors})
    # The confirmed states, so the page shows them without asking again.
    circuits = cache.snapshot()
    if errors:
        return fail("room light not switched everywhere", 502, errors=errors, circuits=circuits)
    return {"ok": True, "circuits": circuits}


def _chosen_lights(lights):
    """The lights a new scene controls: (zones, colour, circuits), or the
    reason it is invalid. Without a choice: every light (older callers)."""
    if lights is None:
        return list(ARTNET_ZONES), True, list(SHELLY_HOSTS)
    if not isinstance(lights, dict):
        return "lights must be {zones, color, circuits}"
    zones, color, circuits = lights.get("zones") or [], lights.get("color", False), lights.get("circuits") or []
    if not isinstance(zones, list) or any(z not in ARTNET_ZONES for z in zones):
        return "unknown stage zone in lights"
    if not isinstance(circuits, list) or any(c not in SHELLY_HOSTS for c in circuits):
        return "unknown room light circuit in lights"
    if not isinstance(color, bool):
        return "lights.color must be true or false"
    if not zones and not color and not circuits:
        return "choose at least one light for the scene"
    return zones, color, circuits


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
    chosen = _chosen_lights(body.get("lights"))
    if isinstance(chosen, str):
        return fail(chosen, 400)
    zones, color, circuits = chosen
    view = await _view(request, fresh=True)  # save what the room light is now
    stage = view["stage"]
    states = {c["id"]: c["on"] for c in view["circuits"] if c["id"] in circuits}
    entry = {
        "name": body.get("name"),
        "stage": {"zones": {z: stage["zones"][z] for z in zones},
                  "warm": stage["warm"] if color else None, "cold": stage["cold"] if color else None},
        "circuits": {k: on for k, on in states.items() if isinstance(on, bool)},
    }
    try:
        scene = await scenes.save(entry, replace=body.get("replace") is True)
    except KeyError:
        return fail("a light scene with this name already exists", 409)
    except SceneError as exc:
        return fail(str(exc), 400, unknown_circuits=[k for k, on in states.items() if on is None])
    unknown = [k for k, on in states.items() if on is None]
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
