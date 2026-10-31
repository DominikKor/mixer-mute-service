"""Persistent snapshots of observed per-channel mute states."""

from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Dict, Optional


_NAME_RE = re.compile(r"^[^\x00-\x1f]{1,64}$")


class MuteSnapshotError(ValueError):
    pass


class MuteSnapshotStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._snapshots = []
        self._load()

    def _load(self) -> None:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                raise ValueError("snapshot file must contain a list")
            self._snapshots = data
        except FileNotFoundError:
            self._persist()
        except (OSError, ValueError, TypeError):
            self._snapshots = []
            self._persist()

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(self._snapshots, indent=2), encoding="utf-8")

    async def list(self) -> list:
        async with self._lock:
            return deepcopy(self._snapshots)

    async def get(self, snapshot_id: str) -> Optional[dict]:
        async with self._lock:
            snapshot = next((item for item in self._snapshots if item["id"] == snapshot_id), None)
            return deepcopy(snapshot)

    async def create(self, name: str, channels: Dict[str, bool]) -> dict:
        name = (name or "").strip()
        if not _NAME_RE.fullmatch(name):
            raise MuteSnapshotError("name must contain 1 to 64 printable characters")
        if not channels or any(
            not str(channel).isdigit() or not isinstance(muted, bool)
            for channel, muted in channels.items()
        ):
            raise MuteSnapshotError("snapshot must contain mute states for valid channels")

        async with self._lock:
            if any(item["name"].casefold() == name.casefold() for item in self._snapshots):
                raise MuteSnapshotError("snapshot name already exists")
            snapshot = {
                "id": uuid.uuid4().hex,
                "name": name,
                "created_at": time.time(),
                "channels": {str(channel): muted for channel, muted in channels.items()},
            }
            self._snapshots.append(snapshot)
            self._persist()
            return deepcopy(snapshot)

    async def delete(self, snapshot_id: str) -> bool:
        async with self._lock:
            original = len(self._snapshots)
            self._snapshots = [item for item in self._snapshots if item["id"] != snapshot_id]
            if len(self._snapshots) == original:
                return False
            self._persist()
            return True