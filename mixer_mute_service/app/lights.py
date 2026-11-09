"""Licht: the stage (Art-Net zones), the room light (Shelly circuits) and
light scenes.

A scene is the light as it was when saved: the four stage zones, warm and
cold, and every room light circuit whose state was known then (a circuit
that did not answer is left out and is not switched when the scene is
applied). Program rows name a scene in their "light" field; it is applied
when the row starts.

The HTTP routes are in app/api/lights.py.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import List, Optional, Tuple

from fastapi import Request

from .jsonstore import read_json, write_json_atomic
from .native_controls import (
    ARTNET_ZONES,
    SHELLY_HOSTS,
    NativeControlError,
)

log = logging.getLogger("mixer.lights")

MAX_SCENES = 40
# Several pages poll /lights; the Shellys are asked at most this often.
CIRCUIT_CACHE_SECONDS = 2.0


class SceneError(ValueError):
    pass


def _level(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255


def _clean_scene(entry: dict) -> dict:
    name = entry.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > 40 or any(ord(c) < 32 for c in name):
        raise SceneError("scene name must contain 1 to 40 characters")
    stage = entry.get("stage") or {}
    zones = stage.get("zones") or {}
    if set(zones) != set(ARTNET_ZONES) or not all(_level(v) for v in zones.values()):
        raise SceneError("scene needs a level 0..255 for every stage zone")
    if not _level(stage.get("warm")) or not _level(stage.get("cold")):
        raise SceneError("scene needs warm and cold 0..255")
    circuits = entry.get("circuits") or {}
    if any(target not in SHELLY_HOSTS or not isinstance(on, bool) for target, on in circuits.items()):
        raise SceneError("invalid room light circuit in scene")
    return {
        "id": str(entry.get("id") or uuid.uuid4().hex),
        "name": name,
        "stage": {"zones": {k: zones[k] for k in ARTNET_ZONES}, "warm": stage["warm"], "cold": stage["cold"]},
        "circuits": {k: circuits[k] for k in SHELLY_HOSTS if k in circuits},
    }


class LightSceneStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._scenes: List[dict] = []
        for entry in read_json(path, list, default=[]):
            try:
                self._scenes.append(_clean_scene(entry))
            except (SceneError, AttributeError, TypeError) as exc:
                log.warning("Skipping invalid light scene in %s: %s", path, exc)

    def _persist(self) -> None:
        write_json_atomic(self._path, self._scenes, ensure_ascii=False)

    async def list(self) -> List[dict]:
        async with self._lock:
            return deepcopy(sorted(self._scenes, key=lambda s: s["name"].casefold()))

    async def get(self, scene_id: str) -> Optional[dict]:
        async with self._lock:
            return deepcopy(next((s for s in self._scenes if s["id"] == scene_id), None))

    async def save(self, entry: dict, replace: bool = False) -> dict:
        """Save a scene; an existing name only with replace=True (KeyError)."""
        scene = _clean_scene(entry)
        async with self._lock:
            existing = next((s for s in self._scenes if s["name"].casefold() == scene["name"].casefold()), None)
            if existing and not replace:
                raise KeyError(scene["name"])
            if existing:
                scene["id"] = existing["id"]
                existing.update(scene)
            else:
                if len(self._scenes) >= MAX_SCENES:
                    raise SceneError(f"at most {MAX_SCENES} scenes")
                self._scenes.append(scene)
            self._persist()
            return deepcopy(scene)

    async def delete(self, scene_id: str) -> bool:
        async with self._lock:
            before = len(self._scenes)
            self._scenes = [s for s in self._scenes if s["id"] != scene_id]
            if len(self._scenes) == before:
                return False
            self._persist()
            return True


class CircuitCache:
    """The room light states, asked from the Shellys at most every few seconds."""

    def __init__(self) -> None:
        self._states: Optional[dict] = None
        self._at = 0.0
        self._lock = asyncio.Lock()

    async def get(self, native) -> dict:
        async with self._lock:
            if self._states is None or time.monotonic() - self._at > CIRCUIT_CACHE_SECONDS:
                self._states = await native.circuit_states()
                self._at = time.monotonic()
            return dict(self._states)

    def update(self, states: dict) -> None:
        if self._states is not None:
            self._states.update(states)

    def forget(self) -> None:
        self._states = None


# -- shared with program rows ---------------------------------------------------

async def apply_scene(request: Request, scene_id: str) -> Tuple[dict, int]:
    """Apply a scene: room light first (quick), then the stage fade. Every
    part is tried; any failure makes the result not ok and is listed."""
    state = request.app.state
    native, scenes = state.native_controls, state.light_scenes
    if native is None or scenes is None:
        return {"ok": False, "reason": "light control is not available"}, 503
    scene = await scenes.get(scene_id)
    if scene is None:
        return {"ok": False, "reason": "unknown light scene"}, 404
    errors = {}
    if scene["circuits"]:
        errors = await native.set_circuits(scene["circuits"])
        state.light_circuits.update({k: v for k, v in scene["circuits"].items() if k not in errors})
    try:
        await native.set_stage(scene["stage"]["zones"], scene["stage"]["warm"], scene["stage"]["cold"])
    except (NativeControlError, OSError) as exc:
        errors["stage"] = str(exc)
    if errors:
        log.warning("Light scene %r applied with errors: %s", scene["name"], errors)
        return {"ok": False, "reason": "light scene not fully applied", "scene": scene["name"], "errors": errors}, 502
    log.info("Light scene %r applied", scene["name"])
    return {"ok": True, "scene": scene["name"]}, 200
