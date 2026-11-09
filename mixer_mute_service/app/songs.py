"""Song display: FoilPresenter songs from the display PC, shown slide by slide.

The song library stays where FoilPresenter keeps it, on the display PC. The
display agent there reads the .foil files from the configured folder; this
service parses them (app/foil.py), keeps the result in song_library.json and
sends one slide at a time back to the agent, which shows it full screen. The
settings (folder, PC, monitor, look) are shared by all songs.

GET  /songs/settings              PUT|POST /songs/settings
POST /songs/library/reload        read the library again from the display PC
GET  /songs                       [{id, title, books, slide_count}]
GET  /songs/{song_id}             one song with all its slides
GET  /songs/live                  what the screen shows: {song_id, slide, blank, song}
POST /songs/show {song_id, slide} show a slide (unblanks)
POST /songs/next | /songs/prev    step through the current song; after the
                                  last slide "next" blanks the screen
POST /songs/blank                 background only (song stays selected)
POST /songs/hide                  close the song window
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request
from ipaddress import IPv4Address
from pathlib import Path
from typing import Optional, Tuple

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from .foil import FoilError, parse_foil
from .jsonstore import read_json, write_json_atomic

router = APIRouter()
log = logging.getLogger("mixer.songs")

AGENT_PORT = 8766
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# C:\... or \\server\share\...
_WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+)")


class SongSettings(BaseModel):
    library_path: str = Field("", max_length=260)
    target_ip: str = "192.168.188.11"
    monitor: int = Field(2, ge=1, le=8)
    fullscreen: bool = True
    font: str = Field("Verdana", min_length=1, max_length=60)
    # Largest text size in px on a 1080 px high screen; long slides get smaller.
    font_size: int = Field(72, ge=12, le=240)
    text_color: str = "#ffffff"
    background_color: str = "#000000"
    show_title: bool = True
    show_copyright: bool = True

    @field_validator("library_path")
    @classmethod
    def _library_path(cls, value: str) -> str:
        value = value.strip()
        if value and (not _WINDOWS_PATH_RE.match(value) or "\0" in value):
            raise ValueError("library_path must be an absolute Windows path")
        return value

    @field_validator("target_ip")
    @classmethod
    def _target_ip(cls, value: str) -> str:
        IPv4Address(value)
        return value

    @field_validator("font")
    @classmethod
    def _font(cls, value: str) -> str:
        value = value.strip()
        if not value or any(ord(c) < 32 for c in value):
            raise ValueError("invalid font name")
        return value

    @field_validator("text_color", "background_color")
    @classmethod
    def _color(cls, value: str) -> str:
        if not _COLOR_RE.match(value):
            raise ValueError("colors use #rrggbb")
        return value.lower()


class SongService:
    """Settings, the parsed library and what is on screen right now."""

    def __init__(self, data_dir: Path) -> None:
        self._settings_path = data_dir / "song_display.json"
        self._library_path = data_dir / "song_library.json"
        self.lock = asyncio.Lock()
        self.settings = self._load_settings()
        library = read_json(self._library_path, dict, default={})
        self.songs: dict = library.get("songs", {}) if isinstance(library.get("songs"), dict) else {}
        self.loaded_at: Optional[float] = library.get("loaded_at")
        self.errors: list = library.get("errors", []) if isinstance(library.get("errors"), list) else []
        self.live = {"song_id": None, "slide": 0, "blank": False}

    def _load_settings(self) -> SongSettings:
        try:
            return SongSettings(**read_json(self._settings_path, dict, default={}))
        except (ValidationError, ValueError, TypeError) as exc:
            log.warning("Ignoring invalid %s: %s", self._settings_path, exc)
            return SongSettings()

    def save_settings(self, settings: SongSettings) -> None:
        write_json_atomic(self._settings_path, settings.model_dump(), ensure_ascii=False, indent=2)
        self.settings = settings

    def replace_library(self, files: list) -> dict:
        songs, errors = {}, []
        for entry in files:
            name = entry.get("name") if isinstance(entry, dict) else None
            xml = entry.get("xml") if isinstance(entry, dict) else None
            if not isinstance(name, str) or not isinstance(xml, str) or not name.lower().endswith(".foil"):
                continue
            try:
                songs[name] = parse_foil(xml.encode("utf-8"))
            except FoilError as exc:
                errors.append({"file": name, "reason": str(exc)})
        self.songs, self.errors, self.loaded_at = songs, errors, time.time()
        write_json_atomic(self._library_path, {
            "loaded_at": self.loaded_at, "songs": songs, "errors": errors,
        }, ensure_ascii=False)
        return {"ok": True, "count": len(songs), "errors": errors}

    def slide_payload(self, song_id: str, slide: int) -> dict:
        song, s = self.songs[song_id], self.settings
        first = slide == 0
        return {
            "action": "show",
            **self._screen(),
            "text": song["slides"][slide]["text"],
            "title": song["title"] if s.show_title and first else "",
            "copyright": song["copyright"] if s.show_copyright and first else "",
        }

    def blank_payload(self) -> dict:
        return {"action": "blank", **self._screen(), "text": "", "title": "", "copyright": ""}

    def _screen(self) -> dict:
        s = self.settings
        return {
            "monitor": s.monitor, "fullscreen": s.fullscreen,
            "style": {
                "font": s.font, "font_size": s.font_size,
                "text_color": s.text_color, "background_color": s.background_color,
            },
        }


def _agent_post(url: str, token: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Display-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


async def _call_agent(request: Request, path: str, payload: dict, timeout: float = 3) -> Tuple[dict, int]:
    """(result, status) of a call to the display agent named in the song settings."""
    cfg = request.app.state.cfg.desktop_display
    token = os.environ.get(cfg.token_env, "")
    if not cfg.enabled or not token:
        return {"ok": False, "reason": "desktop display is not configured"}, 503
    service: SongService = request.app.state.songs
    url = f"http://{service.settings.target_ip}:{AGENT_PORT}{path}"
    try:
        return await asyncio.to_thread(_agent_post, url, token, payload, timeout), 200
    except urllib.error.HTTPError as exc:
        log.warning("Display agent rejected %s: HTTP %s", path, exc.code)
        reason = "song library folder not found on the display PC" if exc.code == 404 and path == "/songs/library" \
            else "display agent rejected the request (update the display service?)"
        return {"ok": False, "reason": reason}, 502
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("Display agent unavailable for %s: %s", path, exc)
        return {"ok": False, "reason": "display agent unavailable"}, 502


def _respond(result: dict, status: int):
    return result if status == 200 and result.get("ok") is not False else JSONResponse(result, status if status != 200 else 502)


def _live_view(service: SongService) -> dict:
    live = dict(service.live)
    song_id = live["song_id"]
    live["song"] = {"id": song_id, **service.songs[song_id]} if song_id in service.songs else None
    return live


async def show_slide(request: Request, song_id: str, slide: int):
    """Show one slide of a song (also used by program rows). Unblanks."""
    service: SongService = request.app.state.songs
    async with service.lock:
        song = service.songs.get(song_id)
        if song is None:
            reason = "song library not loaded" if not service.songs else "unknown song"
            return JSONResponse({"ok": False, "reason": reason, "song_id": song_id}, 409 if not service.songs else 404)
        if not 0 <= slide < len(song["slides"]):
            return JSONResponse({"ok": False, "reason": "no such slide", "song_id": song_id}, 400)
        result, status = await _call_agent(request, "/song", service.slide_payload(song_id, slide))
        if status == 200 and result.get("ok") is not False:
            service.live = {"song_id": song_id, "slide": slide, "blank": False}
            log.info("SONG show %r slide %s/%s", song_id, slide + 1, len(song["slides"]))
            return {"ok": True, **_live_view(service)}
        return _respond(result, status)


async def _blank(request: Request):
    service: SongService = request.app.state.songs
    async with service.lock:
        result, status = await _call_agent(request, "/song", service.blank_payload())
        if status == 200 and result.get("ok") is not False:
            service.live = {**service.live, "blank": True}
            log.info("SONG blank")
            return {"ok": True, **_live_view(service)}
        return _respond(result, status)


@router.get("/songs/settings")
async def get_song_settings(request: Request):
    return request.app.state.songs.settings.model_dump()


@router.put("/songs/settings")
@router.post("/songs/settings")
async def set_song_settings(request: Request, body: dict = Body(...)):
    service: SongService = request.app.state.songs
    try:
        settings = SongSettings(**{**service.settings.model_dump(), **body})
    except (ValidationError, TypeError) as exc:
        first = exc.errors()[0] if isinstance(exc, ValidationError) and exc.errors() else {}
        reason = f"{'.'.join(str(p) for p in first.get('loc', ()))}: {first.get('msg', 'invalid settings')}".strip(": ")
        return JSONResponse({"ok": False, "reason": reason}, 400)
    async with service.lock:
        service.save_settings(settings)
    return {"ok": True, "settings": settings.model_dump()}


@router.post("/songs/library/reload")
async def reload_song_library(request: Request):
    service: SongService = request.app.state.songs
    if not service.settings.library_path:
        return JSONResponse({"ok": False, "reason": "no song library folder configured"}, 400)
    result, status = await _call_agent(
        request, "/songs/library", {"path": service.settings.library_path}, timeout=30)
    if status != 200 or result.get("ok") is False or not isinstance(result.get("files"), list):
        return _respond(result if result.get("ok") is False else {"ok": False, "reason": "invalid library answer"}, status)
    async with service.lock:
        summary = service.replace_library(result["files"])
    log.info("Song library loaded: %s songs, %s unreadable", summary["count"], len(summary["errors"]))
    return {**summary, "truncated": bool(result.get("truncated"))}


@router.get("/songs")
async def list_songs(request: Request):
    service: SongService = request.app.state.songs
    songs = [
        {"id": song_id, "title": song["title"], "books": song["books"], "slide_count": len(song["slides"])}
        for song_id, song in service.songs.items()
    ]
    songs.sort(key=lambda s: s["title"].casefold())
    return {"loaded_at": service.loaded_at, "errors": service.errors, "songs": songs}


@router.get("/songs/live")
async def song_live(request: Request):
    return _live_view(request.app.state.songs)


@router.post("/songs/show")
async def song_show(request: Request, body: dict = Body(...)):
    song_id, slide = body.get("song_id"), body.get("slide", 0)
    if not isinstance(song_id, str) or not isinstance(slide, int) or isinstance(slide, bool):
        return JSONResponse({"ok": False, "reason": "song_id and slide required"}, 400)
    return await show_slide(request, song_id, slide)


@router.post("/songs/next")
async def song_next(request: Request):
    service: SongService = request.app.state.songs
    live = service.live
    if live["song_id"] not in service.songs:
        return JSONResponse({"ok": False, "reason": "no song selected"}, 409)
    last = len(service.songs[live["song_id"]]["slides"]) - 1
    if live["blank"] and live["slide"] >= last:
        return {"ok": True, **_live_view(service)}  # already dark after the end
    if live["slide"] >= last:
        return await _blank(request)
    return await show_slide(request, live["song_id"], live["slide"] + (0 if live["blank"] else 1))


@router.post("/songs/prev")
async def song_prev(request: Request):
    service: SongService = request.app.state.songs
    live = service.live
    if live["song_id"] not in service.songs:
        return JSONResponse({"ok": False, "reason": "no song selected"}, 409)
    # Dark after the last slide: "back" shows the last slide again.
    target = live["slide"] if live["blank"] else max(0, live["slide"] - 1)
    return await show_slide(request, live["song_id"], target)


@router.post("/songs/blank")
async def song_blank(request: Request):
    return await _blank(request)


@router.post("/songs/hide")
async def song_hide(request: Request):
    service: SongService = request.app.state.songs
    async with service.lock:
        result, status = await _call_agent(request, "/song", {"action": "hide"})
        if status == 200 and result.get("ok") is not False:
            service.live = {"song_id": None, "slide": 0, "blank": False}
            log.info("SONG hide")
            return {"ok": True, **_live_view(service)}
        return _respond(result, status)


@router.get("/songs/{song_id:path}")
async def get_song(song_id: str, request: Request):
    service: SongService = request.app.state.songs
    song = service.songs.get(song_id)
    if song is None:
        return JSONResponse({"ok": False, "reason": "unknown song"}, 404)
    return {"id": song_id, **song}

