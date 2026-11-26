"""Song display: FoilPresenter songs from the display PC, shown slide by slide.

The song library stays where FoilPresenter keeps it, on the display PC. The
display agent there reads the .foil files from the configured folder (and
.pptx songs: app/pptx_song.py); this
service parses them (app/foil.py), keeps the result in song_library.json and
sends one slide at a time back to the agent, which shows it full screen. The
settings (folder, PC, monitor, look) are shared by all songs.

GET  /songs/settings              PUT|POST /songs/settings
POST /songs/library/reload        read the library again from the display PC
GET  /songs                       [{id, title, books, slide_count}]
GET  /songs/{song_id}             one song with all its slides
GET  /songs/live[?have=<id>]      what the screen shows: {song_id, slide, blank, song, slide_count}
                                  ("song" left out if the caller already has it)
                                  slide numbers count the slides as shown: with
                                  "title_slide" on, slide 0 is the title slide
POST /songs/show {song_id, slide} show a slide (unblanks)
POST /songs/next | /songs/prev    step through the current song; after the
                                  last slide "next" blanks the screen
POST /songs/blank                 background only (song stays selected)
POST /songs/hide                  close the song window
POST /songs/create                a new song: written as .foil into the library

Screens: the lyrics go to one or more screens at once ("targets": one PC
and monitor each, at most one per PC: a display agent has one song window).
Every show, blank and hide is queued for all of them at once (ScreenSender:
in order, 1 s timeout); a step waits only for the screens that answer,
never for one that is down, which keeps only the newest slide and gets it
again every 5 s until it answers. Answers and /songs/live carry "screens" (per screen: ok, or the
reason it failed). "target_ip" is the PC that holds the library; "monitor"
(the first screen's) is kept for older pages and files: settings without
"targets" mean that one screen, and an older page that saves "monitor"
changes the first screen only.
"""

from __future__ import annotations

import asyncio
import collections
import logging
import re
import time
from pathlib import Path
from typing import List, Literal, Optional, Tuple

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import display_agent
from .display_agent import AgentError
from .foil import FoilError, build_foil, parse_foil, song_file_name
from .jsonstore import read_json, write_json_atomic
from .pptx_song import parse_pptx
from .responses import reply

router = APIRouter()
log = logging.getLogger("mixer.songs")

TITLE_SLIDE_KEY = "Titel"
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# C:\... or \\server\share\...
_WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+)")
DEFAULT_SONG_PC = "192.168.188.11"
MAX_SONG_SCREENS = 4


class SongTarget(BaseModel):
    """One screen the lyrics are shown on: a PC's display agent and its monitor."""
    ip: str
    monitor: int = Field(2, ge=1, le=8)

    @field_validator("ip")
    @classmethod
    def _ip(cls, value: str) -> str:
        if not display_agent.allowed_target(value):
            raise ValueError("ip must be a LAN address of a PC with the display service")
        return value


def clean_targets(targets: list) -> list:
    """The valid screens of a stored list, the first one per PC (a bad or
    doubled entry must not cost all the other song settings)."""
    cleaned, seen = [], set()
    for entry in targets[:MAX_SONG_SCREENS * 2]:
        try:
            target = SongTarget(**entry) if isinstance(entry, dict) else None
        except (ValidationError, TypeError):
            target = None
        if target is not None and target.ip not in seen:
            seen.add(target.ip)
            cleaned.append(target.model_dump())
    return cleaned[:MAX_SONG_SCREENS]


class SongSettings(BaseModel):
    library_path: str = Field("", max_length=260)
    target_ip: str = DEFAULT_SONG_PC         # the PC with the library
    monitor: int = Field(2, ge=1, le=8)      # the first screen's (older pages and files)
    targets: List[SongTarget] = Field(min_length=1, max_length=MAX_SONG_SCREENS)
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

    @model_validator(mode="before")
    @classmethod
    def _one_screen(cls, data):
        """Settings from before several screens: their PC and monitor are the one screen."""
        if isinstance(data, dict) and "targets" not in data:
            data = {**data, "targets": [{"ip": data.get("target_ip", DEFAULT_SONG_PC), "monitor": data.get("monitor", 2)}]}
        return data

    @model_validator(mode="after")
    def _screens(self):
        ips = [target.ip for target in self.targets]
        if len(set(ips)) != len(ips):
            raise ValueError("one screen per PC (a PC shows one song window)")
        self.monitor = self.targets[0].monitor
        return self

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
        # One sender per screen's PC (ScreenSender), and the step numbers:
        # live is set when a step is sent, confirmed once a screen took it:
        # (seq, live, payload) of the last step a screen showed.
        self.senders: dict = {}
        self.seq = 0
        self.live_seq = 0
        self.confirmed = (0, dict(self.live), None)

    def _load_settings(self) -> SongSettings:
        data = read_json(self._settings_path, dict, default={})
        if isinstance(data.get("targets"), list):
            cleaned = clean_targets(data["targets"])
            if len(cleaned) != len(data["targets"]):
                log.warning("%s: dropped %s invalid or duplicate song screen(s)",
                            self._settings_path, len(data["targets"]) - len(cleaned))
            data = {**data, "targets": cleaned} if cleaned else {k: v for k, v in data.items() if k != "targets"}
        try:
            return SongSettings(**data)
        except (ValidationError, ValueError, TypeError) as exc:
            log.warning("Ignoring invalid %s: %s", self._settings_path, exc)
            return SongSettings()

    def save_settings(self, settings: SongSettings) -> None:
        write_json_atomic(self._settings_path, settings.model_dump(), ensure_ascii=False, indent=2)
        self.settings = settings
        keep = {target.ip for target in settings.targets}
        for ip in [ip for ip in self.senders if ip not in keep]:
            self.senders.pop(ip).close()

    def sender(self, ip: str) -> "ScreenSender":
        if ip not in self.senders:
            self.senders[ip] = ScreenSender(ip)
        return self.senders[ip]

    def screens(self) -> list:
        """Per screen: ok (None while nothing was sent there yet) or the reason it failed."""
        screens = []
        for target in self.settings.targets:
            sender = self.senders.get(target.ip)
            entry = {"ip": target.ip, "monitor": target.monitor,
                     "ok": sender.up if sender is not None and sender.tried else None}
            if entry["ok"] is False:
                entry["reason"] = sender.reason
            screens.append(entry)
        return screens

    def close(self) -> None:
        for sender in self.senders.values():
            sender.close()
        self.senders.clear()

    @staticmethod
    def parse_library(files: list) -> Tuple[dict, list]:
        """(songs, errors) of the agent's file list. Slow for a big library:
        runs in a worker thread, not in the event loop (the mixer link)."""
        songs, errors = {}, []
        for entry in files:
            name = entry.get("name") if isinstance(entry, dict) else None
            if isinstance(name, str) and name.lower().endswith(".pptx") and "pptx_parts" in entry:
                # PowerPoint songs: one slide of the presentation is one slide of the song.
                try:
                    songs[name] = parse_pptx(name, entry["pptx_parts"])
                except FoilError as exc:
                    errors.append({"file": name, "reason": str(exc)})
                continue
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
        count these. Keep in sync with songDisplaySlides() in app/static/js/songs.js."""
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
            "monitor": s.targets[0].monitor, "fullscreen": s.fullscreen,   # monitor: per screen (_step)
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
    """(result, status) of a call to the display agent of the PC with the library."""
    return await _agent_call(request.app.state.cfg.desktop_display, request.app.state.songs.settings.target_ip,
                             path, payload, timeout)


async def _agent_call(cfg, target: str, path: str, payload: dict, timeout: float) -> Tuple[dict, int]:
    """(result, status) of a call to the display agent at ``target``."""
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


SONG_SCREEN_TIMEOUT = 1.0   # a live display agent answers at once (202)
SONG_SCREEN_RETRY = 5.0     # a screen that is down gets the newest slide again this often
UNREACHABLE = "display agent unavailable"


class ScreenSender:
    """Sends /song requests to one PC's display agent, one at a time and in
    order: a screen that answers gets every step. While the PC does not
    answer it is "down": steps do not wait for it, only the newest request
    is kept for it (it shows the current slide when it is back, without
    replaying the ones it missed), and that is tried again every
    SONG_SCREEN_RETRY seconds until it answers. The requests are passed
    through as they are (the payload is the caller's business)."""

    MAX_QUEUE = 8

    def __init__(self, ip: str) -> None:
        self.ip = ip
        self.up = True          # until a request fails
        self.tried = False
        self.reason: Optional[str] = None
        self.latest = None      # (cfg, payload): what the screen should show now
        self.queue = collections.deque()   # (seq, cfg, payload): not sent yet
        self.done_seq = 0
        self.waiters: list = []  # (seq, future): steps waiting for this screen
        self.task: Optional[asyncio.Task] = None
        self.wake: Optional[asyncio.Event] = None

    def submit(self, seq: int, cfg, payload: dict) -> asyncio.Future:
        """Queue a request; the future gets (ok, result, status) once it or
        a newer one was sent."""
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        self.waiters.append((seq, future))
        if not self.up:
            self.queue.clear()   # down: only the newest counts
        self.queue.append((seq, cfg, payload))
        while len(self.queue) > self.MAX_QUEUE:
            self.queue.popleft()
        self.latest = (cfg, payload)
        if self.wake is None:
            self.wake = asyncio.Event()
        self.wake.set()
        if self.task is None or self.task.done():
            self.task = loop.create_task(self._run(), name=f"song screen {self.ip}")
        return future

    def reset(self, cfg, payload: Optional[dict]) -> None:
        """Forget the steps not sent yet; the screen is to show ``payload``
        (what the screens last showed), also after a send still on its way."""
        self.queue.clear()
        self.latest = (cfg, payload) if payload is not None else None
        if payload is None:
            return
        self.queue.append((self.done_seq, cfg, payload))
        if self.wake is not None:
            self.wake.set()
        if self.task is None or self.task.done():
            self.task = asyncio.get_running_loop().create_task(self._run(), name=f"song screen {self.ip}")

    def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
        for _, future in self.waiters:
            if not future.done():
                future.cancel()
        self.waiters = []

    async def _run(self) -> None:
        while True:
            self.wake.clear()
            if not self.queue:
                if self.up or self.latest is None:
                    return
                try:
                    await asyncio.wait_for(self.wake.wait(), SONG_SCREEN_RETRY)
                except asyncio.TimeoutError:
                    if not self.queue:
                        self.queue.append((self.done_seq, *self.latest))
                continue
            seq, cfg, payload = self.queue.popleft()
            result, status = await _agent_call(cfg, self.ip, "/song", payload, SONG_SCREEN_TIMEOUT)
            ok = status == 200 and result.get("ok") is not False
            if not ok and (self.up or not self.tried):
                log.warning("SONG screen %s down: %s", self.ip, result.get("reason"))
            elif ok and not self.up:
                log.info("SONG screen %s back", self.ip)
            self.tried = True
            self.up = ok
            self.reason = None if ok else (result.get("reason") or f"HTTP {status}")
            if not ok:
                while len(self.queue) > 1:   # just went down: skip to the newest
                    self.queue.popleft()
            self.done_seq = max(self.done_seq, seq)
            waiting = []
            for waiting_seq, future in self.waiters:
                if waiting_seq > seq:
                    waiting.append((waiting_seq, future))
                elif not future.done():
                    future.set_result((ok, result, status))
            self.waiters = waiting


def _for_screen(payload: dict, target: SongTarget) -> dict:
    """The request for one screen: the same for all, with its own monitor."""
    return payload if payload["action"] == "hide" else {**payload, "monitor": target.monitor}


async def _step(request: Request, plan):
    """One show/blank/hide on every screen. ``plan(service)`` runs under the
    lock and returns (payload, new live state, log text), or an answer. The
    lock is held only to decide and queue (the next press steps on from the
    new state); then the step waits for the screens that are up, at most
    SONG_SCREEN_TIMEOUT, never for one that is down. If no screen took it,
    the live state AND every screen's queue go back to the last step a screen
    took: the page and the screens (also the ones that come back later)
    always agree, and the presses that reached no screen are dropped."""
    service: SongService = request.app.state.songs
    cfg = request.app.state.cfg.desktop_display
    async with service.lock:
        planned = plan(service)
        if not isinstance(planned, tuple):
            return planned
        payload, live, note = planned
        service.seq += 1
        seq = service.seq
        targets = list(service.settings.targets)
        senders = {target.ip: service.sender(target.ip) for target in targets}
        up = [target.ip for target in targets if senders[target.ip].up]
        futures = {target.ip: senders[target.ip].submit(seq, cfg, _for_screen(payload, target))
                   for target in targets}
        service.live, service.live_seq = live, seq
    waited = [futures[ip] for ip in (up or futures)]
    await asyncio.wait(waited, timeout=SONG_SCREEN_TIMEOUT + 0.2)
    screens, failure = [], None
    for target in targets:
        future = futures[target.ip]
        if future.done() and not future.cancelled():
            ok, result, status = future.result()
        else:   # down (not waited for) or still busy
            ok, result, status = False, {"ok": False, "reason": senders[target.ip].reason or UNREACHABLE}, 502
        entry = {"ip": target.ip, "monitor": target.monitor, "ok": ok}
        if not ok:
            entry["reason"] = result.get("reason") or f"HTTP {status}"
            failure = failure or (result, status)
        screens.append(entry)
    reached = any(entry["ok"] for entry in screens)
    async with service.lock:
        if reached and seq > service.confirmed[0]:
            service.confirmed = (seq, dict(live), payload)
        elif not reached and service.live_seq == seq:
            _, confirmed_live, confirmed_payload = service.confirmed
            service.live = dict(confirmed_live)
            for target in service.settings.targets:
                service.sender(target.ip).reset(
                    cfg, _for_screen(confirmed_payload, target) if confirmed_payload is not None else None)
    if not reached:
        result, status = failure
        return reply({**result, "screens": screens}, status)
    log.info(note)
    failed = [entry for entry in screens if not entry["ok"]]
    return {"ok": True, **_live_view(service), **({"failed": failed} if failed else {})}


def _live_view(service: SongService) -> dict:
    live = dict(service.live)
    song_id = live["song_id"]
    live["song"] = {"id": song_id, **service.songs[song_id]} if song_id in service.songs else None
    # Slides as shown (with the title slide first when it is switched on).
    live["slide_count"] = len(service.slides(song_id)) if live["song"] else 0
    live["screens"] = service.screens()
    return live


async def show_slide(request: Request, song_id: str, slide: int):
    """Show one slide of a song (also used by program rows). Unblanks."""
    return await _step(request, lambda service: _plan_show(service, song_id, slide))


def _plan_show(service: SongService, song_id: str, slide: int):
    song = service.songs.get(song_id)
    if song is None:
        reason = "song library not loaded" if not service.songs else "unknown song"
        return JSONResponse({"ok": False, "reason": reason, "song_id": song_id}, 409 if not service.songs else 404)
    count = len(service.slides(song_id))
    if not 0 <= slide < count:
        return JSONResponse({"ok": False, "reason": "no such slide", "song_id": song_id}, 400)
    return (service.slide_payload(song_id, slide), {"song_id": song_id, "slide": slide, "blank": False},
            f"SONG show {song_id!r} slide {slide + 1}/{count}")


def _plan_blank(service: SongService):
    return service.blank_payload(), {**service.live, "blank": True}, "SONG blank"


@router.get("/songs/settings")
async def get_song_settings(request: Request):
    return request.app.state.songs.settings.model_dump()


@router.put("/songs/settings")
@router.post("/songs/settings")
async def set_song_settings(request: Request, body: dict = Body(...)):
    service: SongService = request.app.state.songs
    merged = {**service.settings.model_dump(), **body}
    if "targets" not in body and "monitor" in body:
        # An older page knows one screen (its PC and monitor): that is the
        # first one; the others stay.
        first = {"ip": body.get("target_ip", service.settings.targets[0].ip), "monitor": body["monitor"]}
        merged["targets"] = [first, *[t.model_dump() for t in service.settings.targets[1:]]]
    try:
        settings = SongSettings(**merged)
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
async def song_live(request: Request, have: Optional[str] = None):
    """What the screen shows. With ?have=<song_id> (the page already has that
    song) the song itself is left out while it is still the live one: the
    page polls this every 1.5 s."""
    view = _live_view(request.app.state.songs)
    if have is not None and view["song_id"] == have and view["song"] is not None:
        view["song"] = None
        view["song_unchanged"] = True
    return view


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
    def plan(service: SongService):
        live = service.live
        if live["song_id"] not in service.songs:
            return JSONResponse({"ok": False, "reason": "no song selected"}, 409)
        last = len(service.slides(live["song_id"])) - 1
        if live["blank"] and live["slide"] >= last:
            return {"ok": True, **_live_view(service)}  # already dark after the end
        if live["slide"] >= last:
            return _plan_blank(service)
        return _plan_show(service, live["song_id"], live["slide"] + (0 if live["blank"] else 1))
    return await _step(request, plan)


@router.post("/songs/prev")
async def song_prev(request: Request):
    def plan(service: SongService):
        live = service.live
        if live["song_id"] not in service.songs:
            return JSONResponse({"ok": False, "reason": "no song selected"}, 409)
        # Dark after the last slide: "back" shows the last slide again.
        return _plan_show(service, live["song_id"], live["slide"] if live["blank"] else max(0, live["slide"] - 1))
    return await _step(request, plan)


@router.post("/songs/blank")
async def song_blank(request: Request):
    return await _step(request, _plan_blank)


@router.post("/songs/hide")
async def song_hide(request: Request):
    return await _step(request, lambda service: (
        {"action": "hide"}, {"song_id": None, "slide": 0, "blank": False}, "SONG hide"))


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

