"""Bus registry: buses are discovered from the mixer's state dump.

Every full state dump lists all bus objects (Hall 1 = 128, Monitor = 131, ...)
with their console name. A bus that is not known yet gets the next free API/UI
channel number after the highest one in use. That assignment is persisted to
``buses.json`` next to ``groups.json``, because groups refer to channels by
number: a bus must never silently move to another number.

Buses listed in ``config.yaml`` (``mixer.buses``) always win: they keep their
channel number and name, so an existing installation keeps its numbering.

Object ids in AUTO_BUS_IDS are taken over automatically, the main L/R output
(id 255, "Saal L/R MIX OUTPUT") included: it has the same frame format and
becomes a channel with its own fader and mute. Because a mistake on the master
silences the whole room, groups and the solo-on-unmute never touch it (see
is_main_output and the controller); only its own channel controls do.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, List, Tuple

from .config import BusConfig, Config
from .jsonstore import read_json, write_json_atomic
from .protocol import BusMap, varint_hex

log = logging.getLogger("mixer.buses")

MAIN_LR_ID = 255  # the console's main L/R output
AUTO_BUS_IDS = range(128, MAIN_LR_ID + 1)  # the console's buses plus main L/R


def is_main_output(bus: BusConfig) -> bool:
    return bus.id.lower() == varint_hex(MAIN_LR_ID)


def main_output_channels(cfg: Config) -> set:
    """Channel numbers of the main L/R output (normally none or one)."""
    return {bus.channel for bus in cfg.mixer.buses if is_main_output(bus)}


class BusRegistry:
    def __init__(self, cfg: Config, path: Path) -> None:
        self._cfg = cfg
        self._path = path
        self._listeners: List[Callable[[int], None]] = []
        self._merge_persisted()
        # Shared with the link's parsers; discover() adds to it at runtime.
        self.bus_map = BusMap([(b.channel, b.id) for b in cfg.mixer.buses])

    def on_new_bus(self, callback: Callable[[int], None]) -> None:
        """Call ``callback(channel)`` whenever a bus is added at runtime."""
        self._listeners.append(callback)

    # -- persistence -------------------------------------------------------

    def _load(self) -> List[BusConfig]:
        # An unreadable file is kept as buses.json.corrupt (see jsonstore).
        buses = []
        for entry in read_json(self._path, list, default=[]):
            try:
                buses.append(BusConfig(**entry))
            except (TypeError, ValueError) as exc:
                log.warning("ignoring invalid bus entry %r in %s: %s", entry, self._path, exc)
        return buses

    def _persist(self) -> None:
        explicit = {b.channel for b in self._explicit}
        auto = [b.model_dump() for b in self._cfg.mixer.buses if b.channel not in explicit]
        try:
            write_json_atomic(self._path, auto)
        except OSError as exc:
            # Runs inside the stream parser: a failed write must not take the
            # mixer link down. The bus works now; its number is just not saved.
            log.error("could not save discovered buses to %s: %s", self._path, exc)

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

    def discover(self, objects: List[Tuple[int, str]]) -> None:
        """Take over buses listed in a state dump (called by the link's parser
        for every dump, before its sections are read)."""
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
            for bus in added:
                for callback in self._listeners:
                    try:
                        callback(bus.channel)
                    except Exception:  # noqa: BLE001
                        log.exception("bus listener failed for channel %s", bus.channel)
            self._persist()
