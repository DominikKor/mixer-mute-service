"""Hold-to-move pan/tilt for the VISCA cameras of the camera section.

The UI sends a direction while an arrow key is held and "stop" on release; it
repeats the direction about once a second as a keep-alive. If neither arrives
for WATCHDOG_SECONDS (lost release, dropped Wi-Fi, closed tab) the service
stops the camera itself, so a camera never keeps turning on its own."""

from __future__ import annotations

import asyncio
import logging
from typing import Dict, Optional

from . import native_controls
from .native_controls import VISCA_CAMERAS, NativeControlError

log = logging.getLogger("mixer.camera_ptz")

WATCHDOG_SECONDS = 3.0
# VISCA speeds: pan 0x01..0x18, tilt 0x01..0x14. Slow enough to aim precisely
# during a service, fast enough to cross the room in a few seconds.
PAN_SPEED = 0x06
TILT_SPEED = 0x05
# Pan-tilt drive: 81 01 06 01 VV WW XX YY FF (XX: 01 left, 02 right, 03 none;
# YY: 01 up, 02 down, 03 none).
_DIRECTIONS = {
    "left": (0x01, 0x03),
    "right": (0x02, 0x03),
    "up": (0x03, 0x01),
    "down": (0x03, 0x02),
    "stop": (0x03, 0x03),
}
DIRECTIONS = tuple(_DIRECTIONS)


def pan_tilt_payload(direction: str) -> bytes:
    pan, tilt = _DIRECTIONS[direction]
    return bytes((0x81, 0x01, 0x06, 0x01, PAN_SPEED, TILT_SPEED, pan, tilt, 0xFF))


class PanTiltControl:
    def __init__(self, watchdog_seconds: float = WATCHDOG_SECONDS) -> None:
        self._watchdog_seconds = watchdog_seconds
        # Commands of one camera go out in order, so "stop" never overtakes a move.
        self._locks: Dict[str, asyncio.Lock] = {}
        self._watchdogs: Dict[str, asyncio.Task] = {}

    async def drive(self, camera: str, direction: str) -> dict:
        """Start (or keep alive) a move, or stop. Raises NativeControlError."""
        if camera not in VISCA_CAMERAS or direction not in _DIRECTIONS:
            raise NativeControlError("invalid VISCA pan-tilt action")
        lock = self._locks.setdefault(camera, asyncio.Lock())
        async with lock:
            # Armed before sending: even a move whose answer got lost is stopped.
            if direction == "stop":
                self._disarm(camera)
            else:
                self._arm(camera)
            try:
                await self._send(camera, direction)
            except NativeControlError:
                if direction == "stop":
                    # Try the stop once more after the watchdog delay.
                    self._arm(camera)
                raise
        return {"ok": True}

    async def close(self) -> None:
        """Service shutdown: stop every camera that may still be moving."""
        cameras = list(self._watchdogs)
        for camera in cameras:
            self._disarm(camera)
        for camera in cameras:
            try:
                await self._send(camera, "stop")
            except NativeControlError as exc:
                log.warning("PTZ stop on shutdown failed camera=%s reason=%s", camera, exc)

    async def _send(self, camera: str, direction: str) -> None:
        # Looked up on the module so tests can mock the TCP send.
        await asyncio.to_thread(native_controls._visca_tcp, *VISCA_CAMERAS[camera], pan_tilt_payload(direction))

    def _arm(self, camera: str) -> None:
        self._disarm(camera)
        self._watchdogs[camera] = asyncio.create_task(self._watchdog(camera))

    def _disarm(self, camera: str) -> None:
        task: Optional[asyncio.Task] = self._watchdogs.pop(camera, None)
        if task is not None and task is not asyncio.current_task():
            task.cancel()

    async def _watchdog(self, camera: str) -> None:
        await asyncio.sleep(self._watchdog_seconds)
        lock = self._locks.setdefault(camera, asyncio.Lock())
        async with lock:
            if self._watchdogs.get(camera) is not asyncio.current_task():
                return  # a newer command took over meanwhile
            self._watchdogs.pop(camera, None)
            log.warning("PTZ watchdog: no stop or keep-alive for %.1fs, stopping camera=%s",
                        self._watchdog_seconds, camera)
            try:
                await self._send(camera, "stop")
            except NativeControlError as exc:
                log.warning("PTZ watchdog stop failed camera=%s reason=%s", camera, exc)
