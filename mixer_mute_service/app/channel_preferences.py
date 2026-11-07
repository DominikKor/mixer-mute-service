"""Persistent channel preferences that affect group actions."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Set

from .jsonstore import read_json, write_json_atomic


class ChannelPreferences:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._always_on: Set[int] = set()
        self._load()

    def _load(self) -> None:
        channels = read_json(self._path, dict, default={}).get("always_on", [])
        if not isinstance(channels, list):
            channels = []
        self._always_on = {
            ch for ch in channels
            if isinstance(ch, int) and not isinstance(ch, bool) and ch > 0
        }

    def _persist(self) -> None:
        write_json_atomic(self._path, {"always_on": sorted(self._always_on)})

    async def list_always_on(self) -> Set[int]:
        async with self._lock:
            return set(self._always_on)

    async def set_always_on(self, channel: int, enabled: bool) -> None:
        async with self._lock:
            if enabled:
                self._always_on.add(channel)
            else:
                self._always_on.discard(channel)
            self._persist()
