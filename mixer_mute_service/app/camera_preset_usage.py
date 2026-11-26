"""Who uses a camera's VISCA preset number: program rows, shortcuts,
template rows (their camera presets or a preset key as an action) and keys
in Steuerung (Overall macros, keys running a preset). The camera section asks
before it stores a new position at a number something already relies on."""

from __future__ import annotations

from typing import Dict, Iterable, List

from .actions import native_action_of
from .companion_buttons import CAMERA_PRESET_MAX, CAMERA_PRESET_MIN, CATALOG_PRESET_IDS, saved_preset_id
from .program_items import _ACTION_FIELDS


def _preset_key_ids(camera: str) -> Dict[str, int]:
    """Every key ID that recalls one of this camera's numbers -> the number."""
    ids = {saved_preset_id(camera, n): n for n in range(CAMERA_PRESET_MIN, CAMERA_PRESET_MAX + 1)}
    ids.update({button_id: n for (cam, n), button_id in CATALOG_PRESET_IDS.items() if cam == camera})
    return ids


def _row_numbers(row: dict, camera: str, key_ids: Dict[str, int]) -> set:
    numbers = set()
    preset = row.get(f"{camera}_preset")
    if type(preset) is int:
        numbers.add(preset)
    for field in _ACTION_FIELDS:
        value = row.get(field)
        if isinstance(value, str) and value in key_ids:
            numbers.add(key_ids[value])
    return numbers


def _row_name(row: dict) -> str:
    title = str(row.get("title") or "").strip() or "ohne Titel"
    time = str(row.get("time") or "").strip()
    return f"{time} {title}" if time else title


def _add(used: Dict[int, List[str]], numbers: Iterable[int], label: str) -> None:
    for number in numbers:
        entries = used.setdefault(number, [])
        if label not in entries:
            entries.append(label)


async def camera_preset_usage(state, camera: str) -> Dict[int, List[str]]:
    """Number -> readable list of what uses it ("Programm: 10:00 Predigt",
    "Shortcut: Abendmahl", "Vorlage „Sonntag“: Predigt", "Taste: Overall
    Predigt"). Numbers nothing uses are left out."""
    key_ids = _preset_key_ids(camera)
    used: Dict[int, List[str]] = {}
    program = getattr(state, "program_items", None)
    if program is not None:
        for row in await program.list():
            _add(used, _row_numbers(row, camera, key_ids), f"Programm: {_row_name(row)}")
    shortcuts = getattr(state, "shortcuts", None)
    if shortcuts is not None:
        for shortcut in await shortcuts.list():
            _add(used, _row_numbers(shortcut, camera, key_ids), f"Shortcut: {shortcut.get('title', '')}")
    templates = getattr(state, "program_templates", None)
    if templates is not None:
        for template in await templates.list():
            for row in template.get("rows", []):
                _add(used, _row_numbers(row, camera, key_ids),
                     f"Vorlage „{template.get('name', '')}“: {_row_name(row)}")
    store = getattr(state, "companion_button_store", None)
    if store is not None:
        for button in (await store.snapshot())["buttons"]:
            action = native_action_of(button) or {}
            numbers = set()
            if action.get("type") == "group-macro":
                preset = (action.get("cameras") or {}).get(camera)
                if type(preset) is int:
                    numbers.add(preset)
            elif action.get("type") == "visca-preset" and action.get("camera") == camera:
                numbers.add(action.get("preset"))
            _add(used, numbers, f"Taste: {button.get('label', button.get('id'))}")
    return {n: labels for n, labels in sorted(used.items()) if CAMERA_PRESET_MIN <= n <= CAMERA_PRESET_MAX}
