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
GET  /songs/live                  what the screen shows: {song_id, slide, blank, song, slide_count}
                                  slide numbers count the slides as shown: with
                                  "title_slide" on, slide 0 is the title slide
POST /songs/show {song_id, slide} show a slide (unblanks)
POST /songs/next | /songs/prev    step through the current song; after the
                                  last slide "next" blanks the screen
POST /songs/blank                 background only (song stays selected)
POST /songs/hide                  close the song window
POST /songs/create                a new song: written as .foil into the library
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path
from typing import Literal, Optional, Tuple

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from . import display_agent
from .display_agent import AgentError
from .foil import FoilError, build_foil, parse_foil, song_file_name
from .jsonstore import read_json, write_json_atomic
from .responses import reply

router = APIRouter()
log = logging.getLogger("mixer.songs")

TITLE_SLIDE_KEY = "Titel"
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
    # Every song starts with a title slide: the title centred, the copyright
    # below it. show_title then has no effect (the title is on that slide).
    title_slide: bool = True
    show_title: bool = True
    show_copyright: bool = True
    # Text alignment of the slide (lyrics, preview, title, copyright).
    text_align: Literal["left", "center", "right"] = "center"
    # Below the slide, in grey, the first lines of the next slide where room is.
    show_next_preview: bool = True

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
        if not display_agent.allowed_target(value):
            raise ValueError("target_ip must be a LAN address of the display PC")
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

    @staticmethod
    def parse_library(files: list) -> Tuple[dict, list]:
        """(songs, errors) of the agent's file list. Slow for a big library:
        runs in a worker thread, not in the event loop (the mixer link)."""
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
        return songs, errors

    def set_library(self, songs: dict, errors: list) -> dict:
        self.songs, self.errors, self.loaded_at = songs, errors, time.time()
        write_json_atomic(self._library_path, {
            "loaded_at": self.loaded_at, "songs": songs, "errors": errors,
        }, ensure_ascii=False)
        return {"ok": True, "count": len(songs), "errors": errors}

    def add_song(self, name: str, song: dict) -> None:
        self.songs[name] = song
        write_json_atomic(self._library_path, {
            "loaded_at": self.loaded_at, "songs": self.songs, "errors": self.errors,
        }, ensure_ascii=False)

    def slides(self, song_id: str) -> list:
        """The slides as shown, in order. Slide numbers (show, live, next)
        count these. Keep in sync with songDisplaySlides() in index.html."""
        song = self.songs[song_id]
        if self.settings.title_slide:
            return [{"key": TITLE_SLIDE_KEY, "text": song["title"], "title_slide": True}, *song["slides"]]
        return song["slides"]

    def slide_payload(self, song_id: str, slide: int) -> dict:
        song, s = self.songs[song_id], self.settings
        slides = self.slides(song_id)
        if slides[slide].get("title_slide"):
            # Sent as an ordinary slide (the title as its text, always
            # centred), so every display agent version can show it.
            screen = self._screen()
            screen["style"]["align"] = "center"
            return {
                "action": "show", **screen, "text": song["title"], "title": "",
                "copyright": song["copyright"] if s.show_copyright else "", "next": "",
            }
        # Title and copyright on the first lyrics slide only without a title slide.
        first = slide == 0
        # After the last slide "next" goes dark, so there is nothing to preview.
        upcoming = slides[slide + 1]["text"] if s.show_next_preview and slide + 1 < len(slides) else ""
        return {
            "action": "show",
            **self._screen(),
            "text": slides[slide]["text"],
            "title": song["title"] if s.show_title and first else "",
            "copyright": song["copyright"] if s.show_copyright and first else "",
            "next": upcoming,
        }

    def blank_payload(self) -> dict:
        return {"action": "blank", **self._screen(), "text": "", "title": "", "copyright": "", "next": ""}

    def _screen(self) -> dict:
        s = self.settings
        return {
            "monitor": s.monitor, "fullscreen": s.fullscreen,
            "style": {
                "font": s.font, "font_size": s.font_size,
                "text_color": s.text_color, "background_color": s.background_color,
                "align": s.text_align,
            },
        }


def legacy_song_payload(payload: dict) -> dict:
    """The /song request as display agents before the next-slide preview and
    text alignment expect it (they refuse unknown keys)."""
    legacy = {k: v for k, v in payload.items() if k != "next"}
    if isinstance(legacy.get("style"), dict):
        legacy["style"] = {k: v for k, v in legacy["style"].items() if k != "align"}
    return legacy


async def _call_agent(request: Request, path: str, payload: dict, timeout: float = 3) -> Tuple[dict, int]:
    """(result, status) of a call to the display agent named in the song settings."""
    cfg = request.app.state.cfg.desktop_display
    target = request.app.state.songs.settings.target_ip
    try:
        try:
            return await display_agent.call(cfg, target, path, payload, timeout), 200
        except AgentError as exc:
            legacy = legacy_song_payload(payload) if path == "/song" else payload
            if exc.agent_status != 400 or legacy == payload:
                raise
            # An older display agent: show the slide without preview/alignment
            # rather than not at all, until the display service is updated.
            log.warning("Display agent refused the song preview/alignment (old version?); sending the plain slide")
            return await display_agent.call(cfg, target, path, legacy, timeout), 200
    except AgentError as exc:
        if exc.agent_status == 409 and path == "/songs/save":
            return {"ok": False, "reason": "a song file with this name already exists in the library"}, 409
        if exc.agent_status == 404 and path in ("/songs/library", "/songs/save"):
            return {"ok": False, "reason": "song library folder not found on the display PC"}, 502
        if exc.agent_status is not None:
            return {"ok": False, "reason": "display agent rejected the request (update the display service?)"}, 502
        return exc.answer(), exc.status


def _live_view(service: SongService) -> dict:
    live = dict(service.live)
    song_id = live["song_id"]
    live["song"] = {"id": song_id, **service.songs[song_id]} if song_id in service.songs else None
    # Slides as shown (with the title slide first when it is switched on).
    live["slide_count"] = len(service.slides(song_id)) if live["song"] else 0
    return live


async def show_slide(request: Request, song_id: str, slide: int):
    """Show one slide of a song (also used by program rows). Unblanks."""
    service: SongService = request.app.state.songs
    async with service.lock:
        return await _show_locked(request, song_id, slide)


async def _show_locked(request: Request, song_id: str, slide: int):
    """The caller holds ``service.lock``."""
    service: SongService = request.app.state.songs
    song = service.songs.get(song_id)
    if song is None:
        reason = "song library not loaded" if not service.songs else "unknown song"
        return JSONResponse({"ok": False, "reason": reason, "song_id": song_id}, 409 if not service.songs else 404)
    count = len(service.slides(song_id))
    if not 0 <= slide < count:
        return JSONResponse({"ok": False, "reason": "no such slide", "song_id": song_id}, 400)
    result, status = await _call_agent(request, "/song", service.slide_payload(song_id, slide))
    if status == 200 and result.get("ok") is not False:
        service.live = {"song_id": song_id, "slide": slide, "blank": False}
        log.info("SONG show %r slide %s/%s", song_id, slide + 1, count)
        return {"ok": True, **_live_view(service)}
    return reply(result, status)


async def _blank(request: Request):
    service: SongService = request.app.state.songs
    async with service.lock:
        return await _blank_locked(request)


async def _blank_locked(request: Request):
    """The caller holds ``service.lock``."""
    service: SongService = request.app.state.songs
    result, status = await _call_agent(request, "/song", service.blank_payload())
    if status == 200 and result.get("ok") is not False:
        service.live = {**service.live, "blank": True}
        log.info("SONG blank")
        return {"ok": True, **_live_view(service)}
    return reply(result, status)


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
        return reply(result if result.get("ok") is False else {"ok": False, "reason": "invalid library answer"}, status)
    songs, errors = await asyncio.to_thread(SongService.parse_library, result["files"])
    async with service.lock:
        summary = service.set_library(songs, errors)
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


# Next/back read the current slide under the lock: two quick presses step
# two slides instead of both showing the same one.

@router.post("/songs/next")
async def song_next(request: Request):
    service: SongService = request.app.state.songs
    async with service.lock:
        live = service.live
        if live["song_id"] not in service.songs:
            return JSONResponse({"ok": False, "reason": "no song selected"}, 409)
        last = len(service.slides(live["song_id"])) - 1
        if live["blank"] and live["slide"] >= last:
            return {"ok": True, **_live_view(service)}  # already dark after the end
        if live["slide"] >= last:
            return await _blank_locked(request)
        return await _show_locked(request, live["song_id"], live["slide"] + (0 if live["blank"] else 1))


@router.post("/songs/prev")
async def song_prev(request: Request):
    service: SongService = request.app.state.songs
    async with service.lock:
        live = service.live
        if live["song_id"] not in service.songs:
            return JSONResponse({"ok": False, "reason": "no song selected"}, 409)
        # Dark after the last slide: "back" shows the last slide again.
        target = live["slide"] if live["blank"] else max(0, live["slide"] - 1)
        return await _show_locked(request, live["song_id"], target)


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
        return reply(result, status)


def _new_song_fields(body: dict) -> Tuple[str, str, list, list]:
    """(title, copyright, verses, order) of a new song, or ValueError."""
    title, copyright = body.get("title"), body.get("copyright", "")
    slides, refrain, order = body.get("slides"), body.get("refrain"), body.get("order")
    if not isinstance(title, str) or not 0 < len(title.strip()) <= 120:
        raise ValueError("title must have 1 to 120 characters")
    if not isinstance(copyright, str) or len(copyright) > 500:
        raise ValueError("copyright must be at most 500 characters")
    if (not isinstance(slides, list) or not 0 < len(slides) <= 40
            or not all(isinstance(t, str) and t.strip() and len(t) <= 2000 for t in slides)):
        raise ValueError("1 to 40 slides with text are needed")
    if refrain is not None and (not isinstance(refrain, int) or isinstance(refrain, bool) or not 0 <= refrain < len(slides)):
        raise ValueError("refrain must name one of the slides")
    if (not isinstance(order, list) or not 0 < len(order) <= 100
            or not all(isinstance(i, int) and not isinstance(i, bool) and 0 <= i < len(slides) for i in order)):
        raise ValueError("order must list the slides to show")
    # Slides keep their order in the file; verses are numbered, the refrain is "Ref".
    verses, number = [], 0
    for index, text in enumerate(slides):
        if index == refrain:
            key = "Ref"
        else:
            number += 1
            key = str(number)
        verses.append({"key": key, "text": text.strip()})
    return title.strip(), copyright.strip(), verses, order


@router.post("/songs/create")
async def create_song(request: Request, body: dict = Body(...)):
    service: SongService = request.app.state.songs
    if not service.settings.library_path:
        return JSONResponse({"ok": False, "reason": "no song library folder configured"}, 400)
    try:
        title, copyright, verses, order = _new_song_fields(body)
        xml = build_foil(title, verses, order, copyright)
        song = parse_foil(xml)  # the same reader as for the library
        name = song_file_name(title)
    except (ValueError, FoilError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, 400)
    if name in service.songs:
        return JSONResponse({"ok": False, "reason": "a song file with this name already exists in the library",
                             "song_id": name}, 409)
    result, status = await _call_agent(request, "/songs/save", {
        "path": service.settings.library_path, "name": name, "xml": xml.decode("utf-8"),
    }, timeout=10)
    if status != 200 or result.get("ok") is False:
        return JSONResponse(result, status if status != 200 else 502)
    async with service.lock:
        service.add_song(name, song)
    log.info("SONG created %r (%s slides)", name, len(song["slides"]))
    return {"ok": True, "song_id": name, "song": {"id": name, **song}}


@router.get("/songs/{song_id:path}")
async def get_song(song_id: str, request: Request):
    service: SongService = request.app.state.songs
    song = service.songs.get(song_id)
    if song is None:
        return JSONResponse({"ok": False, "reason": "unknown song"}, 404)
    return {"id": song_id, **song}

