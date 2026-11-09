"""Channel processing settings (gain, HPF, EQ, gate, compressor): read from
the mixer's state dump and saved as a snapshot per mute group.

Store only: nothing in here is ever sent to the mixer. The raw sections are
kept as they came from the console, so a later recall could send them back
unchanged; the decoded view is only for reading.

Field map, measured live on 2026-11-09 by changing each control on channel 8
and watching the dump (input channel object, fields 3/4/5):

  input (3):    20 gain dB, 7 HPF on, 8 HPF frequency Hz
                (5 on/off + 6 value: changed together, meaning not confirmed)
  eq (4):       2 on; bands as (frequency Hz, gain dB, Q) in fields
                3/4/5, 6/7/8, 9/10/11, 12/13/14
  dynamics (5): gate: 2 on, 3 threshold dB, 4 attack ms, 5 hold ms,
                6 release ms, 7 range dB
                compressor: 8 on, 9 threshold dB, 13 ratio, 10 attack ms,
                11 release ms, 12 make-up gain dB

Omitted fields are protobuf defaults (0 / off).
"""

from __future__ import annotations

import asyncio
import struct
import time
from copy import deepcopy
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from .jsonstore import read_json, write_json_atomic
from .protocol import _decode_fields

SECTIONS = ("input", "eq", "dynamics")
# A snapshot only uses values from a dump at most this old (direct mode
# refreshes the dump every 10 s).
MAX_AGE_SECONDS = 30.0

_KNOWN = {
    "input": {20, 7, 8},
    "eq": {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14},
    "dynamics": {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13},
}


class SettingsError(ValueError):
    def __init__(self, reason: str, details: Optional[list] = None) -> None:
        super().__init__(reason)
        self.details = details or []


def _fields(raw: Optional[bytes]) -> Dict[int, object]:
    """{field: value} of one section (first occurrence; floats decoded)."""
    out: Dict[int, object] = {}
    for field, wire, value in _decode_fields(raw or b"") or []:
        if field in out:
            continue
        if wire == 5:
            out[field] = struct.unpack("<f", value)[0]
        elif wire == 0:
            out[field] = value
    return out


def _num(fields: Dict[int, object], field: int, digits: int = 1) -> float:
    value = fields.get(field, 0)
    return round(float(value), digits) if isinstance(value, (int, float)) else 0.0


def decode(sections: Dict[str, bytes]) -> dict:
    """Readable view of one channel's raw sections."""
    inp = _fields(sections.get("input"))
    eq = _fields(sections.get("eq"))
    dyn = _fields(sections.get("dynamics"))
    bands = [
        {"freq_hz": _num(eq, f, 0), "gain_db": _num(eq, f + 1), "q": _num(eq, f + 2, 2)}
        for f in (3, 6, 9, 12)
    ]
    other = {}
    for name, fields in (("input", inp), ("eq", eq), ("dynamics", dyn)):
        for field, value in fields.items():
            if field != 1 and field not in _KNOWN[name]:  # 1 = channel index
                other[f"{name}.{field}"] = round(value, 3) if isinstance(value, float) else value
    return {
        "gain_db": _num(inp, 20),
        "hpf": {"on": bool(inp.get(7)), "freq_hz": _num(inp, 8, 0)},
        "eq": {"on": bool(eq.get(2)), "bands": sorted(bands, key=lambda b: b["freq_hz"])},
        "gate": {
            "on": bool(dyn.get(2)), "threshold_db": _num(dyn, 3), "attack_ms": _num(dyn, 4),
            "hold_ms": _num(dyn, 5), "release_ms": _num(dyn, 6), "range_db": _num(dyn, 7),
        },
        "compressor": {
            "on": bool(dyn.get(8)), "threshold_db": _num(dyn, 9), "ratio": _num(dyn, 13, 2),
            "attack_ms": _num(dyn, 10), "release_ms": _num(dyn, 11), "makeup_db": _num(dyn, 12),
        },
        "other": other,
    }


class ChannelSettingsCache:
    """The latest raw sections per input channel, from the last state dump."""

    def __init__(self) -> None:
        self._latest: Dict[int, Tuple[float, Dict[str, bytes]]] = {}

    def update(self, channels: Dict[int, Dict[str, bytes]]) -> None:
        now = time.monotonic()
        for channel, sections in channels.items():
            self._latest[channel] = (now, dict(sections))

    def get(self, channel: int) -> Optional[Tuple[float, Dict[str, bytes]]]:
        return self._latest.get(channel)


class ChannelSettingsStore:
    """Saved settings snapshots, one per mute group (channel_settings.json)."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        data = read_json(path, dict, default={})
        self._snapshots: Dict[str, dict] = {
            name: snap for name, snap in data.items()
            if isinstance(snap, dict) and isinstance(snap.get("channels"), dict)
        }

    def _persist(self) -> None:
        write_json_atomic(self._path, self._snapshots, ensure_ascii=False)

    async def capture(
        self,
        group: str,
        channels: Iterable[int],
        cache: ChannelSettingsCache,
        session_epoch: Optional[float],
        max_input: int,
    ) -> dict:
        """Save the current settings of a group's input channels.

        All-or-nothing: if any input channel has no current value from the
        mixer, nothing is saved. Buses are skipped (listed in "skipped").
        """
        now = time.monotonic()
        saved: Dict[str, dict] = {}
        skipped: List[int] = []
        missing = []
        for channel in channels:
            if channel > max_input:
                skipped.append(channel)
                continue
            entry = cache.get(channel)
            if entry is None:
                missing.append({"channel": channel, "reason": "no settings from the mixer yet"})
                continue
            seen_at, sections = entry
            if session_epoch is None or seen_at < session_epoch:
                missing.append({"channel": channel, "reason": "settings predate the current mixer link"})
            elif now - seen_at > MAX_AGE_SECONDS:
                missing.append({"channel": channel, "reason": "settings not current",
                                "age_seconds": round(now - seen_at, 1)})
            else:
                saved[str(channel)] = {name: sections[name].hex() for name in SECTIONS if name in sections}
        if missing:
            raise SettingsError("cannot capture settings", missing)
        if not saved:
            raise SettingsError("group has no input channels")
        snapshot = {
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "channels": saved,
            "skipped": skipped,
        }
        async with self._lock:
            self._snapshots[group] = snapshot
            self._persist()
        return self.view(group, snapshot)

    async def get(self, group: str) -> Optional[dict]:
        async with self._lock:
            snapshot = deepcopy(self._snapshots.get(group))
        return None if snapshot is None else self.view(group, snapshot)

    @staticmethod
    def view(group: str, snapshot: dict) -> dict:
        channels = {}
        for channel, raw in snapshot["channels"].items():
            try:
                sections = {name: bytes.fromhex(value) for name, value in raw.items()}
            except (TypeError, ValueError):
                continue
            channels[channel] = decode(sections)
        return {
            "group": group,
            "captured_at": snapshot.get("captured_at"),
            "channels": channels,
            "skipped": snapshot.get("skipped", []),
        }

    async def rename_group(self, old: str, new: str) -> bool:
        async with self._lock:
            if old not in self._snapshots:
                return False
            self._snapshots[new] = self._snapshots.pop(old)
            self._persist()
            return True

    async def delete_group(self, group: str) -> bool:
        async with self._lock:
            if self._snapshots.pop(group, None) is None:
                return False
            self._persist()
            return True
