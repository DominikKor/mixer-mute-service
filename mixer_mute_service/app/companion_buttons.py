"""Persistent category and button data for Companion controls."""

from __future__ import annotations

import asyncio
import logging
import uuid
from copy import deepcopy
from pathlib import Path
from typing import List

from .jsonstore import backup_corrupt, read_json, write_json_atomic

log = logging.getLogger("mixer.companion_buttons")

COMPANION_BUTTONS = [
    {"id": "beamer-freeze", "label": "Beamer freeze", "kind": "native", "category_id": "beamer", "native_action": {"type": "beamer-command", "command": "freeze"}},
    {"id": "cam2-live-top", "label": "Cam 2 Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "Cam 2", "source": "Cam2"}},
    {"id": "pc2-live-top-left", "label": "PC2 Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "PC2", "source": "Capture_PC2"}},
    {"id": "cam1-toggle", "label": "Cam1 ein/aus", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-source-toggle", "scene": "Preview Scene", "source": "Cam1_klein", "hide_sources": ["Capture_PC2", "Beamer", "Cam2_klein"]}},
    {"id": "pc2-live-top-right", "label": "PC2 Live", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-live", "scene": "PC2", "source": "Capture_PC2"}},
    {"id": "start", "label": "Start", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-scene", "scene": "Willkommen (neu)"}},
    {"id": "ende", "label": "Ende", "kind": "native", "category_id": "obs", "native_action": {"type": "obs-scene", "scene": "Ende (neu)"}},
    {"id": "beamer-hide", "label": "Beamer hide", "kind": "native", "category_id": "beamer", "native_action": {"type": "beamer-set", "hidden": True}},
    {"id": "beamer-unhide", "label": "Beamer unhide", "kind": "native", "category_id": "beamer", "native_action": {"type": "beamer-set", "hidden": False}},
    {"id": "beamer-power-off", "label": "Beamer off", "kind": "native", "category_id": "beamer", "native_action": {"type": "beamer-command", "command": "power-off"}},
    {"id": "beamer-power-on", "label": "Beamer on", "kind": "native", "category_id": "beamer", "native_action": {"type": "beamer-command", "command": "power-on"}},
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
# Every macro switches the four stage zones on (to the start brightness; a zone
# that is already on keeps its level), then runs these catalog actions in order.
# They used to toggle the zones, so a second macro switched the light off.
_STAGE_ZONES = ["left", "center-left", "center-right", "right"]
_OVERALL_LINKS = {
    "gemeinsam-singen": ["native-cam1-preset-5", "native-obs-cam1-live", "native-cam2-preset-5", "native-obs-cam2-live"],
    "musik-klavier": ["native-cam1-preset-3", "native-obs-cam1-live", "native-cam2-preset-3", "native-obs-cam2-live"],
    "pult-klavier": ["native-cam1-preset-2", "native-obs-cam1-live", "native-cam2-preset-2", "native-obs-cam2-live"],
    "pult-playback": ["native-cam1-preset-2", "native-obs-cam1-live", "native-cam2-preset-2", "native-obs-cam2-live"],
    "handmik-klavier": ["native-cam1-preset-5", "native-obs-cam1-live", "native-cam2-preset-2", "native-obs-cam2-live"],
    "handmik-playback": ["native-cam1-preset-5", "native-obs-cam1-live", "native-cam2-preset-2", "native-obs-cam2-live"],
    "predigt": ["native-cam1-preset-1", "native-obs-cam1-live", "native-cam2-preset-1", "native-obs-cam2-live"],
    "hintergrundmusik": ["native-cam1-preset-5", "native-obs-cam1-live", "native-cam2-preset-5", "native-obs-cam2-live"],
    "gebet": ["native-cam1-preset-prayer", "native-obs-cam1-live", "native-cam2-preset-prayer", "native-obs-cam2-live"],
    "qr": ["native-cam1-preset-5", "native-obs-cam1-live", "native-cam2-preset-5", "native-obs-cam2-live"],
    "mission-video": ["native-obs-pc2-live"],
    "instrumental": ["native-cam1-preset-3", "native-obs-cam1-live", "native-cam2-preset-5", "native-obs-cam2-live"],
}
NATIVE_BUTTONS.extend({
    "id": f"native-overall-{button_id}", "label": label, "kind": "native", "category_id": "overall",
    "native_action": {
        "type": "group-macro", "group": group,
        "light_zones_on": _STAGE_ZONES, "buttons": _OVERALL_LINKS.get(button_id, []),
    },
} for button_id, label, group in _OVERALL_GROUPS)

COMPANION_BUTTONS = COMPANION_BUTTONS + NATIVE_BUTTONS
COMPANION_BUTTONS_BY_ID = {button["id"]: button for button in COMPANION_BUTTONS}
# The VISCA keys (presets, zoom) stay catalog actions: the Overall macros,
# program rows and the camera section use them by ID. Since v7 they are no
# longer keys in Steuerung; the camera section shows them instead.
CAMERA_BUTTONS = [button for button in COMPANION_BUTTONS if button.get("category_id") == "cameras"]
CAMERA_BUTTON_IDS = {button["id"] for button in CAMERA_BUTTONS}
# Since v8 the light keys (the Licht section controls the light) and the two
# mixer keys are no longer in Steuerung either. Program rows and keys made
# from them still run them by ID.
CATALOG_ONLY_BUTTONS = [b for b in COMPANION_BUTTONS if b.get("category_id") in ("light", "mixer")]
CATALOG_ONLY_IDS = {button["id"] for button in CATALOG_ONLY_BUTTONS}
NOT_IN_STEUERUNG_IDS = CAMERA_BUTTON_IDS | CATALOG_ONLY_IDS
# Bump when catalog actions change; stored native keys then get the new action.
# v3 added the keys below, v4 made the Overall macros switch the light on,
# v5 added state-aware Beamer hide/unhide actions, v6 adds direct projector
# power and freeze commands, v7 removes the camera keys from Steuerung and v8
# the light and mixer keys.
_NATIVE_CATALOG_VERSION = 8
_NATIVE_CATALOG_V3_IDS = {
    "native-artnet-start-up", "native-artnet-start-down",
    "native-artnet-steps-up", "native-artnet-steps-down",
    "native-artnet-fade-faster", "native-artnet-fade-slower",
    "native-obs-cam1-toggle", "native-obs-cam2-toggle", "native-obs-pc2-toggle",
}
_NATIVE_CATALOG_V5_IDS = {"beamer-unhide"}
_NATIVE_CATALOG_V6_IDS = {"beamer-power-off", "beamer-power-on"}

DEFAULT_CATEGORIES = [
    {"id": "beamer", "name": "Beamer", "visible": True},
    {"id": "obs", "name": "OBS", "visible": True},
    {"id": "overall", "name": "Overall", "visible": True},
]


def _updated_action(current: dict, seed_button: dict) -> dict:
    """The catalog's new action for a stored key. A macro keeps its mute group,
    since the user may have renamed that group."""
    action = deepcopy(seed_button["native_action"])
    old = current.get("native_action")
    if action.get("type") == "group-macro" and isinstance(old, dict) and old.get("group"):
        action["group"] = old["group"]
    return action


def _remove_camera_keys(buttons: List[dict], categories: List[dict], camera_labels: dict):
    """v7: the catalog's camera keys leave Steuerung, wherever the user moved
    them; a renamed one keeps its label for the camera section. The Kameras
    category goes too, unless the user put keys of their own in it. Overall
    macros link catalog actions by ID and stay as they are."""
    for button in buttons:
        if button.get("id") not in CAMERA_BUTTON_IDS:
            continue
        label = button.get("label")
        if isinstance(label, str) and label.strip() and label != COMPANION_BUTTONS_BY_ID[button["id"]]["label"]:
            camera_labels.setdefault(button["id"], label)
    buttons = [button for button in buttons if button.get("id") not in CAMERA_BUTTON_IDS]
    if not any(button.get("category_id") == "cameras" for button in buttons):
        categories = [category for category in categories if category.get("id") != "cameras"]
    return buttons, categories


def _remove_light_mixer_keys(buttons: List[dict], categories: List[dict]):
    """v8: the catalog's light and mixer keys leave Steuerung, wherever the
    user moved them. The Licht and Mixer categories go too, unless the user
    put keys of their own in them."""
    buttons = [button for button in buttons if button.get("id") not in CATALOG_ONLY_IDS]
    used = {button.get("category_id") for button in buttons}
    categories = [c for c in categories if c.get("id") not in ("light", "mixer") or c.get("id") in used]
    return buttons, categories


class CompanionButtonStore:
    def __init__(self, path: Path, seed: List[dict]) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._categories = deepcopy(DEFAULT_CATEGORIES)
        self._buttons = deepcopy([button for button in seed if button["id"] not in NOT_IN_STEUERUNG_IDS])
        # Labels of the camera keys as they were stored in Steuerung before v7
        # (only those that differ from the catalog); the camera section uses them.
        self._camera_labels: dict = {}
        self._catalog_version = _NATIVE_CATALOG_VERSION
        self._load()

    def _load(self) -> None:
        data = read_json(self._path, dict, default=None)
        if data is None:
            # Missing or unreadable (then kept as .corrupt): start from the catalog.
            self._persist()
            return
        migrated = False
        try:
            categories = data["categories"]
            buttons = data["buttons"]
            if not isinstance(categories, list) or not isinstance(buttons, list):
                raise ValueError("invalid Companion button data")
            self._catalog_version = int(data.get("native_catalog_version", 0))
            camera_labels = data.get("camera_labels", {})
            if not isinstance(camera_labels, dict):
                raise ValueError("invalid camera labels")
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
                        current["native_action"] = _updated_action(current, seed_button)
                        current["category_id"] = seed_button["category_id"]
                self._catalog_version = 2
                migrated = True
            if self._catalog_version < _NATIVE_CATALOG_VERSION:
                categories_ids = {item["id"] for item in categories}
                categories.extend(item for item in deepcopy(DEFAULT_CATEGORIES) if item["id"] not in categories_ids)
                button_map = {item["id"]: item for item in buttons}
                # Keys added in v3 are added once; one the user deleted later
                # must not come back with a newer catalog version.
                add_v3 = self._catalog_version < 3
                add_v5 = self._catalog_version < 5
                add_v6 = self._catalog_version < 6
                for seed_button in self._buttons:
                    current = button_map.get(seed_button["id"])
                    add_button = (
                        (add_v3 and seed_button["id"] in _NATIVE_CATALOG_V3_IDS)
                        or (add_v5 and seed_button["id"] in _NATIVE_CATALOG_V5_IDS)
                        or (add_v6 and seed_button["id"] in _NATIVE_CATALOG_V6_IDS)
                    )
                    if current is None and add_button:
                        current = deepcopy(seed_button)
                        buttons.append(current)
                        button_map[seed_button["id"]] = current
                    elif current is not None and seed_button.get("kind") == "native":
                        current["kind"] = "native"
                        current["native_action"] = _updated_action(current, seed_button)
                if self._catalog_version < 7:
                    buttons, categories = _remove_camera_keys(buttons, categories, camera_labels)
                if self._catalog_version < 8:
                    buttons, categories = _remove_light_mixer_keys(buttons, categories)
                self._catalog_version = _NATIVE_CATALOG_VERSION
                migrated = True
            category_ids = {item["id"] for item in categories}
            if any(item.get("category_id") not in category_ids for item in buttons):
                raise ValueError("button references missing category")
            self._categories = categories
            self._buttons = buttons
            self._camera_labels = {
                key: value for key, value in camera_labels.items()
                if key in CAMERA_BUTTON_IDS and isinstance(value, str) and value.strip()
            }
            if migrated:
                self._persist()
        except (ValueError, TypeError, KeyError) as exc:
            backup = backup_corrupt(self._path)
            log.error("Invalid %s (%s); kept it as %s and restored the catalog", self._path, exc, backup)
            self._categories = deepcopy(DEFAULT_CATEGORIES)
            self._buttons = deepcopy([b for b in COMPANION_BUTTONS if b["id"] not in NOT_IN_STEUERUNG_IDS])
            self._camera_labels = {}
            self._persist()

    def _persist(self) -> None:
        write_json_atomic(self._path, {
            "categories": self._categories,
            "buttons": self._buttons,
            "native_catalog_version": self._catalog_version,
            "camera_labels": self._camera_labels,
        })

    async def snapshot(self) -> dict:
        async with self._lock:
            return {"categories": deepcopy(self._categories), "buttons": deepcopy(self._buttons)}

    async def camera_buttons(self) -> List[dict]:
        """The catalog's camera keys, with the labels they had in Steuerung."""
        async with self._lock:
            buttons = deepcopy(CAMERA_BUTTONS)
            for button in buttons:
                button["label"] = self._camera_labels.get(button["id"], button["label"])
            return buttons

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

    async def rename_category(self, category_id: str, name) -> dict:
        name = (name or "").strip() if isinstance(name, str) else ""
        if not name or len(name) > 40:
            raise ValueError("category name must be 1 to 40 characters")
        async with self._lock:
            category = next((item for item in self._categories if item["id"] == category_id), None)
            if category is None:
                raise KeyError(category_id)
            if any(item is not category and item["name"].casefold() == name.casefold() for item in self._categories):
                raise ValueError("category already exists")
            category["name"] = name
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

    async def rename_group(self, old: str, new: str) -> int:
        """Point keys at a renamed mute group (group keys and Overall macros)."""
        async with self._lock:
            changed = 0
            for button in self._buttons:
                if button.get("kind") == "group" and button.get("group") == old:
                    button["group"] = new
                    changed += 1
                action = button.get("native_action")
                if isinstance(action, dict) and action.get("type") == "group-macro" and action.get("group") == old:
                    action["group"] = new
                    changed += 1
            if changed:
                self._persist()
            return changed

    async def delete_button(self, button_id: str) -> None:
        async with self._lock:
            before = len(self._buttons)
            self._buttons = [item for item in self._buttons if item["id"] != button_id]
            if len(self._buttons) == before:
                raise KeyError(button_id)
            self._persist()
