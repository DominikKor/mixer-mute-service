"""Writable mute-group store.

Groups in config.yaml are treated as the initial seed. At runtime the UI can
create/edit/delete groups; those live in a separate JSON file so the hand-written
config.yaml (with its comments and mixer/safety settings) is never clobbered.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Dict, List, Optional

_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]{0,63}$")


class GroupError(ValueError):
    """Raised for invalid group operations (bad name/channels, exists, missing)."""


class GroupStore:
    def __init__(self, path: Path, valid_channels, seed: Dict[str, List[int]]) -> None:
        self._path = path
        # All addressable channel numbers (inputs + buses). Accepts an int for
        # backward compat (treated as 1..N).
        if isinstance(valid_channels, int):
            valid_channels = range(1, valid_channels + 1)
        self._valid = set(valid_channels)
        self._lock = asyncio.Lock()
        self._groups: Dict[str, List[int]] = {}
        self._load(seed)

    # -- persistence -------------------------------------------------------

    def _load(self, seed: Dict[str, List[int]]) -> None:
        seeded = {k: list(v) for k, v in seed.items()}
        if self._path.exists():
            try:
                data = json.loads(self._path.read_text() or "{}")
                if isinstance(data, dict) and data:
                    self._groups = {k: list(v) for k, v in data.items()}
                    return
            except Exception:
                pass
            self._groups = seeded
            self._persist()
            return
        self._groups = seeded
        self._persist()

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._groups, indent=2, sort_keys=True))

    # -- validation --------------------------------------------------------

    def _validate_name(self, name: str) -> str:
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            raise GroupError(f"invalid group name: {name!r}")
        return name

    def _validate_channels(self, channels: List[int]) -> List[int]:
        if not channels:
            raise GroupError("group must contain at least one channel")
        seen: List[int] = []
        for ch in channels:
            if not isinstance(ch, int) or isinstance(ch, bool):
                raise GroupError(f"channel must be an integer: {ch!r}")
            if ch not in self._valid:
                raise GroupError(f"unknown channel {ch}")
            if ch not in seen:
                seen.append(ch)
        return seen

    def allow_channel(self, channel: int) -> None:
        """Accept a channel that became addressable at runtime (a bus
        discovered from the mixer's state dump)."""
        self._valid.add(channel)

    # -- read --------------------------------------------------------------

    async def list(self) -> Dict[str, List[int]]:
        async with self._lock:
            return {k: list(v) for k, v in self._groups.items()}

    async def get(self, name: str) -> Optional[List[int]]:
        async with self._lock:
            v = self._groups.get(name)
            return list(v) if v is not None else None

    # -- write -------------------------------------------------------------

    async def create(self, name: str, channels: List[int]) -> List[int]:
        name = self._validate_name(name)
        chans = self._validate_channels(channels)
        async with self._lock:
            if name in self._groups:
                raise GroupError(f"group already exists: {name!r}")
            self._groups[name] = chans
            self._persist()
            return list(chans)

    async def update(self, name: str, channels: List[int]) -> List[int]:
        chans = self._validate_channels(channels)
        async with self._lock:
            if name not in self._groups:
                raise GroupError(f"unknown group: {name!r}")
            self._groups[name] = chans
            self._persist()
            return list(chans)

    async def delete(self, name: str) -> None:
        async with self._lock:
            if name not in self._groups:
                raise GroupError(f"unknown group: {name!r}")
            del self._groups[name]
            self._persist()
