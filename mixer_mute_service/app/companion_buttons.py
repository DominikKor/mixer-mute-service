"""Persistent category and button data for Companion controls."""

from __future__ import annotations

import asyncio
import json
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Dict, List

COMPANION_BUTTONS = [
    {"id": "beamer-freeze", "label": "Beamer freeze", "page": 1, "row": 0, "column": 0, "kind": "companion", "category_id": "beamer"},
    {"id": "cam2-live-top", "label": "Cam 2 Live", "page": 1, "row": 0, "column": 1, "kind": "companion", "category_id": "obs"},
    {"id": "pc2-live-top-left", "label": "PC2 Live", "page": 1, "row": 0, "column": 2, "kind": "companion", "category_id": "obs"},
    {"id": "cam1-toggle", "label": "Cam1 ein/aus", "page": 1, "row": 0, "column": 4, "kind": "companion", "category_id": "obs"},
    {"id": "pc2-live-top-right", "label": "PC2 Live", "page": 1, "row": 0, "column": 5, "kind": "companion", "category_id": "obs"},
    {"id": "start", "label": "Start", "page": 1, "row": 0, "column": 6, "kind": "companion", "category_id": "obs"},
    {"id": "ende", "label": "Ende", "page": 1, "row": 0, "column": 7, "kind": "companion", "category_id": "obs"},
    {"id": "beamer-hide", "label": "Beamer hide", "page": 1, "row": 1, "column": 0, "kind": "companion", "category_id": "beamer"},
    {"id": "cam1-live", "label": "Cam 1 Live", "page": 1, "row": 1, "column": 1, "kind": "companion", "category_id": "obs"},
    {"id": "cam2-live-bottom", "label": "Cam 2 Live", "page": 1, "row": 1, "column": 4, "kind": "companion", "category_id": "obs"},
    {"id": "beamer-live", "label": "Beamer Live", "page": 1, "row": 1, "column": 5, "kind": "companion", "category_id": "beamer"},
    {"id": "pultmikro-mute", "label": "Pultmikro Mute", "kind": "channel", "category_id": "mixer", "channel": 1, "operation": "mute"},
    {"id": "gebetsmikro-unmute", "label": "Gebetsmikro Unmute", "kind": "channel", "category_id": "mixer", "channel": 2, "operation": "unmute"},
]

COMPANION_BUTTONS_BY_ID = {button["id"]: button for button in COMPANION_BUTTONS}

DEFAULT_CATEGORIES = [
    {"id": "beamer", "name": "Beamer", "visible": True},
    {"id": "obs", "name": "OBS", "visible": True},
    {"id": "mixer", "name": "Mixer", "visible": True},
]


class CompanionButtonStore:
    def __init__(self, path: Path, seed: List[dict]) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._categories = deepcopy(DEFAULT_CATEGORIES)
        self._buttons = deepcopy(seed)
        self._load()

    def _load(self) -> None:
        if not self._path.exists():
            self._persist()
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            categories = data["categories"]
            buttons = data["buttons"]
            if not isinstance(categories, list) or not isinstance(buttons, list):
                raise ValueError("invalid Companion button data")
            category_ids = {item["id"] for item in categories}
            if any(item.get("category_id") not in category_ids for item in buttons):
                raise ValueError("button references missing category")
            self._categories = categories
            self._buttons = buttons
        except (OSError, ValueError, TypeError, KeyError):
            self._categories = deepcopy(DEFAULT_CATEGORIES)
            self._buttons = deepcopy(COMPANION_BUTTONS)
            self._persist()

    def _persist(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps({"categories": self._categories, "buttons": self._buttons}, indent=2),
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
