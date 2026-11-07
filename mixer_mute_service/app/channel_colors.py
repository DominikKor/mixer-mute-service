"""Strip colours of the web UI: one optional colour per channel.

Purely cosmetic (the strip border in the UI), nothing here talks to the mixer.
Stored in channel_colors.json next to config.yaml, so every device (iPad,
streaming PC) shows the same colours.

GET  /channel-colors              -> {"colors": {"2": "green"}, "palette": [...]}
POST /channels/{channel}/color    {"color": "green"} or {"color": null} to clear
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from .config import default_config_path
from .jsonstore import read_json, write_json_atomic

router = APIRouter()

# "black" (for black microphones) shows as a grey border, since a black one
# would vanish on the dark strips. It replaced the former light blue "cyan".
PALETTE = ("red", "orange", "yellow", "green", "black", "blue", "purple", "pink")
_lock = asyncio.Lock()


def _path() -> Path:
    return default_config_path().parent / "channel_colors.json"


def _load() -> dict:
    raw = read_json(_path(), dict, default={})
    return {str(k): v for k, v in raw.items() if v in PALETTE and str(k).isdigit()}


def _save(colors: dict) -> None:
    write_json_atomic(_path(), colors, sort_keys=True)


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
