"""Persistent channel preferences that affect group actions."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Set

from .jsonstore import CorruptJSONError, read_json, write_json_atomic

log = logging.getLogger("mixer.preferences")


class ChannelPreferences:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._always_on: Set[int] = set()
        # False when the file exists but cannot be read: the protected
        # ("always on") channels are then unknown, and the exclusive unmute
        # must not mute channels that may be protected (see Controller).
        self.readable = True
        self._load()

    def _load(self) -> None:
        try:
            data = read_json(self._path, dict)
        except FileNotFoundError:
            data = {}
        except CorruptJSONError:
            log.error("%s is unreadable: always-on channels unknown; group unmute will not "
                      "mute other channels until it is fixed or an always-on switch is set",
                      self._path)
            self.readable = False
            return
        channels = data.get("always_on", [])
        if not isinstance(channels, list):
            channels = []
        self._always_on = {
            ch for ch in channels
            if isinstance(ch, int) and not isinstance(ch, bool) and ch > 0
        }


    async def list_always_on(self) -> Set[int]:
        async with self._lock:
            return set(self._always_on)

    async def set_always_on(self, channel: int, enabled: bool) -> None:
        async with self._lock:
            new = set(self._always_on)
            if enabled:
                new.add(channel)
            else:
                new.discard(channel)
            # Write first, then switch, so memory and disk never disagree.
            write_json_atomic(self._path, {"always_on": sorted(new)})
            self._always_on = new
            self.readable = True
