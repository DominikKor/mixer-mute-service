"""Shortcuts: what a program row does (mute group, light, files, song,
cameras, OBS scene, desktop profiles, beamer), saved under a name and run
from a key in the Desktop section or from program rows. Starting a shortcut
never touches the program's run order or start times."""

from __future__ import annotations

import asyncio
import logging
import uuid
from copy import deepcopy
from pathlib import Path
from typing import List, Optional

from .jsonstore import CorruptJSONError, read_json, write_json_atomic
from .program_items import _ACTION_FIELDS, ProgramItemError, validated_actions
from .target_pcs import retarget_file_action, retarget_steps

log = logging.getLogger("mixer.shortcuts")

MAX_SHORTCUTS = 60


def validated_shortcut(data: dict) -> dict:
    title = str(data.get("title", "")).strip()
    if not title or len(title) > 120:
        raise ProgramItemError("title must contain 1 to 120 characters")
    return {"title": title, **validated_actions(data)}


class ShortcutStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._items: List[dict] = []
        # False when the file or a shortcut in it could not be read: rows that
        # refer to shortcuts then keep their references (by_id() is None).
        self.complete = True
        try:
            entries = read_json(path, list)
        except FileNotFoundError:
            entries = []
        except CorruptJSONError:
            entries, self.complete = [], False
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                log.warning("Skipping invalid shortcut in %s: %r", path, entry)
                self.complete = False
                continue
            try:
                self._items.append({"id": entry["id"], **validated_shortcut(entry)})
            except ProgramItemError as exc:
                log.warning("Skipping invalid shortcut %s: %s", entry.get("id"), exc)
                self.complete = False

    def by_id(self) -> Optional[dict]:
        """Shortcut id -> shortcut (for rows saved with a reference to one, see
        app/program_items.py unlink_shortcut); None unless all could be read."""
        if not self.complete:
            return None
        return {item["id"]: deepcopy(item) for item in self._items}

    def _persist(self) -> None:
        write_json_atomic(self._path, self._items, ensure_ascii=False)

    def _index(self, shortcut_id: str) -> Optional[int]:
        return next((i for i, entry in enumerate(self._items) if entry["id"] == shortcut_id), None)

    async def list(self) -> List[dict]:
        async with self._lock:
            return deepcopy(self._items)

    async def get(self, shortcut_id: str) -> Optional[dict]:
        async with self._lock:
            index = self._index(shortcut_id)
            return None if index is None else deepcopy(self._items[index])

    async def create(self, data: dict, after: Optional[str] = None) -> dict:
        fields = validated_shortcut(data)
        async with self._lock:
            if len(self._items) >= MAX_SHORTCUTS:
                raise ProgramItemError(f"at most {MAX_SHORTCUTS} shortcuts")
            item = {"id": uuid.uuid4().hex, **fields}
            index = self._index(after) if after else None
            self._items.insert(len(self._items) if index is None else index + 1, item)
            self._persist()
            return deepcopy(item)

    async def update(self, shortcut_id: str, data: dict) -> Optional[dict]:
        fields = validated_shortcut(data)
        async with self._lock:
            index = self._index(shortcut_id)
            if index is None:
                return None
            self._items[index] = {"id": shortcut_id, **fields}
            self._persist()
            return deepcopy(self._items[index])

    async def duplicate(self, shortcut_id: str) -> Optional[dict]:
        """A copy named "… (Kopie)", placed right after the original."""
        source = await self.get(shortcut_id)
        if source is None:
            return None
        title = f"{source['title']} (Kopie)"[:120]
        return await self.create({**source, "title": title}, after=shortcut_id)

    async def delete(self, shortcut_id: str) -> bool:
        async with self._lock:
            index = self._index(shortcut_id)
            if index is None:
                return False
            del self._items[index]
            self._persist()
            return True

    async def rename_group(self, old: str, new: str) -> int:
        async with self._lock:
            changed = 0
            for item in self._items:
                if item.get("group") == old:
                    item["group"] = new
                    changed += 1
            if changed:
                self._persist()
            return changed

    async def map_rows(self, change) -> int:
        """As ProgramItemStore.map_rows, for every shortcut."""
        async with self._lock:
            changed = sum(1 for item in self._items if change(item, f"shortcut {item.get('title')!r}"))
            if changed:
                self._persist()
            return changed

    async def retarget_files(self, moves: dict) -> int:
        async with self._lock:
            changed = 0
            for item in self._items:
                for field in _ACTION_FIELDS:
                    new = retarget_file_action(item.get(field), moves)
                    if new != item.get(field):
                        item[field] = new
                        changed += 1
                changed += retarget_steps(item, moves)
            if changed:
                self._persist()
            return changed
