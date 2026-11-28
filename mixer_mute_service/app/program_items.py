"""Persistent schedule rows for program playback."""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from copy import deepcopy
from datetime import datetime, time as clock_time, timedelta
from pathlib import Path
from typing import List, Optional

from .camera_program import CAMERAS, DEFAULT_WAIT_SECONDS, MAX_WAIT_SECONDS, MIN_WAIT_SECONDS
from .file_actions import file_action_error, is_file_action
from .row_files import MAX_STEPS, STEP_SONG, is_via, step_error, via_error
from .jsonstore import read_json, write_json_atomic
from .target_pcs import retarget_file_action, retarget_steps

log = logging.getLogger("mixer.program")

_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")
_ACTION_FIELDS = ("playback", "translation", "lyrics")
_DESCRIPTION_FIELDS = ("playback_description", "translation_description", "lyrics_description")
MAX_DESKTOP_PROFILES = 16  # Desktop steps of a row (app/row_files.py MAX_STEPS)
MAX_OBS_SCENE_LENGTH = 100
_SHORTCUT_ID_RE = re.compile(r"^[0-9a-f]{32}$")


def scheduled_occurrence(time_text: str, now: datetime, anchor: Optional[datetime] = None) -> datetime:
    """The moment a row's "HH:MM" means. A row is meant for ONE day, and the list is sorted by
    time within a day, so:
    - With an anchor (the scheduled moment of a row that already ran in this run) it is that
      anchor's day: the rows of a run stay together, also when the run is rehearsed at another
      time of day or runs past midnight (no flipping at 12 hours between neighbours).
    - Without one it is the occurrence closest to now: yesterday, today or tomorrow, whichever
      is nearest (10:30 at 11:00 is 30 min late, 00:10 at 23:50 is in 20 min); exactly 12 h
      away counts as today.
    The same rule is in js/program.js (programDue)."""
    clock = clock_time.fromisoformat(time_text)
    if anchor is not None:
        return datetime.combine(anchor.date(), clock, tzinfo=now.tzinfo)
    days = [now.date() + timedelta(days=shift) for shift in (0, -1, 1)]
    return min((datetime.combine(day, clock, tzinfo=now.tzinfo) for day in days), key=lambda at: abs(at - now))


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
        if is_via(value):
            # A file shown through a Desktop key: "via:<profile>|<path>" (app/row_files.py)
            problem = via_error(value)
            if problem:
                raise ProgramItemError(f"{field}: {problem}")
        elif is_file_action(value):
            # Older: a file on a PC chosen in the row, "file:<ip>|<path>" (app/file_actions.py)
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
    # Liedtext is a song or a file, not both: the song wins.
    if result["song"]:
        result["lyrics"] = None
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
    # The Desktop steps, run in this order (app/row_files.py): keys, keys
    # with another file, freeze / unfreeze, the song.
    desktop = data.get("desktop") or []
    if not isinstance(desktop, list) or len(desktop) > MAX_STEPS:
        raise ProgramItemError(f"desktop must be a list of at most {MAX_STEPS} steps")
    for step in desktop:
        problem = step_error(step)
        if problem:
            raise ProgramItemError(f"desktop step {step!r}: {problem}")
    # The song at most once (its first place counts).
    result["desktop"] = [step for i, step in enumerate(desktop) if step != STEP_SONG or desktop.index(STEP_SONG) == i]
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


# The fields validated_actions() returns: what a row (or a shortcut) does.
ACTION_KEYS = tuple(validated_actions({}))
# What "Shortcut laden" copies into a row. The song is the row's own like its
# time and title: it changes with every service (a template's song slots
# fill it), so a shortcut never replaces it.
SHORTCUT_KEYS = tuple(key for key in ACTION_KEYS if key != "song")
# Keys of rows saved while a row could use shortcuts by reference.
_LINK_KEYS = ("shortcut", "shortcuts", "shortcut_deleted")


def apply_shortcut(item: dict, shortcut: dict) -> dict:
    """"Shortcut laden": the row with the shortcut's action fields copied
    into its own (a copy: editing either later leaves the other). Time,
    title, person and song stay the row's. Liedtext is a song or a file, not
    both: a row with a song keeps no Liedtext file."""
    result = {**item, **{key: deepcopy(shortcut[key]) for key in SHORTCUT_KEYS if key in shortcut}}
    if item.get("song"):
        result["lyrics"] = None
        # The row's song keeps its step (last, if the shortcut has none for it).
        if STEP_SONG not in (result.get("desktop") or []):
            result["desktop"] = [*(result.get("desktop") or []), STEP_SONG]
    return result


def linked_shortcut_id(entry: dict) -> Optional[str]:
    """The shortcut a row of v1.2.1-210 referred to ("shortcut": it ran
    instead of the row's own fields)."""
    shortcut = entry.get("shortcut")
    return shortcut if isinstance(shortcut, str) and _SHORTCUT_ID_RE.fullmatch(shortcut) else None


def listed_shortcut_ids(entry: dict) -> list:
    """The shortcuts a row from before v1.2.1-210 listed ("shortcuts": they
    ran after the row's own steps, in this order)."""
    listed = entry.get("shortcuts")
    if not isinstance(listed, list):
        return []
    return [s for s in listed if isinstance(s, str) and _SHORTCUT_ID_RE.fullmatch(s)]


def _empty(value) -> bool:
    return value is None or value == "" or value == []


def merge_shortcut(item: dict, shortcut: dict, label: str = "") -> dict:
    """A shortcut that ran after the row's own steps (the oldest format):
    its fields are added to the row's. What the shortcut leaves empty stays
    the row's; where both set something, the shortcut's wins (it ran last;
    logged); its desktop profiles follow the row's. The song stays the row's
    and keeps the shortcut's Liedtext file out."""
    result = deepcopy(item)
    title = shortcut.get("title", shortcut.get("id"))
    for key in SHORTCUT_KEYS:
        value = shortcut.get(key)
        if key in ("desktop", "camera_wait_seconds") or _empty(value):
            continue
        if key == "lyrics" and result.get("song"):
            continue
        if not _empty(result.get(key)) and result[key] != value:
            log.warning("%s: %s %r replaced by %r from shortcut „%s“ (it ran after the row)",
                        label, key, result[key], value, title)
        result[key] = deepcopy(value)
    # The wait belongs to the camera presets: only with the shortcut's own.
    if shortcut.get("cam1_preset") or shortcut.get("cam2_preset"):
        result["camera_wait_seconds"] = shortcut.get("camera_wait_seconds", result.get("camera_wait_seconds"))
    desktop = list(dict.fromkeys([*(result.get("desktop") or []), *(shortcut.get("desktop") or [])]))
    if len(desktop) > MAX_DESKTOP_PROFILES:
        log.warning("%s: desktop profiles %s left out (at most %s)", label,
                    ", ".join(desktop[MAX_DESKTOP_PROFILES:]), MAX_DESKTOP_PROFILES)
        desktop = desktop[:MAX_DESKTOP_PROFILES]
    result["desktop"] = desktop
    return result


def unlink_shortcut(what: str, entry: dict, row: dict, shortcuts: dict, validate) -> tuple:
    """A row saved with shortcut references gets their fields into its own
    once, so it keeps doing what it did; the references go. v1.2.1-210
    ("shortcut", run instead of the row's own fields): the shortcut's fields
    replace the row's. Before ("shortcuts", run after the row's own steps,
    in order): each one's fields are merged in (merge_shortcut). Returns
    (row, "copied" / "merged" / "missing" / "none", or None without any)."""
    if not any(key in entry for key in _LINK_KEYS):
        return row, None
    when = f"{row.get('time', '')} {row.get('title', '')}".strip()
    label = f"{what} {row['id']} ({when})" if row.get("id") else f"{what} ({when})"
    shortcut_id = linked_shortcut_id(entry)
    if shortcut_id:
        shortcut = shortcuts.get(shortcut_id)
        if shortcut is None:
            log.warning("%s: its shortcut %s no longer exists; it keeps its own fields", label, shortcut_id)
            return row, "missing"
        log.warning("%s: shortcut „%s“ copied into its own fields", label, shortcut.get("title", shortcut_id))
        return validate(apply_shortcut(row, shortcut)), "copied"
    listed = listed_shortcut_ids(entry)
    if not listed:
        return row, "none"
    merged, used = row, 0
    for listed_id in listed:
        shortcut = shortcuts.get(listed_id)
        if shortcut is None:
            log.warning("%s: listed shortcut %s no longer exists; dropped", label, listed_id)
            continue
        merged = merge_shortcut(merged, shortcut, label)
        used += 1
        log.warning("%s: shortcut „%s“ (ran after its own steps) merged into its fields",
                    label, shortcut.get("title", listed_id))
    return (validate(merged), "merged") if used else (row, "missing")


def keep_references(entry: dict, row: dict) -> dict:
    """The shortcuts could not be read: the references stay for a later start."""
    return {**row, **{key: entry[key] for key in _LINK_KEYS if key in entry}}


def log_unlink_migration(what: str, results: list) -> dict:
    """The counts of unlink_shortcut() over one store, logged once."""
    counts = {status: results.count(status) for status in ("copied", "merged", "missing", "none")}
    log.info("%ss without shortcut references now: %s rows, %s got their shortcut's fields, %s merged "
             "listed shortcuts, %s referred only to deleted shortcuts", what, len(results), counts["copied"],
             counts["merged"], counts["missing"])
    return {"rows": len(results), **counts}


class ProgramItemStore:
    def __init__(self, path: Path, shortcuts: Optional[dict] = None) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._items: List[dict] = []
        self.migration: Optional[dict] = None  # what the last load migrated (unlink_shortcut)
        self._load(shortcuts)

    def _load(self, shortcuts: Optional[dict]) -> None:
        """shortcuts: id -> shortcut, for rows saved with a reference to one;
        None when the shortcuts could not be read: references stay then."""
        items = []
        unlinked = []
        for entry in read_json(self._path, list, default=[]):
            # Skip rows a hand edit or an old version left unusable instead of
            # failing later in list() or play.
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                log.warning("Skipping invalid program row in %s: %r", self._path, entry)
                continue
            if entry.get("song") and entry.get("lyrics"):
                log.warning("Program row %s (%s): a song and a Liedtext file; the song stays, the file is dropped",
                            entry.get("id"), entry.get("title"))
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
            if shortcuts is None:
                items[-1] = keep_references(entry, items[-1])
                continue
            items[-1], status = unlink_shortcut("Program row", entry, items[-1], shortcuts,
                                                lambda row: {**row, **self._validated_fields(row)})
            if status:
                unlinked.append(status)
        self._items = items
        if unlinked:
            # Saved without the references, so this runs once.
            self.migration = log_unlink_migration("Program row", unlinked)
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
        return {"time": scheduled_time, "title": title, "person": person, **validated_actions(data)}

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

    async def update_from_order(self, rows: List[dict]) -> List[dict]:
        """An order (Gottesdienstablauf) applied to the program, all or nothing.

        rows: the program in its new state; a row with an "id" is that row, one
        without is new and is put right after the row before it. Of an existing
        row only the person, the title, the song (and with a song no Liedtext
        file) and "hidden" are taken; everything else, its ID and when it ran
        stay. No row is removed.
        """
        async with self._lock:
            by_id = {item["id"]: item for item in self._items}
            if any(row.get("id") is not None and row["id"] not in by_id for row in rows):
                raise ProgramItemError("a program row was removed meanwhile")
            changes, new = [], []
            for row in rows:
                if row.get("id") is None:
                    new.append((row, self._validated_fields(row)))
                    continue
                item = by_id[row["id"]]
                fields = self._validated_fields({**item, **{k: row[k] for k in ("title", "person", "song") if k in row}})
                changes.append((item, {k: fields[k] for k in ("title", "person", "song", "lyrics")},
                                row.get("hidden", item.get("hidden")) is True))
            for item, fields, hidden in changes:
                item.update(fields)
                item["hidden"] = hidden
            previous = None
            for row in rows:
                if row.get("id") is None:
                    fields = next(f for r, f in new if r is row)
                    item = {"id": uuid.uuid4().hex, **fields, "last_delay_seconds": None, "last_played_at": None,
                            "hidden": row.get("hidden") is True}
                    self._items.insert(self._items.index(by_id[previous]) + 1 if previous else 0, item)
                    by_id[item["id"]] = item
                    previous = item["id"]
                else:
                    previous = row["id"]
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

    async def map_rows(self, change) -> int:
        """change(row, where) changes a row in place and says whether it did
        (app/row_files.py migrate_stores). Saved once if anything changed;
        returns how many rows changed."""
        async with self._lock:
            changed = sum(1 for item in self._items if change(item, f"row {item['time']} {item['title']!r}"))
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
                changed += retarget_steps(item, moves)
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

    def _run_anchor(self, now: datetime, skip_id: str) -> Optional[datetime]:
        """The scheduled moment of the row that started last today (not skip_id): what it was
        meant for is "played at - delay". None when no other row ran today."""
        today = now.date().isoformat()
        ran = [item for item in self._items if item["id"] != skip_id and isinstance(item.get("last_played_at"), str)
               and item["last_played_at"].startswith(today) and isinstance(item.get("last_delay_seconds"), (int, float))]
        if not ran:
            return None
        last = max(ran, key=lambda item: item["last_played_at"])
        try:
            return datetime.fromisoformat(last["last_played_at"]) - timedelta(seconds=last["last_delay_seconds"])
        except ValueError:
            return None

    async def mark_played(self, item_id: str, now: Optional[datetime] = None) -> Optional[dict]:
        now = now or datetime.now().astimezone()
        async with self._lock:
            item = next((entry for entry in self._items if entry["id"] == item_id), None)
            if item is None:
                return None
            try:
                scheduled = scheduled_occurrence(item["time"], now, self._run_anchor(now, item_id))
                item["last_delay_seconds"] = round((now - scheduled).total_seconds())
            except ValueError:  # a row without a usable time has no delay
                item["last_delay_seconds"] = None
            item["last_played_at"] = now.isoformat(timespec="seconds")
            self._persist()
            return deepcopy(item)
