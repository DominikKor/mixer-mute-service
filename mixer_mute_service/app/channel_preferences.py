"""Persistent channel preferences that affect group actions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Set


class ChannelPreferences:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._always_on: Set[int] = set()
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            channels = data.get("always_on", []) if isinstance(data, dict) else []
            self._always_on = {
                ch for ch in channels
                if isinstance(ch, int) and not isinstance(ch, bool) and ch > 0
            }
        except (OSError, ValueError, TypeError):
            self._always_on = set()

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"always_on": sorted(self._always_on)}, indent=2),
            encoding="utf-8",
        )

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
