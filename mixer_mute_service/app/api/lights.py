"""Licht: stage zones, room light, scenes (app/lights.py has the logic).

GET    /lights                       stage, circuits (on/off/null, stale), scenes
POST   /lights/stage {zones, warm, cold, fade}   zones: {zone: {on, level,
                                     warm, cold}} (any of them) or {zone:
                                     level} (0 = off, the level is kept);
                                     warm/cold for all; faded unless fade
                                     is false (slider drags)
PUT    /lights/settings {start, steps, fade_ms}
POST   /lights/circuits {states: {target: bool}} room light on/off; answers
                                     with the circuits' states ({target: bool|null})
GET    /lights/scenes                    only the scenes (no device asked)
POST   /lights/scenes {name, replace, id, lights: {zones, circuits}}
                                     save the current light of the chosen
                                     lights (without lights: all of them);
                                     with id: update that scene
POST   /lights/scenes/order {ids}       new order of the scenes
DELETE /lights/scenes/{id}
POST   /lights/scenes/{id}/apply
GET    /lights/artnet                    ask the stage node (ArtPoll): name and
                                     whether it merges a second controller;
                                     "sending": whether this service holds
                                     the stage (frame resent every second)
"""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, Request

from ..lights import SceneError, apply_scene, scene_is_active
from ..native_controls import (
    ARTNET_HOST, ARTNET_ZONE_LABELS, ARTNET_ZONES, SHELLY_HOSTS, SHELLY_LABELS, InvalidLightValue, NativeControlError,
    artnet_status,
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
    stale = request.app.state.light_circuits.stale()
    scenes = request.app.state.light_scenes
    store = request.app.state.names
    zone_names = store.get("light-zone") if store else {}
    circuit_names = store.get("light-circuit") if store else {}
    stage = native.stage_state()
    scene_list = await scenes.list() if scenes is not None else []
    for scene in scene_list:
        scene["active"] = scene_is_active(scene, stage, circuits)
    return {
        "ok": True,
        "stage": stage,
        # The user's names (app/names.py), else the built-in ones.
        "zone_labels": {k: zone_names.get(k) or v for k, v in ARTNET_ZONE_LABELS.items()},
        "zone_defaults": ARTNET_ZONE_LABELS,
        # stale: the last question(s) got no answer; "on" is the last state.
        "circuits": [{"id": k, "label": circuit_names.get(k) or SHELLY_LABELS.get(k, k),
                      "default_label": SHELLY_LABELS.get(k, k), "on": circuits.get(k),
                      "stale": k in stale} for k in SHELLY_HOSTS],
        "scenes": scene_list,
        # The scene applied last (from this page, a program row, ...).
        "last_scene": getattr(request.app.state, "light_last_scene", None),
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
    except InvalidLightValue as exc:  # refused, nothing sent
        return fail(str(exc), 400)
    except NativeControlError as exc:
        return fail(str(exc), 502)
    except OSError as exc:
        return fail(f"Art-Net send failed: {exc}", 502)
    return {"ok": True, "stage": stage}


@router.get("/lights/artnet")
async def lights_artnet(request: Request):
    """Ask the stage's Art-Net node who it is and whether it merges: if
    another controller (an old Companion, a lighting desk) also sends to it,
    the stage jumps between both sources' values."""
    native = _native(request)
    if native is None:
        return _unavailable()
    # Whether this service holds the stage (resends its frame every second).
    sending = native.keepalive_status()
    try:
        node = await asyncio.to_thread(artnet_status)
    except (NativeControlError, OSError) as exc:
        return fail(f"Art-Net node not asked: {exc}", 502, host=ARTNET_HOST, sending=sending)
    if node is None:
        return fail("Art-Net node did not answer the ArtPoll", 504, host=ARTNET_HOST, sending=sending)
    merging = any(output["merging"] for output in node["outputs"])
    if merging:
        log.warning("Art-Net node %s merges two sources: another controller sends to the stage", ARTNET_HOST)
    return {"ok": True, "host": ARTNET_HOST, "node": node, "merging": merging, "sending": sending}


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
    """The lights a new scene controls: (zones, circuits), or the reason it
    is invalid. Without a choice: every light (older callers). Each chosen
    stage light is saved with its colour ("color" of older pages is not
    needed any more and ignored)."""
    if lights is None:
        return list(ARTNET_ZONES), list(SHELLY_HOSTS)
    if not isinstance(lights, dict):
        return "lights must be {zones, circuits}"
    zones, circuits = lights.get("zones") or [], lights.get("circuits") or []
    if not isinstance(zones, list) or any(z not in ARTNET_ZONES for z in zones):
        return "unknown stage zone in lights"
    if not isinstance(circuits, list) or any(c not in SHELLY_HOSTS for c in circuits):
        return "unknown room light circuit in lights"
    if not zones and not circuits:
        return "choose at least one light for the scene"
    return zones, circuits


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
    zones, circuits = chosen
    scene_id = body.get("id")
    if scene_id is not None and not isinstance(scene_id, str):
        return fail("id must be a light scene id", 400)
    view = await _view(request, fresh=True)  # save what the room light is now
    fixtures = view["stage"]["fixtures"]
    states = {c["id"]: c["on"] for c in view["circuits"] if c["id"] in circuits}
    # Overwriting a scene: a chosen circuit that does not answer now keeps
    # the state the scene had for it (instead of silently dropping out).
    old = await scenes.get(scene_id) if scene_id is not None else None
    kept = {k: old["circuits"][k] for k, on in states.items() if on is None and old and k in old["circuits"]}
    entry = {
        "name": body.get("name"),
        "stage": {"fixtures": {z: fixtures[z] for z in zones}},
        "circuits": {**kept, **{k: on for k, on in states.items() if isinstance(on, bool)}},
    }
    try:
        scene = await scenes.save(entry, replace=body.get("replace") is True, scene_id=scene_id)
    except KeyError:  # before LookupError: KeyError is one
        return fail("Es gibt schon ein Preset mit diesem Namen – Namen von Presets müssen eindeutig sein "
                    "(Groß-/Kleinschreibung zählt nicht).", 409)
    except LookupError:
        return fail("unknown light scene", 404)
    except SceneError as exc:
        return fail(str(exc), 400, unknown_circuits=[k for k, on in states.items() if on is None])
    unknown = [k for k, on in states.items() if on is None and k not in kept]
    log.info("Light scene %r saved (%s room circuits unknown, %s kept)", scene["name"], len(unknown), len(kept))
    # unknown_circuits: left out; kept_circuits: no answer, the scene's old state kept.
    return {"ok": True, "scene": scene, "unknown_circuits": unknown, "kept_circuits": list(kept)}


@router.post("/lights/scenes/order")
async def lights_scene_order(request: Request, body: dict = Body(...)):
    """{"ids": [...]}: every scene once, in the new order."""
    scenes = request.app.state.light_scenes
    if scenes is None:
        return _unavailable()
    try:
        ordered = await scenes.reorder(body.get("ids"))
    except ValueError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "scenes": ordered}


@router.delete("/lights/scenes/{scene_id}")
async def lights_scene_delete(scene_id: str, request: Request):
    scenes = request.app.state.light_scenes
    if scenes is None or not await scenes.delete(scene_id):
        return fail("unknown light scene", 404)
    return {"ok": True}


@router.post("/lights/scenes/{scene_id}/apply")
async def lights_scene_apply(scene_id: str, request: Request):
    return reply(*await apply_scene(request, scene_id))
