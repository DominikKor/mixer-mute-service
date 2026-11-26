"""What OBS has on air, for the live colours of the Steuerung keys.

Every page polls GET /obs/status; one OBS query (one connection, see
native_controls.obs_status_blocking) serves all of them for a short while,
and while OBS does not answer it is asked again only every few seconds.
A key that switches OBS clears the cache, so the next poll shows the cut."""

from __future__ import annotations

import asyncio
import time
from typing import Callable

from .native_controls import NativeControlError, obs_status_blocking

STATUS_TTL_SECONDS = 1.0
ERROR_TTL_SECONDS = 5.0


class ObsStatus:
    def __init__(self, fetch: Callable[[], dict] = obs_status_blocking,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._fetch = fetch
        self._clock = clock
        self._lock = asyncio.Lock()
        self._value: dict = {}
        self._expires = 0.0

    async def get(self) -> dict:
        async with self._lock:
            if self._value and self._clock() < self._expires:
                return self._value
            try:
                value = {"ok": True, **await asyncio.to_thread(self._fetch)}
                ttl = STATUS_TTL_SECONDS
            except NativeControlError as exc:
                value = {"ok": False, "reason": str(exc), "program": None, "preview": None}
                ttl = ERROR_TTL_SECONDS
            self._value, self._expires = value, self._clock() + ttl
            return value

    def invalidate(self) -> None:
        self._expires = 0.0
