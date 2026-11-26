"""Readable names in the program file (/program/export, /program/import).

A row refers to a light scene, desktop profiles, keys, a song and camera
presets by ID. The export keeps those IDs and puts a readable name next to
each (``"light": "a1b2...", "light_name": "Predigt hell"``), so a person can
read and edit the file. The import trusts the ID when it exists on this
installation; if it does not, an exact name (any case) that fits exactly one
item is used instead; otherwise the reference is dropped and the import says so.
Unknown ``*_name`` fields are ignored (pydantic drops them), so files without
names import as before and files with names import on an older service.
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from fastapi import Request

from .actions import DESKTOP_PREFIX, PROGRAM_ACTION_FIELDS, pressable_button
from .camera_program import CAMERAS
from .companion_buttons import CATALOG_ONLY_IDS, COMPANION_BUTTONS_BY_ID
from .file_actions import is_file_action

_KEY_FIELDS = PROGRAM_ACTION_FIELDS  # playback, translation, lyrics: a key or "desktop:<profile>"


def _same(a: str, b: str) -> bool:
    return " ".join(a.split()).casefold() == " ".join(b.split()).casefold()


def _only(matches: List[str], what: str, name: str) -> Tuple[Optional[str], Optional[str]]:
    """(id, reason): the one match, else None and why not."""
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, f"{what} '{name}' not found; reference dropped"
    return None, f"{what} '{name}' is ambiguous ({len(matches)} fit); reference dropped"


class Names:
    """What the IDs of a row stand for on this installation."""

    def __init__(self, request: Request, scenes: list, buttons: dict) -> None:
        self.request = request
        self.scenes = {scene["id"]: scene["name"] for scene in scenes}
        self.profiles = {pid: p.title for pid, p in request.app.state.cfg.desktop_display.profiles.items()}
        self.buttons = buttons  # id -> label
        self.songs = request.app.state.songs.songs

    @classmethod
    async def load(cls, request: Request) -> "Names":
        state = request.app.state
        scenes = await state.light_scenes.list() if getattr(state, "light_scenes", None) is not None else []
        buttons = {}
        store = state.companion_button_store
        if store is not None:
            buttons = {b["id"]: b["label"] for b in (await store.snapshot())["buttons"]}
            buttons.update({b["id"]: b["label"] for b in await store.camera_buttons()})
        buttons.update({i: COMPANION_BUTTONS_BY_ID[i]["label"] for i in CATALOG_ONLY_IDS})
        return cls(request, scenes, buttons)

    # -- export ------------------------------------------------------------

    def key_name(self, value: str) -> Optional[str]:
        if value.startswith(DESKTOP_PREFIX):
            return self.profiles.get(value.removeprefix(DESKTOP_PREFIX))
        return self.buttons.get(value)

    def song_book(self, song: dict) -> str:
        return ", ".join(f"{b.get('name', '')} {b.get('number', '')}".strip() for b in song.get("books") or [])

    async def preset_name(self, camera: str, preset) -> Optional[str]:
        store = self.request.app.state.companion_button_store
        button = await store.camera_preset(camera, preset) if store is not None and isinstance(preset, int) else None
        return f"{preset} · {button['label']}" if button else None


async def annotate(row: dict, names: Names) -> dict:
    """The row with a readable name after each ID it has (only where one is known)."""
    result = {}
    for field, value in row.items():
        result[field] = value
        if field == "light" and value in names.scenes:
            result["light_name"] = names.scenes[value]
        elif field in _KEY_FIELDS and isinstance(value, str) and not is_file_action(value):
            name = names.key_name(value)
            if name:
                result[f"{field}_name"] = name
        elif field == "desktop" and value:
            result["desktop_names"] = [names.profiles.get(pid) for pid in value]
        elif field == "song" and value in names.songs:
            result["song_title"] = names.songs[value]["title"]
            book = names.song_book(names.songs[value])
            if book:
                result["song_book"] = book
        elif field in (f"{cam}_preset" for cam in CAMERAS) and value:
            name = await names.preset_name(field.removesuffix("_preset"), value)
            if name:
                result[f"{field}_name"] = name
    return result


# -- import ----------------------------------------------------------------------

async def resolve(row: dict, names: Names) -> Tuple[dict, List[dict]]:
    """The row with every reference that does not exist here found by its name,
    or dropped; the second value lists what was dropped: [{field, reason}]."""
    row, dropped = dict(row), []

    def drop(field: str, reason: str) -> None:
        dropped.append({"field": field, "reason": reason})

    value = row.get("light")
    if value and value not in names.scenes:
        name = row.get("light_name")
        found, reason = _only([i for i, n in names.scenes.items() if name and _same(n, str(name))], "light scene",
                              name or value)
        row["light"] = found
        if reason:
            drop("light", reason)
    for field in _KEY_FIELDS:
        value = row.get(field)
        if not value or not isinstance(value, str) or is_file_action(value):
            continue
        if value.startswith(DESKTOP_PREFIX):
            if value.removeprefix(DESKTOP_PREFIX) in names.profiles:
                continue
        elif await pressable_button(names.request, value) is not None:
            continue
        name = row.get(f"{field}_name")
        matches = [] if not isinstance(name, str) else (
            [f"{DESKTOP_PREFIX}{i}" for i, t in names.profiles.items() if _same(t, name)]
            + [i for i, label in names.buttons.items() if _same(label, name)])
        found, reason = _only(list(dict.fromkeys(matches)), "key", name or value)
        row[field] = found
        if reason:
            drop(field, reason)
    profiles = row.get("desktop")
    if isinstance(profiles, list):
        titles = row.get("desktop_names") if isinstance(row.get("desktop_names"), list) else []
        kept = []
        for index, pid in enumerate(profiles):
            if pid in names.profiles:
                kept.append(pid)
                continue
            name = titles[index] if index < len(titles) and isinstance(titles[index], str) else None
            found, reason = _only([i for i, t in names.profiles.items() if name and _same(t, name)],
                                  "desktop profile", name or pid)
            if found:
                kept.append(found)
            else:
                drop("desktop", reason)
        row["desktop"] = kept
    value = row.get("song")
    if value and names.songs and value not in names.songs:  # the library may not be loaded: then it stays
        title = row.get("song_title")
        found, reason = _only([i for i, s in names.songs.items() if isinstance(title, str) and any(
            _same(t, title) for t in s.get("titles") or [s["title"]])], "song", title or value)
        row["song"] = found
        if reason:
            drop("song", reason)
    group = row.get("group")
    if group and await names.request.app.state.groups.get(group) is None:
        drop("group", f"mute group '{group}' not found; reference dropped")
        row["group"] = ""
    return row, dropped
