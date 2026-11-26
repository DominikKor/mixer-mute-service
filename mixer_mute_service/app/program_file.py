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


def _text(value) -> Optional[str]:
    """The value if it is a text; anything else is left to the validation of the row."""
    return value if isinstance(value, str) else None


def _only(matches: List[str]) -> Tuple[Optional[str], Optional[str]]:
    """(id, problem): the one match, else None and "nicht gefunden" or "mehrdeutig (n passen)"."""
    if len(matches) == 1:
        return matches[0], None
    if not matches:
        return None, "nicht gefunden"
    return None, f"mehrdeutig ({len(matches)} passen)"


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
    """The row with every reference that does not exist here found by its name
    (the *_name field, or the value itself when someone typed a name into the
    ID field), or dropped. The second value lists what was dropped:
    [{field, kind, name, reason}], reasons in German. Only texts are looked at;
    other values stay for the validation of the row (a clean 400)."""
    row, dropped = dict(row), []

    def drop(field: str, kind: str, name: str, problem: str) -> None:
        dropped.append({"field": field, "kind": kind, "name": name,
                        "reason": f"{kind} „{name}“ {problem}; Verweis entfernt"})

    value = _text(row.get("light"))
    if value and value not in names.scenes:
        name = _text(row.get("light_name")) or value
        found, problem = _only([i for i, n in names.scenes.items() if _same(n, name)])
        row["light"] = found
        if problem:
            drop("light", "Licht", name, problem)
    for field in _KEY_FIELDS:
        value = _text(row.get(field))
        if not value or is_file_action(value):
            continue
        name = _text(row.get(f"{field}_name"))
        if value.startswith(DESKTOP_PREFIX):  # a desktop profile stays one: only profiles are searched
            if value.removeprefix(DESKTOP_PREFIX) in names.profiles:
                continue
            name = name or value.removeprefix(DESKTOP_PREFIX)
            found, problem = _only([f"{DESKTOP_PREFIX}{i}" for i, t in names.profiles.items() if _same(t, name)])
            kind = "Desktop-Profil"
        else:  # a key stays one: only keys are searched
            if await pressable_button(names.request, value) is not None:
                continue
            name = name or value
            found, problem = _only([i for i, label in names.buttons.items() if _same(label, name)])
            kind = "Taste"
        row[field] = found
        if problem:
            drop(field, kind, name, problem)
    profiles = row.get("desktop")
    if isinstance(profiles, list):
        titles = row.get("desktop_names") if isinstance(row.get("desktop_names"), list) else []
        kept = []
        for index, pid in enumerate(profiles):
            if not isinstance(pid, str) or pid in names.profiles:
                kept.append(pid)  # (a wrong type is for the validation)
                continue
            name = (_text(titles[index]) if index < len(titles) else None) or pid
            found, problem = _only([i for i, t in names.profiles.items() if _same(t, name)])
            if found:
                kept.append(found)
            else:
                drop("desktop", "Desktop-Profil", name, problem)
        row["desktop"] = kept
    value = _text(row.get("song"))
    if value and names.songs and value not in names.songs:  # the library may not be loaded: then it stays
        title = _text(row.get("song_title")) or value
        matches = [i for i, song in names.songs.items()
                   if any(_same(t, title) for t in song.get("titles") or [song["title"]])]
        book = _text(row.get("song_book"))
        if len(matches) > 1 and book:  # the same title in several books: the book tells which
            by_book = [i for i in matches if _same(names.song_book(names.songs[i]), book)]
            matches = by_book or matches
        found, problem = _only(matches)
        row["song"] = found
        if problem:
            drop("song", "Lied", title, problem)
    group = _text(row.get("group"))
    if group:
        groups = await names.request.app.state.groups.list()
        if group not in groups:
            found, problem = _only([g for g in groups if _same(g, group)])
            row["group"] = found or ""
            if problem:
                drop("group", "Mute-Gruppe", group, problem)
    return row, dropped
