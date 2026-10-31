"""Persistent category and button data for Companion controls."""

from __future__ import annotations

import asyncio
import json
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Dict, List

COMPANION_BUTTONS = [
    {"id": "beamer-freeze", "label": "Beamer freeze", "kind": "native", "category_id": "beamer", "native_action": {"type": "beamer-script", "script": "beamer-freeze"}},
    {"id": "cam2-live-top", "label": "Cam 2 Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "Cam 2", "source": "Cam2"}},
    {"id": "pc2-live-top-left", "label": "PC2 Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "PC2", "source": "Capture_PC2"}},
    {"id": "cam1-toggle", "label": "Cam1 ein/aus", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-source-toggle", "scene": "Preview Scene", "source": "Cam1_klein", "hide_sources": ["Capture_PC2", "Beamer", "Cam2_klein"]}},
    {"id": "pc2-live-top-right", "label": "PC2 Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "PC2", "source": "Capture_PC2"}},
    {"id": "start", "label": "Start", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-scene", "scene": "Willkommen (neu)"}},
    {"id": "ende", "label": "Ende", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-scene", "scene": "Ende (neu)"}},
    {"id": "beamer-hide", "label": "Beamer hide", "kind": "native", "category_id": "beamer", "native_action": {"type": "beamer-script", "script": "beamer-hide"}},
    {"id": "cam1-live", "label": "Cam 1 Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "Cam 1", "source": "Cam1"}},
    {"id": "cam2-live-bottom", "label": "Cam 2 Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "Cam 2", "source": "Cam2"}},
    {"id": "beamer-live", "label": "Beamer Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "BeamerVonVorne", "source": "Beamer"}},
    {"id": "pultmikro-mute", "label": "Pultmikro Mute", "kind": "channel", "category_id": "mixer", "channel": 1, "operation": "mute"},
    {"id": "gebetsmikro-unmute", "label": "Gebetsmikro Unmute", "kind": "channel", "category_id": "mixer", "channel": 2, "operation": "unmute"},
]

_SHELLY_BUTTONS = [
    ("left", "Licht Links"), ("center-left", "Licht Mitte Links"),
    ("center-right", "Licht Mitte Rechts"), ("right", "Licht Rechts"),
    ("front", "Licht Vorne"), ("tech", "Licht Technik"), ("stage", "Licht Bühne"),
]
NATIVE_BUTTONS = [
    {"id": f"native-shelly-{key}", "label": label, "kind": "native", "category_id": "light",
     "native_action": {"type": "shelly-toggle", "target": f"light-{key}"}}
    for key, label in _SHELLY_BUTTONS
] + [
    {"id": "native-shelly-all-on", "label": "Licht AN", "kind": "native", "category_id": "light",
     "native_action": {"type": "shelly-set-all", "on": True}},
    {"id": "native-shelly-all-off", "label": "Licht AUS", "kind": "native", "category_id": "light",
     "native_action": {"type": "shelly-set-all", "on": False}},
    {"id": "native-shelly-center-on", "label": "Licht Mitte AN", "kind": "native", "category_id": "light",
     "native_action": {"type": "shelly-set-group", "targets": ["light-tech", "light-center-right", "light-left", "light-stage", "light-right", "light-center-left", "light-front"], "on": True}},
]
for _zone, _label in (("left", "Links"), ("center-left", "Mitte Links"), ("center-right", "Mitte Rechts"), ("right", "Rechts")):
    NATIVE_BUTTONS.extend([
        {"id": f"native-artnet-{_zone}-up", "label": f"Bühne { _label } Heller", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-zone-adjust", "zone": _zone, "delta": 50}},
        {"id": f"native-artnet-{_zone}-down", "label": f"Bühne { _label } Dunkler", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-zone-adjust", "zone": _zone, "delta": -50}},
    ])
NATIVE_BUTTONS.extend([
    {"id": "native-artnet-all-up", "label": "Bühne Alle Heller", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-all-adjust", "delta": 50}},
    {"id": "native-artnet-all-down", "label": "Bühne Alle Dunkler", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-all-adjust", "delta": -50}},
    {"id": "native-artnet-start-up", "label": "Starthelligkeit Heller", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-start-adjust", "delta": 50}},
    {"id": "native-artnet-start-down", "label": "Starthelligkeit Dunkler", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-start-adjust", "delta": -50}},
    {"id": "native-artnet-steps-up", "label": "Dimm-Schrittweite Größer", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-setting-adjust", "setting": "steps", "delta": 5}},
    {"id": "native-artnet-steps-down", "label": "Dimm-Schrittweite Kleiner", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-setting-adjust", "setting": "steps", "delta": -5}},
    {"id": "native-artnet-fade-faster", "label": "Licht-Übergang Schneller", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-setting-adjust", "setting": "fade_ms", "delta": -250}},
    {"id": "native-artnet-fade-slower", "label": "Licht-Übergang Langsamer", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-setting-adjust", "setting": "fade_ms", "delta": 250}},
    {"id": "native-artnet-warm-up", "label": "Bühne Wärmer", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-global-adjust", "color": "warm", "delta": 50}},
    {"id": "native-artnet-warm-down", "label": "Bühne Weniger warm", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-global-adjust", "color": "warm", "delta": -50}},
    {"id": "native-artnet-cold-up", "label": "Bühne Kälter", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-global-adjust", "color": "cold", "delta": 50}},
    {"id": "native-artnet-cold-down", "label": "Bühne Weniger kalt", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-global-adjust", "color": "cold", "delta": -50}},
])

for _camera in ("cam1", "cam2"):
    for _preset, _label in ((1, "Pult nah"), (2, "Pult weit"), (3, "Klavier"), (4, "Tasten"), (5, "Raum")):
        NATIVE_BUTTONS.append({
            "id": f"native-{_camera}-preset-{_preset}", "label": f"{_camera.upper()} {_label}",
            "kind": "native", "category_id": "cameras",
            "native_action": {"type": "visca-preset", "camera": _camera, "preset": _preset},
        })
    NATIVE_BUTTONS.extend([
        {"id": f"native-{_camera}-zoom-in", "label": f"{_camera.upper()} Zoom IN", "kind": "native", "category_id": "cameras", "native_action": {"type": "visca-zoom", "camera": _camera, "direction": "in"}},
        {"id": f"native-{_camera}-zoom-out", "label": f"{_camera.upper()} Zoom OUT", "kind": "native", "category_id": "cameras", "native_action": {"type": "visca-zoom", "camera": _camera, "direction": "out"}},
    ])
NATIVE_BUTTONS.extend([
    {"id": "native-cam1-preset-logo", "label": "CAM1 PSET Logo", "kind": "native", "category_id": "cameras", "native_action": {"type": "visca-preset", "camera": "cam1", "preset": 7}},
    {"id": "native-cam1-preset-prayer", "label": "CAM1 PSET Gebet", "kind": "native", "category_id": "cameras", "native_action": {"type": "visca-preset", "camera": "cam1", "preset": 8}},
    {"id": "native-cam2-preset-prayer", "label": "CAM2 PSET Gebet", "kind": "native", "category_id": "cameras", "native_action": {"type": "visca-preset", "camera": "cam2", "preset": 8}},
    {"id": "native-artnet-zone-left-toggle", "label": "Bühne Links", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-zone-toggle", "zone": "left"}},
    {"id": "native-artnet-zone-center-left-toggle", "label": "Bühne Mitte Links", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-zone-toggle", "zone": "center-left"}},
    {"id": "native-artnet-zone-center-right-toggle", "label": "Bühne Mitte Rechts", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-zone-toggle", "zone": "center-right"}},
    {"id": "native-artnet-zone-right-toggle", "label": "Bühne Rechts", "kind": "native", "category_id": "light", "native_action": {"type": "artnet-zone-toggle", "zone": "right"}},
    {"id": "native-obs-cam1-live", "label": "Cam 1 Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "Cam 1", "source_scene": "Current Scene", "source": "Cam1"}},
    {"id": "native-obs-cam2-live", "label": "Cam 2 Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "Cam 2", "source_scene": "Current Scene", "source": "Cam2"}},
    {"id": "native-obs-pc2-live", "label": "PC2 Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "PC2", "source_scene": "Current Scene", "source": "Capture_PC2"}},
    {"id": "native-obs-cam1-toggle", "label": "Cam 1 ein/aus", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-source-toggle", "scene": "Preview Scene", "source": "Cam1_klein", "hide_sources": ["Capture_PC2", "Beamer", "Cam2_klein"]}},
    {"id": "native-obs-cam2-toggle", "label": "Cam 2 ein/aus", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-source-toggle", "scene": "Preview Scene", "source": "Cam2_klein", "hide_sources": ["Capture_PC2", "Beamer", "Cam1_klein"]}},
    {"id": "native-obs-pc2-toggle", "label": "PC2 ein/aus", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-source-toggle", "scene": "Preview Scene", "source": "Capture_PC2", "hide_sources": ["Cam1_klein", "Beamer", "Cam2_klein"]}},
    {"id": "native-obs-start", "label": "Start", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-scene", "scene": "Willkommen (neu)"}},
    {"id": "native-obs-end", "label": "Ende", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-scene", "scene": "Ende (neu)"}},
])

_OVERALL_GROUPS = [
    ("gemeinsam-singen", "Gemeinsam Singen", "GemeinsamSingen"),
    ("predigt", "Predigt", "Predigt"),
    ("musik-klavier", "Musik Klavier", "Klavier"),
    ("pult-klavier", "Musik Pult Klavier", "PultKlavier"),
    ("pult-playback", "Musik Pult Playback", "PultPlayback"),
    ("handmik-klavier", "Musik Handmik Klavier", "HandmikKlavier"),
    ("handmik-playback", "Musik Handmik Playback", "HandmikPlayback"),
    ("hintergrundmusik", "Hintergrundmusik", "Hintergrundmusik"),
    ("gebet", "Gebet", "Gebet"),
    ("qr", "QR", "Hintergrundmusik"),
    ("mission-video", "Mission Video", "Missionsvideo"),
    ("instrumental", "Instrumental", "Instrumental"),
]
_OVERALL_LINKS = {
    "gemeinsam-singen": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-cam1-preset-5", "native-obs-cam1-live", "native-cam2-preset-5", "native-obs-cam2-live"],
    "musik-klavier": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-cam1-preset-3", "native-obs-cam1-live", "native-cam2-preset-3", "native-obs-cam2-live"],
    "pult-klavier": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-cam1-preset-2", "native-obs-cam1-live", "native-cam2-preset-2", "native-obs-cam2-live"],
    "pult-playback": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-cam1-preset-2", "native-obs-cam1-live", "native-cam2-preset-2", "native-obs-cam2-live"],
    "handmik-klavier": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-cam1-preset-5", "native-obs-cam1-live", "native-cam2-preset-2", "native-obs-cam2-live"],
    "handmik-playback": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-cam1-preset-5", "native-obs-cam1-live", "native-cam2-preset-2", "native-obs-cam2-live"],
    "predigt": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-cam1-preset-1", "native-obs-cam1-live", "native-cam2-preset-1", "native-obs-cam2-live"],
    "hintergrundmusik": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-cam1-preset-5", "native-obs-cam1-live", "native-cam2-preset-5", "native-obs-cam2-live"],
    "gebet": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-cam1-preset-prayer", "native-obs-cam1-live", "native-cam2-preset-prayer", "native-obs-cam2-live"],
    "qr": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-cam1-preset-5", "native-obs-cam1-live", "native-cam2-preset-5", "native-obs-cam2-live"],
    "mission-video": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-obs-pc2-live"],
    "instrumental": ["native-artnet-zone-left-toggle", "native-artnet-zone-center-right-toggle", "native-artnet-zone-right-toggle", "native-artnet-zone-center-left-toggle", "native-cam1-preset-3", "native-obs-cam1-live", "native-cam2-preset-5", "native-obs-cam2-live"],
}
NATIVE_BUTTONS.extend({
    "id": f"native-overall-{button_id}", "label": label, "kind": "native", "category_id": "overall",
    "native_action": {"type": "group-macro", "group": group, "buttons": _OVERALL_LINKS.get(button_id, [])},
} for button_id, label, group in _OVERALL_GROUPS)

COMPANION_BUTTONS = COMPANION_BUTTONS + NATIVE_BUTTONS
COMPANION_BUTTONS_BY_ID = {button["id"]: button for button in COMPANION_BUTTONS}
_NATIVE_CATALOG_VERSION = 3
_NATIVE_CATALOG_V3_IDS = {
    "native-artnet-start-up", "native-artnet-start-down",
    "native-artnet-steps-up", "native-artnet-steps-down",
    "native-artnet-fade-faster", "native-artnet-fade-slower",
    "native-obs-cam1-toggle", "native-obs-cam2-toggle", "native-obs-pc2-toggle",
}

DEFAULT_CATEGORIES = [
    {"id": "beamer", "name": "Beamer", "visible": True},
    {"id": "light", "name": "Licht", "visible": True},
    {"id": "cameras", "name": "Kameras", "visible": True},
    {"id": "obs", "name": "OBS", "visible": True},
    {"id": "overall", "name": "Overall", "visible": True},
    {"id": "mixer", "name": "Mixer", "visible": True},
]


class CompanionButtonStore:
    def __init__(self, path: Path, seed: List[dict]) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._categories = deepcopy(DEFAULT_CATEGORIES)
        self._buttons = deepcopy(seed)
        self._catalog_version = _NATIVE_CATALOG_VERSION
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._persist()
            return
        migrated = False
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            categories = data["categories"]
            buttons = data["buttons"]
            if not isinstance(categories, list) or not isinstance(buttons, list):
                raise ValueError("invalid Companion button data")
            self._catalog_version = int(data.get("native_catalog_version", 0))
            if self._catalog_version < 2:
                category_ids = {item["id"] for item in categories}
                categories.extend(item for item in deepcopy(DEFAULT_CATEGORIES) if item["id"] not in category_ids)
                button_map = {item["id"]: item for item in buttons}
                for seed_button in self._buttons:
                    current = button_map.get(seed_button["id"])
                    if current is None and seed_button["id"].startswith("native-"):
                        buttons.append(deepcopy(seed_button))
                    elif current is not None and seed_button.get("kind") == "native":
                        current["kind"] = "native"
                        current["native_action"] = deepcopy(seed_button["native_action"])
                        current["category_id"] = seed_button["category_id"]
                self._catalog_version = 2
                migrated = True
            if self._catalog_version < _NATIVE_CATALOG_VERSION:
                categories_ids = {item["id"] for item in categories}
                categories.extend(item for item in deepcopy(DEFAULT_CATEGORIES) if item["id"] not in categories_ids)
                button_map = {item["id"]: item for item in buttons}
                for seed_button in self._buttons:
                    current = button_map.get(seed_button["id"])
                    if current is None and seed_button["id"] in _NATIVE_CATALOG_V3_IDS:
                        current = deepcopy(seed_button)
                        buttons.append(current)
                        button_map[seed_button["id"]] = current
                    elif current is not None and seed_button.get("kind") == "native":
                        current["kind"] = "native"
                        current["native_action"] = deepcopy(seed_button["native_action"])
                self._catalog_version = _NATIVE_CATALOG_VERSION
                migrated = True
            category_ids = {item["id"] for item in categories}
            if any(item.get("category_id") not in category_ids for item in buttons):
                raise ValueError("button references missing category")
            self._categories = categories
            self._buttons = buttons
            if migrated:
                self._persist()
        except (OSError, ValueError, TypeError, KeyError):
            self._categories = deepcopy(DEFAULT_CATEGORIES)
            self._buttons = deepcopy(COMPANION_BUTTONS)
            self._persist()

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({
                "categories": self._categories,
                "buttons": self._buttons,
                "native_catalog_version": self._catalog_version,
            }, indent=2),
            encoding="utf-8",
        )

    async def snapshot(self) -> dict:
        async with self._lock:
            return {"categories": deepcopy(self._categories), "buttons": deepcopy(self._buttons)}

    async def get_button(self, button_id: str):
        async with self._lock:
            button = next((item for item in self._buttons if item["id"] == button_id), None)
            return deepcopy(button)

    async def create_category(self, name: str) -> dict:
        name = (name or "").strip()
        if not name or len(name) > 40:
            raise ValueError("category name must be 1 to 40 characters")
        async with self._lock:
            if any(item["name"].casefold() == name.casefold() for item in self._categories):
                raise ValueError("category already exists")
            category = {"id": f"category-{uuid.uuid4().hex[:10]}", "name": name, "visible": True}
            self._categories.append(category)
            self._persist()
            return deepcopy(category)

    async def set_category_visible(self, category_id: str, visible: bool) -> dict:
        async with self._lock:
            category = next((item for item in self._categories if item["id"] == category_id), None)
            if category is None:
                raise KeyError(category_id)
            category["visible"] = visible
            self._persist()
            return deepcopy(category)

    async def create_button(self, label: str, category_id: str, action: dict) -> dict:
        label = (label or "").strip()
        if not label or len(label) > 40:
            raise ValueError("button label must be 1 to 40 characters")
        async with self._lock:
            if not any(item["id"] == category_id for item in self._categories):
                raise KeyError(category_id)
            button = {"id": f"user-{uuid.uuid4().hex[:12]}", "label": label, "category_id": category_id, **action}
            self._buttons.append(button)
            self._persist()
            return deepcopy(button)

    async def update_button(self, button_id: str, label: str, category_id: str) -> dict:
        label = (label or "").strip()
        if not label or len(label) > 40:
            raise ValueError("button label must be 1 to 40 characters")
        async with self._lock:
            button = next((item for item in self._buttons if item["id"] == button_id), None)
            if button is None:
                raise KeyError(button_id)
            if not any(item["id"] == category_id for item in self._categories):
                raise KeyError(category_id)
            button["label"] = label
            button["category_id"] = category_id
            self._persist()
            return deepcopy(button)

    async def delete_button(self, button_id: str) -> None:
        async with self._lock:
            before = len(self._buttons)
            self._buttons = [item for item in self._buttons if item["id"] != button_id]
            if len(self._buttons) == before:
                raise KeyError(button_id)
            self._persist()
