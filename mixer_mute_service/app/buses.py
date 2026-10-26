"""Bus registry: buses are discovered from the mixer's state dump.

Every full state dump lists all bus objects (Hall 1 = 128, Monitor = 131, ...)
with their console name. A bus that is not known yet gets the next free API/UI
channel number after the highest one in use. That assignment is persisted to
``buses.json`` next to ``groups.json``, because groups refer to channels by
number: a bus must never silently move to another number.

Buses listed in ``config.yaml`` (``mixer.buses``) always win: they keep their
channel number and name, so an existing installation keeps its numbering.

Only object ids in AUTO_BUS_IDS are taken over automatically. The main L/R
output (id 255) has the same frame format but was never verified live, and the
master is the one fader where a mistake hurts most -- it stays out unless it is
configured explicitly.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable, List, Tuple

from .config import BusConfig, Config
from .protocol import BusMap, varint_hex

log = logging.getLogger("mixer.buses")

AUTO_BUS_IDS = range(128, 255)  # the console's buses; 255 = main L/R excluded


class BusRegistry:
    def __init__(self, cfg: Config, path: Path) -> None:
        self._cfg = cfg
        self._path = path
        self._listeners: List[Callable[[int], None]] = []
        self._merge_persisted()
        self.bus_map = BusMap(
            [(b.channel, b.id) for b in cfg.mixer.buses], on_dump=self._on_dump
        )

    def on_new_bus(self, callback: Callable[[int], None]) -> None:
        """Call ``callback(channel)`` whenever a bus is added at runtime."""
        self._listeners.append(callback)

    # -- persistence -------------------------------------------------------

    def _load(self) -> List[BusConfig]:
        if not self._path.exists():
            return []
        try:
            data = json.loads(self._path.read_text() or "[]")
            return [BusConfig(**b) for b in data]
        except Exception as exc:  # noqa: BLE001
            log.warning("ignoring unreadable %s: %s", self._path, exc)
            return []

    def _persist(self) -> None:
        explicit = {b.channel for b in self._explicit}
        auto = [b.model_dump() for b in self._cfg.mixer.buses if b.channel not in explicit]
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(auto, indent=2))

    def _merge_persisted(self) -> None:
        """Add previously discovered buses that do not clash with config."""
        self._explicit = list(self._cfg.mixer.buses)
        used_channels = set(self._cfg.valid_channels())
        used_ids = {b.id for b in self._explicit}
        for b in self._load():
            if b.channel in used_channels or b.id in used_ids:
                log.warning("persisted bus %s (id %s) clashes with config; dropped", b.channel, b.id)
                continue
            self._cfg.mixer.buses.append(b)
            used_channels.add(b.channel)
            used_ids.add(b.id)

    # -- discovery ---------------------------------------------------------

    def _on_dump(self, objects: List[Tuple[int, str]]) -> None:
        added = []
        for object_id, name in sorted(objects):
            if object_id in self.bus_map.ids or object_id not in AUTO_BUS_IDS:
                continue
            channel = max(self._cfg.valid_channels()) + 1
            bus = BusConfig(channel=channel, name=name or f"Bus {object_id}", id=varint_hex(object_id))
            self._cfg.mixer.buses.append(bus)
            self.bus_map.add(channel, object_id)
            added.append(bus)
            log.info("discovered bus id=%s name=%r -> channel %s", object_id, bus.name, channel)
        if added:
            self._persist()
            for bus in added:
                for callback in self._listeners:
                    callback(bus.channel)
