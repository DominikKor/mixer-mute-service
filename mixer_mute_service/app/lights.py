"""Licht: the stage (Art-Net zones), the room light (Shelly circuits) and
light scenes.

A scene (a "Preset" on the page) is the light as it was when saved, but only
for the lights chosen for it: any of the stage lights (each with on/off,
brightness and its own warm/cold colour) and room light circuits. Applying
it sets only those; every other light stays as it is. A chosen circuit
whose state was unknown when saving (it did not answer) is left out.
Program rows name a scene in their "light" field; it is applied when the
row starts. Scenes keep the order the user gives them.

Scenes from before the lights were stored one by one ({"zones": {zone:
level}, "warm", "cold"}) are read as: level > 0 on at that level, 0 off
(keeping the light's level), the colour (if saved) for each of those
lights, or for all lights if the scene held only the colour.

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
    FIXTURE_KEYS,
    SHELLY_HOSTS,
    NativeControlError,
)

log = logging.getLogger("mixer.lights")

MAX_SCENES = 40
# Several pages poll /lights; the Shellys are asked at most this often.
CIRCUIT_CACHE_SECONDS = 2.0
# How long GET /lights waits for the Shellys' answers; a later answer is
# shown by the next poll.
CIRCUIT_WAIT_SECONDS = 0.4


class SceneError(ValueError):
    pass


def _level(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= 255


def _clean_fixture(entry) -> dict:
    """A stage light in a scene: on (bool), level, warm, cold; None (or
    left out) keeps what the light has."""
    if not isinstance(entry, dict) or any(key not in FIXTURE_KEYS for key in entry):
        raise SceneError("a scene stage light is {on, level, warm, cold}")
    fixture = {key: entry.get(key) for key in FIXTURE_KEYS}
    if fixture["on"] is not None and not isinstance(fixture["on"], bool):
        raise SceneError("a scene stage light's on must be true or false")
    if any(fixture[key] is not None and not _level(fixture[key]) for key in ("level", "warm", "cold")):
        raise SceneError("scene light values must be 0..255")
    if all(value is None for value in fixture.values()):
        raise SceneError("a scene stage light must set something")
    return fixture


def _legacy_fixtures(stage: dict) -> dict:
    """{"zones": {zone: level}, "warm", "cold"} (one colour for all lights)
    as stage lights."""
    zones = stage.get("zones") or {}
    if not isinstance(zones, dict) or any(z not in ARTNET_ZONES or not _level(v) for z, v in zones.items()):
        raise SceneError("scene stage zones must be known zones with a level 0..255")
    warm, cold = stage.get("warm"), stage.get("cold")
    # The colour was one choice: warm and cold together, or neither.
    if (warm is None) != (cold is None) or (warm is not None and not (_level(warm) and _level(cold))):
        raise SceneError("scene needs warm and cold 0..255 together, or neither")
    if not zones and warm is not None:
        zones = dict.fromkeys(ARTNET_ZONES)  # colour only: every light, on/off as it is
    return {zone: {"on": None if level is None else level > 0, "level": level or None, "warm": warm, "cold": cold}
            for zone, level in zones.items()}


def _clean_scene(entry: dict) -> dict:
    name = entry.get("name")
    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > 40 or any(ord(c) < 32 for c in name):
        raise SceneError("scene name must contain 1 to 40 characters")
    stage = entry.get("stage") or {}
    if not isinstance(stage, dict):
        raise SceneError("scene stage must be {fixtures}")
    if "fixtures" in stage:
        fixtures = stage["fixtures"] or {}
        if not isinstance(fixtures, dict) or any(z not in ARTNET_ZONES for z in fixtures):
            raise SceneError("scene stage lights must be known zones")
        fixtures = {zone: _clean_fixture(value) for zone, value in fixtures.items()}
    else:
        fixtures = _legacy_fixtures(stage)
    circuits = entry.get("circuits") or {}
    if not isinstance(circuits, dict) or any(
            target not in SHELLY_HOSTS or not isinstance(on, bool) for target, on in circuits.items()):
        raise SceneError("invalid room light circuit in scene")
    if not fixtures and not circuits:
        raise SceneError("a scene must control at least one light")
    return {
        "id": str(entry.get("id") or uuid.uuid4().hex),
        "name": name,
        "stage": {"fixtures": {k: fixtures[k] for k in ARTNET_ZONES if k in fixtures}},
        "circuits": {k: circuits[k] for k in SHELLY_HOSTS if k in circuits},
    }


def scene_is_active(scene: dict, stage: dict, circuits: dict) -> bool:
    """Whether the light is now as the scene sets it. A light that is (and
    should be) off counts whatever level or colour it keeps. A circuit whose
    state is unknown (no answer) means: not known to match, so not active
    (else a flaky Shelly would make the mark come and go)."""
    for zone, want in scene["stage"]["fixtures"].items():
        now = stage["fixtures"][zone]
        lit = now["on"] if want["on"] is None else want["on"]
        if lit != now["on"]:
            return False
        if lit and any(want[key] is not None and want[key] != now[key] for key in ("level", "warm", "cold")):
            return False
    return all(circuits.get(target) is on for target, on in scene["circuits"].items())


class LightSceneStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._scenes: List[dict] = []
        raw = read_json(path, list, default=[])
        legacy = False
        for entry in raw:
            try:
                self._scenes.append(_clean_scene(entry))
            except (SceneError, AttributeError, TypeError) as exc:
                log.warning("Skipping invalid light scene in %s: %s", path, exc)
                continue
            legacy = legacy or "fixtures" not in ((entry.get("stage") or {}) if isinstance(entry, dict) else {})
        if legacy:
            # Saved before the lights were stored one by one: the page showed
            # the scenes by name, so that becomes their order.
            self._scenes.sort(key=lambda s: s["name"].casefold())
            try:
                self._persist()
                log.info("Light scenes in %s converted to stage lights one by one", path)
            except OSError as exc:
                log.warning("Could not save converted light scenes to %s: %s", path, exc)

    def _persist(self) -> None:
        write_json_atomic(self._path, self._scenes, ensure_ascii=False)

    async def list(self) -> List[dict]:
        async with self._lock:
            return deepcopy(self._scenes)

    async def get(self, scene_id: str) -> Optional[dict]:
        async with self._lock:
            return deepcopy(next((s for s in self._scenes if s["id"] == scene_id), None))

    async def save(self, entry: dict, replace: bool = False, scene_id: Optional[str] = None) -> dict:
        """Save a scene; an existing name only with replace=True (KeyError).
        ``scene_id``: update that scene (its place and program rows stay; it
        may get a new name): LookupError if unknown, KeyError if the name is
        another scene's."""
        scene = _clean_scene(entry)
        async with self._lock:
            if scene_id is not None:
                target = next((s for s in self._scenes if s["id"] == scene_id), None)
                if target is None:
                    raise LookupError(scene_id)
                if any(s is not target and s["name"].casefold() == scene["name"].casefold() for s in self._scenes):
                    raise KeyError(scene["name"])
                scene["id"] = scene_id
                target.clear()
                target.update(scene)
                self._persist()
                return deepcopy(scene)
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

    async def rename(self, scene_id: str, name) -> dict:
        """KeyError: unknown scene; FileExistsError: name taken; SceneError: bad name."""
        async with self._lock:
            scene = next((s for s in self._scenes if s["id"] == scene_id), None)
            if scene is None:
                raise KeyError(scene_id)
            new = _clean_scene({**scene, "name": name})["name"]
            if any(s is not scene and s["name"].casefold() == new.casefold() for s in self._scenes):
                raise FileExistsError(new)
            scene["name"] = new
            self._persist()
            return deepcopy(scene)

    async def reorder(self, ids) -> List[dict]:
        """New order: every scene id exactly once (ValueError otherwise)."""
        async with self._lock:
            by_id = {s["id"]: s for s in self._scenes}
            if not isinstance(ids, list) or sorted(map(str, ids)) != sorted(by_id) or len(set(ids)) != len(ids):
                raise ValueError("order must name every light scene once")
            self._scenes = [by_id[i] for i in ids]
            self._persist()
            return deepcopy(self._scenes)

    async def delete(self, scene_id: str) -> bool:
        async with self._lock:
            before = len(self._scenes)
            self._scenes = [s for s in self._scenes if s["id"] != scene_id]
            if len(self._scenes) == before:
                return False
            self._persist()
            return True


class CircuitCache:
    """The room light states, asked from each Shelly at most every few seconds.

    Every circuit is asked on its own: a page waits at most
    CIRCUIT_WAIT_SECONDS for the answers; a Shelly that is slower (or dead,
    up to its timeout) updates its state when it is done, so one dead
    circuit never holds up the page or the others. At most one question
    per Shelly is open at a time. An answer to a question asked before the
    circuit was last switched is dropped: it may show the old state.
    """

    def __init__(self) -> None:
        self._states: dict = {}      # target -> True / False / None (no answer)
        self._asked_at: dict = {}    # target -> monotonic start of the last question
        self._set_at: dict = {}      # target -> monotonic time of the last switch
        self._pending: dict = {}     # target -> task asking it now

    def _ask(self, native, target: str) -> asyncio.Task:
        task = self._pending.get(target)
        if task is None or task.done():
            started = time.monotonic()
            self._asked_at[target] = started
            task = asyncio.ensure_future(self._query(native, target, started))
            self._pending[target] = task
        return task

    async def _query(self, native, target: str, started: float) -> None:
        try:
            on = await native.circuit_state(target)
        except Exception:  # noqa: BLE001 - a background question must not die silently
            log.exception("Room light %s: state question failed", target)
            on = None
        if self._set_at.get(target, float("-inf")) < started:
            self._states[target] = on

    async def get(self, native, fresh: bool = False) -> dict:
        """The states; ``fresh``: ask every Shelly now and wait for all
        answers (saving a scene)."""
        now = time.monotonic()
        asked = {target: self._ask(native, target) for target in SHELLY_HOSTS
                 if fresh or target not in self._states
                 or now - self._asked_at.get(target, 0.0) > CIRCUIT_CACHE_SECONDS}
        # The first answer of a circuit (and a fresh one) is waited for in
        # full; a circuit that did not answer last time is not waited for.
        full = fresh or any(target not in self._states for target in SHELLY_HOSTS)
        tasks = [task for target, task in asked.items() if full or self._states.get(target) is not None]
        if tasks:
            await asyncio.wait(tasks, timeout=None if full else CIRCUIT_WAIT_SECONDS)
        return self.snapshot()

    def snapshot(self) -> dict:
        """The states as known now, without asking any Shelly."""
        return {target: self._states.get(target) for target in SHELLY_HOSTS}

    def update(self, states: dict) -> None:
        """Circuits just switched (their Shelly confirmed it)."""
        now = time.monotonic()
        for target, on in states.items():
            self._states[target] = on
            self._set_at[target] = now


# -- shared with program rows ---------------------------------------------------

async def apply_scene(request: Request, scene_id: str) -> Tuple[dict, int]:
    """Apply a scene: room light and stage fade at the same time (a dead
    Shelly must not hold back the stage). Every part is tried; any failure
    makes the result not ok and is listed."""
    state = request.app.state
    native, scenes = state.native_controls, state.light_scenes
    if native is None or scenes is None:
        return {"ok": False, "reason": "light control is not available"}, 503
    scene = await scenes.get(scene_id)
    if scene is None:
        return {"ok": False, "reason": "unknown light scene"}, 404
    errors = {}

    async def room_light() -> None:
        if scene["circuits"]:
            failed = await native.set_circuits(scene["circuits"])
            state.light_circuits.update({k: v for k, v in scene["circuits"].items() if k not in failed})
            errors.update(failed)

    async def stage_light() -> None:
        # Only the chosen lights; the others keep what they have.
        changes = {zone: {k: v for k, v in fixture.items() if v is not None}
                   for zone, fixture in scene["stage"]["fixtures"].items()}
        if changes:
            try:
                await native.set_stage(changes)
            except (NativeControlError, OSError) as exc:
                errors["stage"] = str(exc)

    await asyncio.gather(room_light(), stage_light())
    if errors:
        log.warning("Light scene %r applied with errors: %s", scene["name"], errors)
        return {"ok": False, "reason": "light scene not fully applied", "scene": scene["name"], "errors": errors}, 502
    state.light_last_scene = scene["id"]
    log.info("Light scene %r applied", scene["name"])
    return {"ok": True, "scene": scene["name"]}, 200
