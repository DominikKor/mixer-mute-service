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

from .camera_program import CAMERAS, DEFAULT_WAIT_SECONDS, MAX_WAIT_SECONDS, MIN_WAIT_SECONDS
from .desktop_profiles import PROFILE_ID_RE
from .file_actions import file_action_error, is_file_action
from .jsonstore import read_json, write_json_atomic
from .target_pcs import retarget_file_action

log = logging.getLogger("mixer.program")

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_ACTION_FIELDS = ("playback", "translation", "lyrics")
_DESCRIPTION_FIELDS = ("playback_description", "translation_description", "lyrics_description")
MAX_DESKTOP_PROFILES = 8
MAX_OBS_SCENE_LENGTH = 100
MAX_SHORTCUTS = 8  # only for reading the old "shortcuts" list
_SHORTCUT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


class ProgramItemError(ValueError):
    pass


def validated_actions(data: dict) -> dict:
    """What a program row or a shortcut does when it runs (everything but its
    name, time and person); raises ProgramItemError."""
    # Optional: without a group the mutes stay as they are and only the
    # actions run (e.g. a camera change during a sermon).
    group = str(data.get("group") or "").strip()
    if len(group) > 64:
        raise ProgramItemError("group must be at most 64 characters")
    result = {"group": group}
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
    # Camera presets (app/camera_program.py): one per camera, or none.
    for cam in CAMERAS:
        value = data.get(f"{cam}_preset")
        if isinstance(value, str) and value.strip().isdigit():
            value = int(value.strip())
        if value in (None, "", 0):
            value = None
        elif type(value) is not int or not 1 <= value <= 16:
            raise ProgramItemError(f"{cam}_preset must be a preset 1 to 16 or empty")
        result[f"{cam}_preset"] = value
    wait = data.get("camera_wait_seconds")
    if wait in (None, ""):
        wait = DEFAULT_WAIT_SECONDS
    try:
        wait = float(str(wait).replace(",", "."))
    except ValueError:
        raise ProgramItemError("camera_wait_seconds must be a number") from None
    if not MIN_WAIT_SECONDS <= wait <= MAX_WAIT_SECONDS:
        raise ProgramItemError(
            f"camera_wait_seconds must be {MIN_WAIT_SECONDS:g} to {MAX_WAIT_SECONDS:g} seconds")
    result["camera_wait_seconds"] = wait
    light = data.get("light")
    if light is not None and (not isinstance(light, str) or len(light) > 64):
        raise ProgramItemError("light must be a light scene id or empty")
    result["light"] = light or None
    # Desktop profiles shown or started when the row starts, in this order.
    desktop = data.get("desktop") or []
    if (not isinstance(desktop, list) or len(desktop) > MAX_DESKTOP_PROFILES
            or any(not isinstance(p, str) or not PROFILE_ID_RE.fullmatch(p) for p in desktop)):
        raise ProgramItemError(f"desktop must be a list of at most {MAX_DESKTOP_PROFILES} desktop profile IDs")
    result["desktop"] = list(dict.fromkeys(desktop))
    # OBS scene per camera (app/camera_program.py); empty: the camera's "Cam N Live" key.
    for cam in CAMERAS:
        scene = data.get(f"{cam}_scene")
        if scene is not None and (
                not isinstance(scene, str) or len(scene) > MAX_OBS_SCENE_LENGTH
                or any(ord(c) < 32 for c in scene)):
            raise ProgramItemError(f"{cam}_scene must be an OBS scene name of at most {MAX_OBS_SCENE_LENGTH} characters")
        result[f"{cam}_scene"] = (scene or "").strip() or None
    # Beamer: "hide", "unhide" or None (left as it is).
    beamer = data.get("beamer") or None
    if beamer not in (None, "hide", "unhide"):
        raise ProgramItemError("beamer must be hide, unhide or empty")
    result["beamer"] = beamer
    return result


# The fields validated_actions() returns: what a row (or its shortcut) does.
ACTION_KEYS = tuple(validated_actions({}))
# Those that do something when set (descriptions and the camera wait alone do nothing).
_ACTIVE_KEYS = ("group", *_ACTION_FIELDS, "song", "light", "cam1_preset", "cam2_preset",
                "desktop", "cam1_scene", "cam2_scene", "beamer")


def has_own_actions(item: dict) -> bool:
    """Whether a row does anything by its own fields."""
    return any(item.get(key) for key in _ACTIVE_KEYS)


def validated_shortcut_id(data: dict) -> Optional[str]:
    """The row's shortcut: its whole configuration (app/shortcuts.py). Rows
    saved before took a list of shortcuts that ran after their own steps
    ("shortcuts"); the first of them is now the shortcut."""
    shortcut = data.get("shortcut")
    if shortcut in (None, ""):
        legacy = data.get("shortcuts") or []
        if (not isinstance(legacy, list) or len(legacy) > MAX_SHORTCUTS
                or any(not isinstance(s, str) or not _SHORTCUT_ID_RE.fullmatch(s) for s in legacy)):
            raise ProgramItemError(f"shortcuts must be a list of at most {MAX_SHORTCUTS} shortcut IDs")
        return legacy[0] if legacy else None
    if not isinstance(shortcut, str) or not _SHORTCUT_ID_RE.fullmatch(shortcut):
        raise ProgramItemError("shortcut must be a shortcut ID or empty")
    return shortcut


class ProgramItemStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._items: List[dict] = []
        self.migration: Optional[dict] = None  # what the last load migrated (see _migrate_shortcuts)
        self._load()

    def _load(self) -> None:
        items = []
        migrated = []  # rows still saved with the old list of shortcuts
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
                "hidden": entry.get("hidden") is True,
            })
            if "shortcuts" in entry:
                migrated.append((entry, items[-1]))
        self._items = items
        if migrated:
            self._migrate_shortcuts(migrated)

    def _migrate_shortcuts(self, migrated: list) -> None:
        """Rows used to run a list of shortcuts after their own steps; now one
        shortcut is the row's whole configuration (the first of the list).
        Rows that now do something else are logged; their own fields stay
        saved. Saved without the old list, so this runs once."""
        several = [(item, entry["shortcuts"][1:]) for entry, item in migrated
                   if isinstance(entry["shortcuts"], list) and len(entry["shortcuts"]) > 1]
        replaced = [item for _entry, item in migrated if item["shortcut"] and has_own_actions(item)]
        for item, dropped in several:
            log.warning("Program row %s (%s %s): only its first shortcut stays; %s no longer run",
                        item["id"], item["time"], item["title"], ", ".join(dropped))
        for item in replaced:
            log.warning("Program row %s (%s %s): runs its shortcut %s instead of its own steps now",
                        item["id"], item["time"], item["title"], item["shortcut"])
        log.info("Program rows migrated to one shortcut each: %s rows, %s had several shortcuts, "
                 "%s had own steps and a shortcut", len(migrated), len(several), len(replaced))
        self.migration = {"rows": len(migrated), "several_shortcuts": [item["id"] for item, _ in several],
                          "own_steps_and_shortcut": [item["id"] for item in replaced]}
        self._persist()

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
        result = {"time": scheduled_time, "title": title, "person": person, **validated_actions(data)}
        # With a shortcut the row runs the shortcut's fields instead of its own
        # (app/actions.py); its own stay saved for when the shortcut is removed.
        result["shortcut"] = validated_shortcut_id(data)
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
        is next. Without a start today, the first row is next. Hidden rows are
        skipped: "next" is the first visible row after the current one.
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
            upcoming = (item for item in ordered[following:] if not item.get("hidden"))
            next_item = next(upcoming, None)
            return {
                "current_id": None if current is None else ordered[current]["id"],
                "next_id": None if next_item is None else next_item["id"],
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
                "hidden": False,
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

    async def replace_all(self, rows: List[dict], preserve_hidden: bool = False) -> List[dict]:
        """Replace the whole program (a template applied); all or nothing."""
        fields = [self._validated_fields(row) for row in rows]
        async with self._lock:
            self._items = [
                {
                    "id": uuid.uuid4().hex,
                    **validated,
                    "last_delay_seconds": None,
                    "last_played_at": None,
                    "hidden": preserve_hidden and source.get("hidden") is True,
                }
                for source, validated in zip(rows, fields)
            ]
            self._persist()
            return deepcopy(self._ordered())

    async def set_hidden(self, item_id: str, hidden: bool) -> Optional[dict]:
        """Grey a row out (it stays in the list but "Weiter" skips it) or show it again."""
        async with self._lock:
            item = next((entry for entry in self._items if entry["id"] == item_id), None)
            if item is None:
                return None
            if item.get("hidden") is not hidden:
                item["hidden"] = hidden
                self._persist()
            return deepcopy(item)

    async def drop_shortcut(self, shortcut_id: str) -> int:
        """A deleted shortcut leaves every row that used it: those rows run
        their own fields again."""
        async with self._lock:
            changed = 0
            for item in self._items:
                if item.get("shortcut") == shortcut_id:
                    item["shortcut"] = None
                    changed += 1
            if changed:
                self._persist()
            return changed

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

    async def retarget_files(self, moves: dict) -> int:
        """Point file actions on a PC whose address changed at the new one."""
        async with self._lock:
            changed = 0
            for item in self._items:
                for field in _ACTION_FIELDS:
                    new = retarget_file_action(item.get(field), moves)
                    if new != item.get(field):
                        item[field] = new
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
