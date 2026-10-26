"""Strip colours of the web UI: one optional colour per channel.

Purely cosmetic (the strip border in the UI), nothing here talks to the mixer.
Stored in channel_colors.json next to config.yaml, so every device (iPad,
streaming PC) shows the same colours.

GET  /channel-colors              -> {"colors": {"2": "green"}, "palette": [...]}
POST /channels/{channel}/color    {"color": "green"} or {"color": null} to clear
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from .config import default_config_path

router = APIRouter()

PALETTE = ("red", "orange", "yellow", "green", "cyan", "blue", "purple", "pink")
_lock = asyncio.Lock()


def _path() -> Path:
    return default_config_path().parent / "channel_colors.json"


def _load() -> dict:
    try:
        raw = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): v for k, v in raw.items() if v in PALETTE and str(k).isdigit()}


def _save(colors: dict) -> None:
    path = _path()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(colors, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


@router.get("/channel-colors")
async def get_channel_colors():
    return {"colors": _load(), "palette": list(PALETTE)}


@router.post("/channels/{channel}/color")
async def set_channel_color(channel: int, request: Request, body: dict = Body(...)):
    if channel not in request.app.state.cfg.valid_channels():
        return JSONResponse({"ok": False, "channel": channel, "reason": "invalid channel"}, 400)
    color = body.get("color")
    if color is not None and color not in PALETTE:
        return JSONResponse({"ok": False, "channel": channel, "reason": "unknown color"}, 400)
    async with _lock:
        colors = _load()
        if color is None:
            colors.pop(str(channel), None)
        else:
            colors[str(channel)] = color
        _save(colors)
    return {"ok": True, "channel": channel, "color": color}
