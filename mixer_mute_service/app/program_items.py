"""Persistent schedule rows for program playback."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from copy import deepcopy
from datetime import datetime, time as clock_time
from pathlib import Path
from typing import List, Optional

from .jsonstore import read_json, write_json_atomic

log = logging.getLogger("mixer.program")

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_ACTION_FIELDS = ("playback", "translation", "lyrics")
_DESCRIPTION_FIELDS = ("playback_description", "translation_description", "lyrics_description")


class ProgramItemError(ValueError):
    pass


class ProgramItemStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._items: List[dict] = []
        self._load()

    def _load(self) -> None:
        items = []
        for entry in read_json(self._path, list, default=[]):
            # Skip rows a hand edit or an old version left unusable instead of
            # failing later in list() or play.
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                log.warning("Skipping invalid program row in %s: %r", self._path, entry)
                continue
            try:
                fields = self._validated_fields(entry)
            except ProgramItemError as exc:
                log.warning("Skipping invalid program row %s: %s", entry.get("id"), exc)
                continue
            items.append({
                "id": entry["id"],
                **fields,
                "last_delay_seconds": entry.get("last_delay_seconds"),
                "last_played_at": entry.get("last_played_at"),
            })
        self._items = items

    def _persist(self) -> None:
        write_json_atomic(self._path, self._items, ensure_ascii=False)

    def _validated_fields(self, data: dict) -> dict:
        scheduled_time = str(data.get("time", "")).strip()
        if not _TIME_RE.fullmatch(scheduled_time):
            raise ProgramItemError("time must use HH:MM")
        title = str(data.get("title", "")).strip()
        if not title or len(title) > 120:
            raise ProgramItemError("title must contain 1 to 120 characters")
        person = str(data.get("person", "")).strip()
        if len(person) > 80:
            raise ProgramItemError("person must be at most 80 characters")
        group = str(data.get("group", "")).strip()
        if not group or len(group) > 64:
            raise ProgramItemError("group is required")
        result = {"time": scheduled_time, "title": title, "person": person, "group": group}
        for field in _ACTION_FIELDS:
            value = data.get(field)
            if value is not None and (not isinstance(value, str) or len(value) > 80):
                raise ProgramItemError(f"{field} must be a button ID or empty")
            result[field] = value or None
        for field in _DESCRIPTION_FIELDS:
            value = data.get(field, "")
            if not isinstance(value, str) or len(value) > 160:
                raise ProgramItemError(f"{field} must be at most 160 characters")
            result[field] = value.strip()
        return result

    async def list(self) -> List[dict]:
        async with self._lock:
            return sorted(deepcopy(self._items), key=lambda item: (item["time"], item["id"]))

    async def get(self, item_id: str) -> Optional[dict]:
        async with self._lock:
            item = next((entry for entry in self._items if entry["id"] == item_id), None)
            return deepcopy(item)

    async def create(self, data: dict) -> dict:
        fields = self._validated_fields(data)
        async with self._lock:
            item = {
                "id": uuid.uuid4().hex,
                **fields,
                "last_delay_seconds": None,
                "last_played_at": None,
            }
            self._items.append(item)
            self._persist()
            return deepcopy(item)

    async def update(self, item_id: str, data: dict) -> Optional[dict]:
        fields = self._validated_fields(data)
        async with self._lock:
            item = next((entry for entry in self._items if entry["id"] == item_id), None)
            if item is None:
                return None
            item.update(fields)
            self._persist()
            return deepcopy(item)

    async def delete(self, item_id: str) -> bool:
        async with self._lock:
            original = len(self._items)
            self._items = [entry for entry in self._items if entry["id"] != item_id]
            if len(self._items) == original:
                return False
            self._persist()
            return True

    async def mark_played(self, item_id: str, now: Optional[datetime] = None) -> Optional[dict]:
        now = now or datetime.now().astimezone()
        async with self._lock:
            item = next((entry for entry in self._items if entry["id"] == item_id), None)
            if item is None:
                return None
            scheduled = datetime.combine(now.date(), clock_time.fromisoformat(item["time"]), tzinfo=now.tzinfo)
            delay = (now - scheduled).total_seconds()
            if delay > 12 * 60 * 60:
                delay -= 24 * 60 * 60
            elif delay < -12 * 60 * 60:
                delay += 24 * 60 * 60
            item["last_delay_seconds"] = round(delay)
            item["last_played_at"] = now.isoformat(timespec="seconds")
            self._persist()
            return deepcopy(item)
