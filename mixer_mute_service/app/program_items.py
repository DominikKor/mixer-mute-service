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

from .file_actions import file_action_error, is_file_action
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

    @staticmethod
    def _validated_fields(data: dict) -> dict:
        scheduled_time = str(data.get("time", "")).strip()
        if not _TIME_RE.fullmatch(scheduled_time):
            raise ProgramItemError("time must use HH:MM")
        title = str(data.get("title", "")).strip()
        if not title or len(title) > 120:
            raise ProgramItemError("title must contain 1 to 120 characters")
        person = str(data.get("person", "")).strip()
        if len(person) > 80:
            raise ProgramItemError("person must be at most 80 characters")
        # Optional: a row without a group leaves all mutes as they are and
        # only runs its actions (e.g. a camera change during a sermon).
        group = str(data.get("group") or "").strip()
        if len(group) > 64:
            raise ProgramItemError("group must be at most 64 characters")
        result = {"time": scheduled_time, "title": title, "person": person, "group": group}
        for field in _ACTION_FIELDS:
            value = data.get(field)
            if is_file_action(value):
                # A file opened directly: "file:<ip>|<path>" (app/file_actions.py)
                problem = file_action_error(value)
                if problem:
                    raise ProgramItemError(f"{field}: {problem}")
            elif value is not None and (not isinstance(value, str) or len(value) > 80):
                raise ProgramItemError(f"{field} must be a button ID or empty")
            result[field] = value or None
        for field in _DESCRIPTION_FIELDS:
            value = data.get(field, "")
            if not isinstance(value, str) or len(value) > 160:
                raise ProgramItemError(f"{field} must be at most 160 characters")
            result[field] = value.strip()
        song = data.get("song")
        if song is not None and (not isinstance(song, str) or len(song) > 200):
            raise ProgramItemError("song must be a song file name or empty")
        result["song"] = song or None
        return result

    def _ordered(self) -> List[dict]:
        # By time; rows with the same time keep the order they were added in
        # (a duplicate sits right after its original).
        return sorted(self._items, key=lambda item: item["time"])

    async def list(self) -> List[dict]:
        async with self._lock:
            return deepcopy(self._ordered())

    async def status(self, now: Optional[datetime] = None) -> dict:
        """Which row ran last today ("current") and which comes next.

        Only starts from today count, so last week's run does not decide what
        is next. Without a start today, the first row is next.
        """
        today = (now or datetime.now().astimezone()).date().isoformat()
        async with self._lock:
            ordered = self._ordered()
            played = [
                (item["last_played_at"], index) for index, item in enumerate(ordered)
                if isinstance(item.get("last_played_at"), str) and item["last_played_at"].startswith(today)
            ]
            current = max(played)[1] if played else None
            following = 0 if current is None else current + 1
            return {
                "current_id": None if current is None else ordered[current]["id"],
                "next_id": ordered[following]["id"] if following < len(ordered) else None,
            }

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

    async def duplicate(self, item_id: str) -> Optional[dict]:
        """Copy a row; the copy gets the same time and sits right after it."""
        async with self._lock:
            index = next((i for i, entry in enumerate(self._items) if entry["id"] == item_id), None)
            if index is None:
                return None
            item = {
                **deepcopy(self._items[index]),
                "id": uuid.uuid4().hex,
                "last_delay_seconds": None,
                "last_played_at": None,
            }
            self._items.insert(index + 1, item)
            self._persist()
            return deepcopy(item)

    async def replace_all(self, rows: List[dict]) -> List[dict]:
        """Replace the whole program (a template applied); all or nothing."""
        fields = [self._validated_fields(row) for row in rows]
        async with self._lock:
            self._items = [
                {"id": uuid.uuid4().hex, **row, "last_delay_seconds": None, "last_played_at": None}
                for row in fields
            ]
            self._persist()
            return deepcopy(self._ordered())

    async def reset_played(self) -> int:
        """Forget all start times and delays (a new service or a rehearsal)."""
        async with self._lock:
            count = 0
            for item in self._items:
                if item.get("last_played_at") or item.get("last_delay_seconds") is not None:
                    item["last_played_at"] = None
                    item["last_delay_seconds"] = None
                    count += 1
            if count:
                self._persist()
            return count

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
