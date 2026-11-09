"""Names the user gave to fixed things: stage zones and room light circuits
(Licht) and the devices of "Alles ausschalten" (PCs by address, the beamer).

Stored on the service (display_names.json) so every device shows them. An
empty name goes back to the built-in one. The routes are in app/api/names.py.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from pathlib import Path
from typing import Dict

from .jsonstore import read_json, write_json_atomic
from .native_controls import ARTNET_ZONES, SHELLY_HOSTS

log = logging.getLogger("mixer.names")

MAX_NAME = 40


def _power_target(item_id: str) -> bool:
    if item_id == "beamer":
        return True
    try:
        ipaddress.IPv4Address(item_id)
        return True
    except ValueError:
        return False


KINDS = {
    "light-zone": lambda item_id: item_id in ARTNET_ZONES,
    "light-circuit": lambda item_id: item_id in SHELLY_HOSTS,
    "power-target": _power_target,
}


def clean_name(name) -> str:
    """The name as stored ("" = built-in name); ValueError if unusable."""
    if not isinstance(name, str):
        raise ValueError("name must be text")
    name = " ".join(name.split())
    if len(name) > MAX_NAME:
        raise ValueError(f"name must be at most {MAX_NAME} characters")
    return name


class NameStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        data = read_json(path, dict, default={})
        self._names: Dict[str, Dict[str, str]] = {}
        for kind, valid in KINDS.items():
            entries = data.get(kind, {})
            if not isinstance(entries, dict):
                continue
            self._names[kind] = {
                k: v for k, v in entries.items()
                if isinstance(k, str) and valid(k) and isinstance(v, str) and 0 < len(v) <= MAX_NAME
            }

    def get(self, kind: str) -> Dict[str, str]:
        return dict(self._names.get(kind, {}))

    async def set(self, kind: str, item_id: str, name) -> str:
        """Store a name; "" removes it. KeyError for an unknown kind or id."""
        valid = KINDS.get(kind)
        if valid is None or not valid(item_id):
            raise KeyError(f"{kind}/{item_id}")
        name = clean_name(name)
        async with self._lock:
            names = self._names.setdefault(kind, {})
            if name:
                names[item_id] = name
            else:
                names.pop(item_id, None)
            write_json_atomic(self._path, self._names, ensure_ascii=False)
            return name
