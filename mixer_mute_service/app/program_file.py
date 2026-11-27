"""Names in the program file (/program/export, /program/import).

A row refers to a light scene, keys, desktop profiles, a song and camera
presets. Where the ID is opaque (a light scene's hex ID, a user key ``user-...``,
a saved camera preset) the export writes the NAME instead
(``"light": "Predigt hell"``, a Desktop step ``"key:Kollekte Musik"``), so a
person can read and edit the file: names of scenes and keys are unique
(case-insensitive), so they serve as keys. Where the ID is readable already
(a Desktop key ``qr`` or ``via:qr|<path>``, a group, a song file, a catalog
key) it stays; the title next to it (``desktop_names``, ``song_title`` ...)
is only for reading and is ignored by the import. A name that is not unique
cannot be a key: that reference keeps its ID (with ``light_name`` /
``<field>_name`` / its ``desktop_names`` entry next to it) and the export
lists it under "warnings".

The import trusts an ID that exists on this installation; otherwise it takes the
value (or the ``*_name`` field of an older file) as an exact name, any case; a
name that fits no or several items drops the reference and is reported in German.
Files with IDs and ``*_name`` fields (older exports) therefore import as before.
Unknown fields are ignored (pydantic drops them), so files with names also import
on an older service.
"""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from fastapi import Request

from .actions import DESKTOP_PREFIX, PROGRAM_ACTION_FIELDS, pressable_button
from .camera_program import CAMERAS
from .companion_buttons import CATALOG_ONLY_IDS, COMPANION_BUTTONS_BY_ID, _saved_preset_key, label_key
from .file_actions import is_file_action
from .row_files import KEY_PREFIX, PSEUDO_STEPS, STEP_FREEZE, STEP_SONG, STEP_UNFREEZE, is_via, parse_via, via_value

# Names of the Desktop steps that are no key (app/row_files.py).
STEP_NAMES = {STEP_FREEZE: "Beamer einfrieren", STEP_UNFREEZE: "Beamer auftauen", STEP_SONG: "Liedtext"}

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
        # The keys somebody named (user keys, saved presets): their names are the keys of a program file.
        self.named_buttons = {i: label for i, label in buttons.items() if _opaque(i)}
        self.songs = request.app.state.songs.songs
        self.ids = set(self.scenes) | set(buttons) | {f"{DESKTOP_PREFIX}{i}" for i in self.profiles}

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
        if is_via(value):  # a file through a Desktop key: the key's name
            try:
                return self.profiles.get(parse_via(value)[0])
            except ValueError:
                return None
        if value.startswith(DESKTOP_PREFIX):
            return self.profiles.get(value.removeprefix(DESKTOP_PREFIX))
        return self.buttons.get(value)

    def step_name(self, step) -> Optional[str]:
        """A Desktop step's readable name: the key's title (also for a key
        with another file), "Beamer einfrieren" and so on, a Steuerung key's
        label; None for an older file step."""
        if not isinstance(step, str):
            return None
        if step in STEP_NAMES:
            return STEP_NAMES[step]
        if is_via(step):
            try:
                return self.profiles.get(parse_via(step)[0])
            except ValueError:
                return None
        if step.startswith(KEY_PREFIX):
            return self.buttons.get(step.removeprefix(KEY_PREFIX))
        return self.profiles.get(step)

    def find_key(self, name: str) -> List[str]:
        """The keys with that name. A user key may be named like a catalog
        key: the keys somebody named come first."""
        return ([i for i, label in self.named_buttons.items() if _same(label, name)]
                or [i for i, label in self.buttons.items() if _same(label, name)])

    def song_book(self, song: dict) -> str:
        return ", ".join(f"{b.get('name', '')} {b.get('number', '')}".strip() for b in song.get("books") or [])

    async def preset_name(self, camera: str, preset) -> Optional[str]:
        store = self.request.app.state.companion_button_store
        button = await store.camera_preset(camera, preset) if store is not None and isinstance(preset, int) else None
        return f"{preset} · {button['label']}" if button else None


_HEX_ID = re.compile(r"[0-9a-f]{32}|user-[0-9a-f]{12}")


def _opaque(button_id: str) -> bool:
    """An ID nobody can read: a hex ID, a user key, a saved camera preset."""
    return _HEX_ID.fullmatch(button_id) is not None or _saved_preset_key(button_id) is not None


_PREFIXED = ("desktop:", "file:", "via:", "key:", "@")


def _confusable(name: str, ids) -> bool:
    """A name that import would take for something else: it looks like an ID or has the
    prefix of a reference kind (desktop:, file:, via:, key:, @), or it is an existing ID.
    Such a name does not go into the file as the key: the row keeps the ID."""
    text = name.strip()
    return (_opaque(text.lower()) or text.lower().startswith(_PREFIXED)
            or text in ids or text.lower() in ids)


def _once(warnings: List[dict], warning: dict) -> None:
    if not any(w["kind"] == warning["kind"] and label_key(w["name"]) == label_key(warning["name"]) for w in warnings):
        warnings.append(warning)


def _not_a_key(kind: str, name: str) -> dict:
    return {"kind": kind, "name": name,
            "reason": f"{kind} „{name}“ sieht aus wie eine ID oder ein Präfix: die Datei enthält die ID"}


async def annotate(row: dict, names: Names, warnings: List[dict]) -> dict:
    """The row as it goes into the file: opaque references by name, readable IDs
    as they are with a title next to them. warnings gets what cannot be a name."""
    result = {}

    def unique(kind: str, name: str, labels) -> bool:
        count = sum(label_key(label) == label_key(name) for label in labels)
        if count > 1 and not any(w["kind"] == kind and label_key(w["name"]) == label_key(name) for w in warnings):
            warnings.append({"kind": kind, "name": name, "reason":
                             f"{kind} „{name}“ ist nicht eindeutig ({count}×): die Datei enthält die ID"})
        return count == 1

    def key_by_name(key_id: str, name: str) -> bool:
        """A key that goes into the file by its name: one somebody named, its
        name unique and not to be taken for an ID or a prefix (else: a warning)."""
        if not _opaque(key_id):
            return False
        if _confusable(name, names.ids):
            _once(warnings, _not_a_key("Taste", name))
            return False
        return unique("Taste", name, names.named_buttons.values())

    def key_step(step):
        key = step.removeprefix(KEY_PREFIX) if isinstance(step, str) and step.startswith(KEY_PREFIX) else None
        name = names.buttons.get(key) if key else None
        return KEY_PREFIX + name if name and key_by_name(key, name) else step

    for field, value in row.items():
        result[field] = value
        if field == "light" and value in names.scenes:
            name = names.scenes[value]
            if _confusable(name, names.ids):
                result["light_name"] = name
                _once(warnings, _not_a_key("Licht", name))
            elif unique("Licht", name, names.scenes.values()):
                result["light"] = name
            else:
                result["light_name"] = name
        elif field in _KEY_FIELDS and isinstance(value, str) and not is_file_action(value):
            name = names.key_name(value)
            if name and key_by_name(value, name):
                result[field] = name
            elif name:
                result[f"{field}_name"] = name
        elif field == "desktop" and value:
            # A named key's step by its name ("key:Kollekte Musik"); Desktop keys,
            # files and the other steps as they are. The names next to them are for reading.
            result["desktop"] = [key_step(step) for step in value]
            result["desktop_names"] = [names.step_name(step) for step in value]
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

def _lookup(value: str, hint: Optional[str], finder) -> Tuple[Optional[str], Optional[str], str]:
    """(id, problem, name for a report). The value is tried as a name first (a hand-edited
    file: the name is in the ID field, a stale *_name next to it must not win), then
    the *_name of an older file. The first name that fits something decides: one item is
    found, several are ambiguous; finder(name) returns the matching IDs."""
    for name in (value, hint):
        if name:
            matches = finder(name)
            if matches:
                found, problem = _only(matches)
                return found, problem, name
    return None, "nicht gefunden", hint or value


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
        found, problem, name = _lookup(value, _text(row.get("light_name")),
                                       lambda n: [i for i, t in names.scenes.items() if _same(t, n)])
        row["light"] = found
        if problem:
            drop("light", "Licht", name, problem)
    for field in _KEY_FIELDS:
        value = _text(row.get(field))
        if not value or is_file_action(value):
            continue
        name = _text(row.get(f"{field}_name"))
        if is_via(value):  # a file through a Desktop key: the key by its name, the path stays
            try:
                profile_id, path = parse_via(value)
            except ValueError:
                continue  # for the validation of the row
            if profile_id in names.profiles:
                continue
            name = name or profile_id
            found, problem = _only([i for i, t in names.profiles.items() if _same(t, name)])
            if found:
                row[field] = via_value(found, path)
            else:
                # Without its Desktop key the file has no output here: dropped, and said so.
                row[field] = None
                drop(field, "Desktop-Taste", name, problem)
            continue
        if value.startswith(DESKTOP_PREFIX):  # a desktop profile stays one: only profiles are searched
            if value.removeprefix(DESKTOP_PREFIX) in names.profiles:
                continue
            found, problem, name = _lookup(value.removeprefix(DESKTOP_PREFIX), name,
                                           lambda n: [f"{DESKTOP_PREFIX}{i}" for i, t in names.profiles.items() if _same(t, n)])
            kind = "Desktop-Profil"
        else:  # a key stays one: only keys are searched
            if await pressable_button(names.request, value) is not None:
                continue
            found, problem, name = _lookup(value, name, names.find_key)
            kind = "Taste"
        row[field] = found
        if problem:
            drop(field, kind, name, problem)
    steps = row.get("desktop")
    if isinstance(steps, list):
        titles = row.get("desktop_names") if isinstance(row.get("desktop_names"), list) else []
        kept = []
        for index, step in enumerate(steps):
            title = _text(titles[index]) if index < len(titles) else None
            if not isinstance(step, str) or step in PSEUDO_STEPS or is_file_action(step):
                kept.append(step)  # (a wrong type is for the validation)
                continue
            if step.startswith(KEY_PREFIX):  # a key stays one: only keys are searched, named ones first
                key = step.removeprefix(KEY_PREFIX)
                if await pressable_button(names.request, key) is not None:
                    kept.append(step)
                    continue
                found, problem, name = _lookup(key, title, names.find_key)
                if found:
                    kept.append(KEY_PREFIX + found)
                else:
                    drop("desktop", "Taste", name, problem)
                continue
            path = None
            if is_via(step):  # a file through a Desktop key: the key by its name, the path stays
                try:
                    profile_id, path = parse_via(step)
                except ValueError:
                    kept.append(step)  # for the validation
                    continue
            else:
                profile_id = step
            if profile_id in names.profiles:
                kept.append(step)
                continue
            found, problem, name = _lookup(profile_id, title,
                                           lambda n: [i for i, t in names.profiles.items() if _same(t, n)])
            if found:
                kept.append(via_value(found, path) if path is not None else found)
            else:
                drop("desktop", "Desktop-Taste", name, problem)
        row["desktop"] = kept
    value = _text(row.get("song"))
    if value and names.songs and value not in names.songs:  # the library may not be loaded: then it stays
        book = _text(row.get("song_book"))

        def songs_titled(title: str) -> List[str]:
            matches = [i for i, song in names.songs.items()
                       if any(_same(t, title) for t in song.get("titles") or [song["title"]])]
            if len(matches) > 1 and book:  # the same title in several books: the book tells which
                matches = [i for i in matches if _same(names.song_book(names.songs[i]), book)] or matches
            return matches

        found, problem, title = _lookup(value, _text(row.get("song_title")), songs_titled)
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
