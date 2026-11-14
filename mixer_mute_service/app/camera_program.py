"""Camera presets of a program row, moved only while the camera is off air.

A row may name a VISCA preset per camera. A preset recall drives the camera
there visibly, and that jerky move must never be seen on the stream. So:

1. OBS is asked which camera is live: a program scene named after a camera
   ("Cam 1") means that camera; in any other scene every enabled camera
   source ("Cam2_klein", also in nested scenes) counts. If OBS cannot be
   asked, no camera moves.
2. The camera that is NOT live gets its preset at once.
3. After the row's wait (the move takes a few seconds), OBS switches to that
   camera ("Cam N Live", as the key in Steuerung does), unless someone
   switched OBS in the meantime: then nothing more happens.
4. Once the transition is over, the camera that was live before gets its
   preset. It is not switched to; the operator does that by hand.

With no camera live (title, PC or beamer on air) both presets are applied at
once and nothing is switched. A preset for a camera that is live, and every
preset while both are live (picture in picture), is skipped.

Each camera of a row may also name an OBS scene. It replaces the switch in
step 3: OBS goes to the scene of the camera that moved. With no camera live
the scene of a moved camera (Cam 1 first) follows after the wait, so a
moving camera is never seen. A scene only counts for a camera whose preset
is applied; when nothing moves, OBS stays as it is.

Steps 3 and 4 run in the background; a new row cancels a sequence still
waiting. The last outcome is kept for the UI (``status``).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Awaitable, Callable, Dict, List, Optional, Set

from .native_controls import (
    OBS_HOST,
    OBS_PORT,
    VISCA_CAMERAS,
    NativeControlError,
    _obs_request,
)

log = logging.getLogger("mixer.camera_program")

CAMERAS = ("cam1", "cam2")
CAMERA_LABELS = {"cam1": "Cam 1", "cam2": "Cam 2"}
# The Steuerung keys that put a camera on air.
LIVE_BUTTON_IDS = {"cam1": "native-obs-cam1-live", "cam2": "native-obs-cam2-live"}
DEFAULT_WAIT_SECONDS = 5.0
MIN_WAIT_SECONDS = 1.0
MAX_WAIT_SECONDS = 30.0
# A scene or source named "Cam1", "Cam 1", "Cam1_klein", "CAM 2 (NDI)"...
_CAMERA_NAME_RE = re.compile(r"^\s*cam\s*[-_]?\s*([12])(?!\d)", re.IGNORECASE)
_MAX_SCENE_DEPTH = 4


def camera_of(name: object) -> Optional[str]:
    match = _CAMERA_NAME_RE.match(name) if isinstance(name, str) else None
    return f"cam{match.group(1)}" if match else None


def _obs(request_type: str, data: Optional[dict] = None) -> dict:
    return _obs_request(OBS_HOST, OBS_PORT, request_type, data)


def _live_cameras_blocking(obs: Callable[..., dict] = _obs) -> Set[str]:
    """Cameras visible in OBS's program output. Raises NativeControlError."""
    scene = obs("GetCurrentProgramScene").get("currentProgramSceneName")
    if not isinstance(scene, str) or not scene:
        raise NativeControlError("OBS did not name its program scene")
    if camera_of(scene):
        # A camera's own scene ("Cam 1") shows that camera. It may hold the
        # other camera's source too: the "Cam N Live" keys switch sources in
        # the current scene, so that one is not taken as on air.
        return {camera_of(scene)}
    # Any other scene (title, PC, picture in picture): every camera source
    # shown in it or in a scene nested in it counts as on air.
    live: Set[str] = set()
    seen: Set[str] = set()

    def walk(name: str, group: bool, depth: int) -> None:
        cam = camera_of(name)
        if cam:
            live.add(cam)
        if name in seen or depth > _MAX_SCENE_DEPTH:
            return
        seen.add(name)
        request = "GetGroupSceneItemList" if group else "GetSceneItemList"
        for item in obs(request, {"sceneName": name}).get("sceneItems", []):
            if not item.get("sceneItemEnabled"):
                continue
            source = item.get("sourceName")
            if item.get("isGroup") or item.get("sourceType") == "OBS_SOURCE_TYPE_SCENE":
                if isinstance(source, str):
                    walk(source, bool(item.get("isGroup")), depth + 1)
            elif camera_of(source):
                live.add(camera_of(source))

    walk(scene, False, 0)
    return live


def _transition_seconds_blocking(obs: Callable[..., dict] = _obs) -> float:
    try:
        duration = obs("GetCurrentSceneTransition").get("transitionDuration")
    except NativeControlError:
        return 2.0
    return duration / 1000.0 if isinstance(duration, (int, float)) and duration > 0 else 0.0


def valid_preset(value: object) -> bool:
    return type(value) is int and 1 <= value <= 16


def obs_scene_names(obs: Callable[..., dict] = _obs) -> List[str]:
    """OBS's scenes, top first as in its scene list. Raises NativeControlError."""
    scenes = obs("GetSceneList").get("scenes", [])
    # OBS answers bottom-up (index 0 is the lowest scene in its window).
    names = [scene.get("sceneName") for scene in reversed(scenes) if isinstance(scene, dict)]
    return [name for name in names if isinstance(name, str) and name]


class CameraProgram:
    """Runs the camera part of program rows. The device calls are injected,
    so tests run the whole sequence without OBS or cameras."""

    def __init__(
        self,
        read_live: Callable[[], Awaitable[Set[str]]],
        recall: Callable[[str, int], Awaitable[None]],
        switch: Callable[[str], Awaitable[None]],
        transition_seconds: Callable[[], Awaitable[float]],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        switch_scene: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> None:
        self._read_live = read_live
        self._recall = recall
        self._switch = switch
        self._transition_seconds = transition_seconds
        self._sleep = sleep
        self._switch_scene = switch_scene
        self._task: Optional[asyncio.Task] = None
        self._seq = 0
        self.status: dict = {"seq": 0, "state": "idle", "message": "", "at": None}

    def _set_status(self, state: str, message: str) -> None:
        self._seq += 1
        self.status = {"seq": self._seq, "state": state, "message": message, "at": time.time()}
        (log.warning if state in ("failed", "aborted") else log.info)("Camera program %s: %s", state, message)

    def cancel(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None

    async def run(self, presets: Dict[str, Optional[int]], wait_seconds: float, title: str = "",
                  scenes: Optional[Dict[str, Optional[str]]] = None) -> dict:
        """Phase one now (preset of the camera that is off air); switching and
        the second preset follow in the background. Returns what was done,
        with the status number it left (the UI reports later ones)."""
        chosen = {cam: scene for cam, scene in (scenes or {}).items() if cam in CAMERAS and scene}
        result = await self._run(presets, wait_seconds, title, chosen)
        return {**result, "seq": self.status["seq"]}

    async def _to_scene(self, scene: str) -> None:
        if self._switch_scene is None:
            raise NativeControlError("OBS scenes cannot be switched here")
        await self._switch_scene(scene)

    async def _run(self, presets: Dict[str, Optional[int]], wait_seconds: float, title: str,
                   scenes: Dict[str, str]) -> dict:
        wanted = {cam: presets.get(cam) for cam in CAMERAS if valid_preset(presets.get(cam))}
        if not wanted:
            return {"ok": True, "applied": [], "skipped": []}
        self.cancel()  # a row still waiting to switch must not switch any more
        wait = min(MAX_WAIT_SECONDS, max(MIN_WAIT_SECONDS, float(wait_seconds)))
        try:
            live = await self._read_live()
        except NativeControlError as exc:
            message = f"OBS nicht erreichbar, keine Kamera bewegt ({exc})"
            self._set_status("failed", message)
            return {"ok": False, "reason": message, "applied": [], "skipped": sorted(wanted)}

        if len(live) > 1 or (live and not set(CAMERAS) - live):
            message = "Beide Kameras sind live, kein Preset angefahren"
            self._set_status("skipped", message)
            return {"ok": True, "message": message, "applied": [], "skipped": sorted(wanted)}

        try:
            if not live:
                for cam, preset in wanted.items():
                    await self._recall(cam, preset)
                applied = sorted(wanted)
                moved = ", ".join(f"{CAMERA_LABELS[c]} Preset {wanted[c]}" for c in applied)
                scene = next((scenes[cam] for cam in CAMERAS if cam in wanted and cam in scenes), None)
                if scene:
                    message = f"Keine Kamera live: {moved}, OBS-Szene „{scene}“ in {wait:g} s"
                    self._set_status("waiting", message)
                    self._task = asyncio.create_task(self._finish_scene(scene, wait))
                    return {"ok": True, "message": message, "applied": applied, "skipped": [],
                            "switch_to": None, "scene": scene, "switch_in_seconds": wait}
                message = f"Keine Kamera live: {moved}, nicht umgeschaltet"
                self._set_status("done", message)
                return {"ok": True, "message": message, "applied": applied, "skipped": [], "switch_to": None}

            (on_air,) = live
            off_air = next(cam for cam in CAMERAS if cam != on_air)
            if off_air not in wanted:
                message = f"{CAMERA_LABELS[on_air]} ist live, Preset nicht angefahren"
                self._set_status("skipped", message)
                return {"ok": True, "message": message, "applied": [], "skipped": [on_air]}
            await self._recall(off_air, wanted[off_air])
        except NativeControlError as exc:
            message = f"Kamera-Preset fehlgeschlagen: {exc}"
            self._set_status("failed", message)
            return {"ok": False, "reason": message, "applied": [], "skipped": sorted(wanted)}

        later = wanted.get(on_air)
        scene = scenes.get(off_air)
        target = f"OBS-Szene „{scene}“" if scene else "Umschalten"
        message = f"{CAMERA_LABELS[off_air]} fährt Preset {wanted[off_air]} an, {target} in {wait:g} s"
        self._set_status("waiting", message)
        self._task = asyncio.create_task(self._finish(on_air, off_air, later, wait, title, scene))
        return {"ok": True, "message": message, "applied": [off_air], "skipped": [],
                "switch_to": off_air, "scene": scene, "switch_in_seconds": wait,
                "then": on_air if later else None}

    async def _finish_scene(self, scene: str, wait: float) -> None:
        """No camera was live: the row's scene after the presets had time to move."""
        try:
            await self._sleep(wait)
            if await self._read_live():
                self._set_status("aborted", f"OBS wurde inzwischen umgeschaltet, Szene „{scene}“ nicht geschaltet")
                return
            await self._to_scene(scene)
            self._set_status("done", f"OBS-Szene „{scene}“")
        except asyncio.CancelledError:
            self._set_status("aborted", "Vom nächsten Programmpunkt abgelöst")
            raise
        except NativeControlError as exc:
            self._set_status("failed", f"OBS-Szene „{scene}“ nicht geschaltet: {exc}")
        except Exception:  # noqa: BLE001 - a background task must report, not vanish
            log.exception("Camera program failed")
            self._set_status("failed", "Kamera-Ablauf abgebrochen (interner Fehler)")

    async def _finish(self, on_air: str, off_air: str, later: Optional[int], wait: float, title: str,
                      scene: Optional[str] = None) -> None:
        shown = f"OBS-Szene „{scene}“" if scene else f"{CAMERA_LABELS[off_air]} live"
        try:
            await self._sleep(wait)
            live = await self._read_live()
            if live != {on_air}:
                # Someone switched OBS meanwhile: leave the picture to them.
                skipped = f"Szene „{scene}“ nicht geschaltet" if scene else f"{CAMERA_LABELS[off_air]} nicht live geschaltet"
                self._set_status("aborted", f"OBS wurde inzwischen umgeschaltet, {skipped}")
                return
            if scene:
                await self._to_scene(scene)
            else:
                await self._switch(off_air)
            if later is None:
                self._set_status("done", shown)
                return
            await self._sleep(await self._transition_seconds() + 0.5)
            live = await self._read_live()
            if on_air in live:
                self._set_status("aborted", f"{CAMERA_LABELS[on_air]} ist wieder live, Preset {later} nicht angefahren")
                return
            await self._recall(on_air, later)
            self._set_status("done", f"{shown}, {CAMERA_LABELS[on_air]} fährt Preset {later} an")
        except asyncio.CancelledError:
            self._set_status("aborted", "Vom nächsten Programmpunkt abgelöst")
            raise
        except NativeControlError as exc:
            self._set_status("failed", f"Kamera-Ablauf abgebrochen: {exc}")
        except Exception:  # noqa: BLE001 - a background task must report, not vanish
            log.exception("Camera program failed")
            self._set_status("failed", "Kamera-Ablauf abgebrochen (interner Fehler)")


def device_camera_program(native_controls) -> CameraProgram:
    """The real wiring: OBS over its WebSocket, presets and the live switch
    through the same catalog actions as the keys."""
    from .companion_buttons import COMPANION_BUTTONS_BY_ID

    async def read_live() -> Set[str]:
        return await asyncio.to_thread(_live_cameras_blocking)

    async def recall(cam: str, preset: int) -> None:
        if cam not in VISCA_CAMERAS:
            raise NativeControlError(f"unknown camera {cam}")
        await native_controls.press({"type": "visca-preset", "camera": cam, "preset": preset})

    async def switch(cam: str) -> None:
        await native_controls.press(COMPANION_BUTTONS_BY_ID[LIVE_BUTTON_IDS[cam]]["native_action"])

    async def transition_seconds() -> float:
        return await asyncio.to_thread(_transition_seconds_blocking)

    async def switch_scene(scene: str) -> None:
        await native_controls.press({"type": "obs-scene", "scene": scene})

    return CameraProgram(read_live, recall, switch, transition_seconds, switch_scene=switch_scene)
