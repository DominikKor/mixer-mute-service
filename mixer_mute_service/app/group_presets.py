"""Saved fader values and descriptions per mute group and channel.

They used to live only in each browser's localStorage. Safari deletes the
storage of a site that has not been used for 7 days, and the service runs
once a week, so the iPad kept losing them. Now the service keeps them in
group_presets.json and every device sees the same values.

Store only: a saved fader value is sent to the mixer only by the normal
recall path (POST /channels/{n}/recall-fader), with all its safety checks.

  {"<group>": {"<channel>": {"fader_hex": "00c06b44" | null,
                             "description": "...", "set_at": <ms epoch>}}}
"""

from __future__ import annotations

import asyncio
import time
from copy import deepcopy
from pathlib import Path
from typing import Dict, Iterable, Optional

from .jsonstore import read_json, write_json_atomic
from .protocol import fader_hex_to_value

MAX_DESCRIPTION = 200


class PresetError(ValueError):
    pass


def _clean_entry(entry: object) -> Optional[dict]:
    if not isinstance(entry, dict):
        return None
    fader_hex = entry.get("fader_hex")
    if fader_hex is not None and fader_hex_to_value(fader_hex) is None:
        fader_hex = None
    description = entry.get("description", "")
    if not isinstance(description, str):
        description = ""
    set_at = entry.get("set_at")
    return {
        "fader_hex": fader_hex,
        "description": description[:MAX_DESCRIPTION],
        "set_at": set_at if isinstance(set_at, (int, float)) and not isinstance(set_at, bool) else None,
    }


class GroupPresetStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        data = read_json(path, dict, default={})
        self._presets: Dict[str, Dict[str, dict]] = {}
        for group, channels in data.items():
            if not isinstance(group, str) or not isinstance(channels, dict):
                continue
            cleaned = {str(ch): e for ch, e in ((ch, _clean_entry(v)) for ch, v in channels.items()) if e}
            if cleaned:
                self._presets[group] = cleaned

    def _commit(self, presets: Dict[str, Dict[str, dict]]) -> None:
        """Write first, then switch: a failed write leaves memory unchanged."""
        write_json_atomic(self._path, presets, ensure_ascii=False)
        self._presets = presets

    async def view(self, groups: Dict[str, Iterable[int]]) -> Dict[str, Dict[str, dict]]:
        """Presets of the existing groups, only for channels still in them."""
        async with self._lock:
            presets = deepcopy(self._presets)
        out = {}
        for group, channels in groups.items():
            members = {str(ch) for ch in channels}
            entries = {ch: e for ch, e in presets.get(group, {}).items() if ch in members}
            if entries:
                out[group] = entries
        return out

    async def set(self, group: str, channel: int, fader_hex=..., description=...) -> dict:
        """Change one entry; fields left out keep their value."""
        if fader_hex is not ... and fader_hex is not None and fader_hex_to_value(fader_hex) is None:
            raise PresetError("invalid fader_hex")
        if description is not ...:
            if not isinstance(description, str) or len(description) > MAX_DESCRIPTION:
                raise PresetError(f"description must be text of at most {MAX_DESCRIPTION} characters")
            description = description.strip()
        async with self._lock:
            presets = deepcopy(self._presets)
            entry = presets.setdefault(group, {}).setdefault(
                str(channel), {"fader_hex": None, "description": "", "set_at": None})
            if fader_hex is not ...:
                entry["fader_hex"] = fader_hex
                entry["set_at"] = int(time.time() * 1000)
            if description is not ...:
                entry["description"] = description
            self._commit(presets)
            return dict(entry)

    async def set_faders(self, group: str, fader_hex: Dict[str, str]) -> None:
        """Save several fader values at once (a group made from the console)."""
        now = int(time.time() * 1000)
        async with self._lock:
            presets = deepcopy(self._presets)
            entries = presets.setdefault(group, {})
            for channel, value in fader_hex.items():
                if fader_hex_to_value(value) is None:
                    continue
                entry = entries.setdefault(str(channel), {"fader_hex": None, "description": "", "set_at": None})
                entry.update({"fader_hex": value, "set_at": now})
            self._commit(presets)

    async def rename_group(self, old: str, new: str) -> bool:
        async with self._lock:
            if old not in self._presets:
                return False
            self._commit({(new if k == old else k): v for k, v in self._presets.items()})
            return True

    async def delete_group(self, group: str) -> bool:
        async with self._lock:
            if group not in self._presets:
                return False
            self._commit({k: v for k, v in self._presets.items() if k != group})
            return True
