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
GET  /songs/live[?have=<id>&rev=<n>]  what the screen shows: {song_id, slide, blank, song,
                                  slide_count, layout_rev} ("song" left out if the
                                  caller already has it in that layout). Slide numbers
                                  count the slides as shown: with "title_slide" on,
                                  slide 0 is the title slide; a verse too long for one
                                  slide counts as two or more (song.shown, song.layout)
POST /songs/layout                the layout of a song (or of an unsaved one) with the
                                  saved or given settings, for the page's previews
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

Layout: the service lays the song out for every screen (app/slide_layout.py:
one text size per song and screen, line breaks, verses split into more
slides where needed) with the font widths and window size the screen's
display agent measures (POST /song/metrics), and sends each agent the lines
and size to draw ("layout"). Display agents from before refuse that; they
get the plain slide (and, older still, without preview and alignment) and
fit it themselves, still with the service's split. What an agent refuses is
remembered for AGENT_MEMORY_SECONDS, so it gets one request per slide.
"""

from __future__ import annotations

import asyncio
import collections
import json
import logging
import re
import time
from pathlib import Path
from typing import List, Literal, Optional, Tuple

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from . import display_agent
from . import slide_layout as layout_engine
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
    # Text size in px on a 1080 px high screen, the same on every slide of a
    # song (app/slide_layout.py): if a verse does not fit, the whole song gets
    # up to 18 % smaller, then the verse is split into two or more slides.
    # Earlier this was the largest size of each slide on its own; a saved
    # value is taken over as it is.
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
        # (seq, live, {ip: request}) of the last step a screen showed (each
        # screen's own request: its layout and monitor).
        self.senders: dict = {}
        self.seq = 0
        self.live_seq = 0
        self.confirmed = (0, dict(self.live), {})
        # Song layouts (app/slide_layout.py), newest last: song_id -> plan.
        self.layouts: dict = {}
        self.layout_rev = 0
        # Font widths and window size per (ip, monitor, fullscreen, font) from the agents.
        self.metrics: dict = {}
        # Per agent: what it could not do ("metrics", or the /song level it takes) and since when.
        self.agent_memory: dict = {}

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
            self.senders[ip] = ScreenSender(ip, self.agent_memory)
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

    def base_slides(self, song_id: str, song: Optional[dict] = None,
                    settings: Optional["SongSettings"] = None) -> list:
        """The song's slides with the title slide first when it is switched
        on, before any verse is split."""
        song = song if song is not None else self.songs[song_id]
        settings = settings or self.settings
        if settings.title_slide:
            return [{"key": TITLE_SLIDE_KEY, "text": song["title"], "title_slide": True}, *song["slides"]]
        return song["slides"]

    def slides(self, song_id: str) -> list:
        """The slides as shown, in order: verses too long for one slide split
        (the song's current layout). Slide numbers (show, live, next) count
        these. The page takes them from the song's "shown"."""
        plan = self.layouts.get(song_id)
        if plan is not None and plan["inputs"] == self._inputs_key(song_id, self.songs[song_id], self.settings):
            return plan["shown"]
        return self.base_slides(song_id)

    @staticmethod
    def _inputs_key(song_id: str, song: dict, settings: "SongSettings") -> str:
        return json.dumps([song_id, song.get("title"), song.get("copyright"),
                           [slide["text"] for slide in song.get("slides", [])],
                           settings.title_slide, settings.show_title, settings.show_copyright,
                           settings.show_next_preview, settings.font, settings.font_size, settings.fullscreen,
                           [[t.ip, t.monitor] for t in settings.targets]], ensure_ascii=False)

    def layout_inputs(self, song: dict, settings: "SongSettings") -> list:
        """The slides to lay out (title/copyright rows on the first lyrics
        slide only without a title slide; the title slide is never split)."""
        inputs = []
        for index, slide in enumerate(self.base_slides("", song, settings)):
            if slide.get("title_slide"):
                inputs.append(layout_engine.SlideInput(song["title"], False, bool(settings.show_copyright and song["copyright"]),
                                                       splittable=False, key=TITLE_SLIDE_KEY))
                continue
            first = index == 0
            inputs.append(layout_engine.SlideInput(
                slide["text"], bool(settings.show_title and first and song["title"]),
                bool(settings.show_copyright and first and song["copyright"]), key=slide.get("key", "")))
        return inputs

    def _slide_parts(self, song_id: str, slide: int) -> Tuple[str, str, str]:
        """(text, title, copyright) of a slide as shown."""
        song, s = self.songs[song_id], self.settings
        shown = self.slides(song_id)[slide]
        if shown.get("title_slide"):
            return song["title"], "", song["copyright"] if s.show_copyright else ""
        if "title_row" in shown:   # laid out: the first part of a split first slide has them
            return (shown["text"], song["title"] if shown["title_row"] else "",
                    song["copyright"] if shown["copyright_row"] else "")
        # Title and copyright on the first lyrics slide only without a title slide.
        first = slide == 0
        return (shown["text"], song["title"] if s.show_title and first else "",
                song["copyright"] if s.show_copyright and first else "")

    def slide_payload(self, song_id: str, slide: int, screen: int = 0) -> dict:
        """The /song request of a slide for one screen (index into the
        targets): with "layout" (lines and size to draw) where that screen's
        display agent measured the font."""
        s = self.settings
        slides = self.slides(song_id)
        text, title, copyright = self._slide_parts(song_id, slide)
        base = self._screen()
        plan = self.layouts.get(song_id)
        extra = {}
        if plan is not None and plan["shown"] is slides and screen < len(plan["screens"]):
            layout = plan["screens"][screen]
            if layout["source"] == "agent":
                drawn = layout["slides"][slide]
                extra = {"layout": {"screen": [layout["width"], layout["height"]], "px": drawn["px"],
                                    "lines": drawn["lines"], "preview": drawn["preview"]}}
        if slides[slide].get("title_slide"):
            # Sent as an ordinary slide (the title as its text, always
            # centred), so every display agent version can show it.
            base["style"]["align"] = "center"
            return {"action": "show", **base, "text": text, "title": title,
                    "copyright": copyright, "next": "", **extra}
        # After the last slide "next" goes dark, so there is nothing to preview.
        upcoming = slides[slide + 1]["text"] if s.show_next_preview and slide + 1 < len(slides) else ""
        return {"action": "show", **base, "text": text, "title": title,
                "copyright": copyright, "next": upcoming, **extra}

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


# How long an agent's refusal is remembered (an updated agent is asked again after that).
AGENT_MEMORY_SECONDS = 600
# A live display agent measures at once; one that does not answer is asked again after METRICS_RETRY_SECONDS.
METRICS_TIMEOUT = 1.5
# An agent that did not answer the metrics request is asked again after this.
METRICS_RETRY_SECONDS = 60
# Window size assumed where the agent cannot tell (full screen / framed window).
ESTIMATE_SCREEN = {True: (1920, 1080), False: (1280, 720)}
# Characters always measured with a new font (so most songs need no second request).
BASE_CHARS = ("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZäöüÄÖÜß0123456789"
              " .,;:!?'\"-–—()|/„“”‚‘’…&")
MAX_METRICS_CHARS = 4000


def song_payload_levels(payload: dict) -> list:
    """The /song request in the forms the display agent versions take,
    newest first: as it is; without "layout" (agents from before the
    service's layout: they fit the slide themselves); also without the
    next-slide preview and text alignment (the oldest). Each form appears once."""
    without_layout = {k: v for k, v in payload.items() if k != "layout"}
    oldest = {k: v for k, v in without_layout.items() if k != "next"}
    if isinstance(oldest.get("style"), dict):
        oldest["style"] = {k: v for k, v in oldest["style"].items() if k != "align"}
    return [payload, without_layout, oldest]


def _remembered(memory: dict, ip: str, what: str, seconds: float = AGENT_MEMORY_SECONDS):
    entry = memory.get((ip, what))
    if entry is None or time.monotonic() - entry[1] > seconds:
        return None
    return entry[0]


def _valid_metrics(answer, asked: str) -> bool:
    widths = answer.get("widths") if isinstance(answer, dict) else None
    return (
        isinstance(widths, list) and len(widths) == len(asked)
        and all(isinstance(w, (int, float)) and not isinstance(w, bool) and 0 <= w <= 1000 for w in widths)
        and all(isinstance(answer.get(k), int) and not isinstance(answer.get(k), bool) and 100 <= answer[k] <= 20000
                for k in ("width", "height"))
    )


async def _screen_for(request: Request, target: "SongTarget", settings: "SongSettings", chars: set):
    """(Screen, source) of one target: the font widths and window size its
    display agent measured ("agent"), asked for the characters still
    missing; else an estimate ("estimate": an agent from before /song/metrics
    or one that does not answer)."""
    service: SongService = request.app.state.songs
    key = (target.ip, target.monitor, settings.fullscreen, settings.font)
    entry = service.metrics.get(key)
    missing = (chars - entry["metrics"].widths.keys()) if entry else (chars | set(BASE_CHARS))
    sender = service.senders.get(target.ip)
    # Never wait for a screen that is down (the step does not either).
    if (missing and not (sender is not None and not sender.up)
            and not _remembered(service.agent_memory, target.ip, "no_metrics")
            and not _remembered(service.agent_memory, target.ip, "metrics_failed", METRICS_RETRY_SECONDS)):
        asked = "".join(sorted(missing))[:MAX_METRICS_CHARS]
        payload = {"font": settings.font, "monitor": target.monitor, "fullscreen": settings.fullscreen, "chars": asked}
        try:
            answer = await display_agent.call(request.app.state.cfg.desktop_display, target.ip, "/song/metrics",
                                              payload, METRICS_TIMEOUT)
        except AgentError as exc:
            if exc.agent_status in (400, 404):
                log.info("Display agent %s cannot measure fonts (old version); estimating the song layout", target.ip)
                service.agent_memory[(target.ip, "no_metrics")] = (True, time.monotonic())
            else:
                log.warning("Display agent %s did not measure the font: %s", target.ip, exc)
                service.agent_memory[(target.ip, "metrics_failed")] = (True, time.monotonic())
        except Exception:  # noqa: BLE001 - a song is shown with an estimated layout rather than not at all
            log.exception("Font widths from display agent %s failed", target.ip)
            service.agent_memory[(target.ip, "metrics_failed")] = (True, time.monotonic())
        else:
            if _valid_metrics(answer, asked):
                widths = {**(entry["metrics"].widths if entry else {}), **dict(zip(asked, answer["widths"]))}
                default = sum(widths.values()) / len(widths) if widths else 60.0
                entry = {"metrics": layout_engine.Metrics(widths, default), "width": answer["width"],
                         "height": answer["height"]}
                service.metrics[key] = entry
            else:
                log.warning("Display agent %s sent invalid font widths", target.ip)
                service.agent_memory[(target.ip, "metrics_failed")] = (True, time.monotonic())
    if entry:
        return layout_engine.Screen(entry["metrics"], entry["width"], entry["height"]), "agent"
    width, height = ESTIMATE_SCREEN[settings.fullscreen]
    return layout_engine.Screen(layout_engine.estimate_metrics(), width, height), "estimate"


async def plan_layout(request: Request, song_id: str, song: dict, settings: "SongSettings",
                      known: Optional[dict] = None) -> dict:
    """The song laid out for all screens of the settings (not stored);
    ``known`` (an earlier layout) is returned as it is if nothing changed."""
    service: SongService = request.app.state.songs
    inputs = service.layout_inputs(song, settings)
    chars = set("".join(slide.text for slide in inputs)) | {" "}
    found = await asyncio.gather(*(_screen_for(request, t, settings, chars) for t in settings.targets))
    inputs_key = SongService._inputs_key(song_id, song, settings)
    metrics_key = [(source, screen.width, screen.height, len(screen.metrics.widths)) for screen, source in found]
    if known is not None and known["inputs"] == inputs_key and known["metrics"] == metrics_key:
        return known
    screens = [screen for screen, _source in found]
    shown, layouts = await asyncio.to_thread(layout_engine.plan_song, screens, inputs, settings.font_size,
                                             settings.show_next_preview)
    base = service.base_slides(song_id, song, settings)
    entries = []
    for slide in shown:
        origin = base[slide.source]
        entry = {**origin, "text": slide.text, "source": slide.source, "part": slide.part, "parts": slide.parts,
                 "title_row": slide.title, "copyright_row": slide.copyright}
        if slide.parts > 1:
            entry["key"] = f"{origin.get('key', '')}.{slide.part}"
        entries.append(entry)
    return {
        "inputs": inputs_key,
        "metrics": metrics_key,
        "shown": entries,
        "screens": [{"width": l.width, "height": l.height, "px": l.px, "slides": l.slides, "source": source}
                    for l, (_screen, source) in zip(layouts, found)],
    }


# Layouts kept (songs stepped through recently).
MAX_LAYOUTS = 16


async def ensure_layout(request: Request, song_id: str) -> dict:
    """The song's layout with the current settings and font widths, worked
    out again only when one of them changed. Keeps slides() and the
    payloads in step. The caller holds ``service.lock``."""
    service: SongService = request.app.state.songs
    song, settings = service.songs[song_id], service.settings
    old = service.layouts.get(song_id)
    plan = await plan_layout(request, song_id, song, settings, old)
    if plan is old:
        return old
    service.layout_rev += 1
    plan["rev"] = service.layout_rev
    service.layouts.pop(song_id, None)
    service.layouts[song_id] = plan
    while len(service.layouts) > MAX_LAYOUTS:
        service.layouts.pop(next(iter(service.layouts)))
    split = [f"{s['key']}" for s in plan["shown"] if s.get("parts", 1) > 1]
    log.info("SONG layout %r: %s slides%s, size %s", song_id, len(plan["shown"]),
             f" (split: {', '.join(split)})" if split else "",
             ", ".join(f"{l['px']:.0f}px@{l['height']}" if l["px"] else "-" for l in plan["screens"]))
    return plan


async def _call_agent(request: Request, path: str, payload: dict, timeout: float = 3) -> Tuple[dict, int]:
    """(result, status) of a call to the display agent of the PC with the library."""
    return await _agent_call(request.app.state.cfg.desktop_display, request.app.state.songs.settings.target_ip,
                             path, payload, timeout)


async def _agent_call(cfg, target: str, path: str, payload: dict, timeout: float,
                      memory: Optional[dict] = None) -> Tuple[dict, int]:
    """(result, status) of a call to the display agent at ``target``.
    memory: SongService.agent_memory, where a refused /song form is kept."""
    memory = memory if memory is not None else {}
    try:
        if path != "/song":
            return await display_agent.call(cfg, target, path, payload, timeout), 200
        # Older display agents refuse newer parts with 400: the next older
        # form, remembered, so the next slides go there in one request.
        levels = song_payload_levels(payload)
        level = _remembered(memory, target, "song_level") or 0
        while True:
            try:
                return await display_agent.call(cfg, target, path, levels[level], timeout), 200
            except AgentError as exc:
                following = next((n for n in range(level + 1, len(levels)) if levels[n] != levels[level]), None)
                if exc.agent_status != 400 or following is None:
                    raise
                level = following
                memory[(target, "song_level")] = (level, time.monotonic())
                log.warning("Display agent %s refused the song request (old version?); sending it %s", target,
                            "without the layout" if level == 1 else "without preview/alignment")
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

    def __init__(self, ip: str, memory: Optional[dict] = None) -> None:
        self.ip = ip
        self.memory = memory if memory is not None else {}   # SongService.agent_memory
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
            result, status = await _agent_call(cfg, self.ip, "/song", payload, SONG_SCREEN_TIMEOUT, self.memory)
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
    """The request for one screen with its own monitor (the payload is that
    screen's: with its layout)."""
    return payload if payload["action"] == "hide" else {**payload, "monitor": target.monitor}


async def _step(request: Request, plan):
    """One show/blank/hide on every screen. ``plan(service)`` runs under the
    lock (a coroutine: awaited) and returns (payload, new live state, log
    text), or an answer; the payload is one request for all screens or a
    function (screen index -> request), as each screen gets its own layout. The
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
        if asyncio.iscoroutine(planned):
            planned = await planned
        if not isinstance(planned, tuple):
            return planned
        payload, live, note = planned
        service.seq += 1
        seq = service.seq
        targets = list(service.settings.targets)
        senders = {target.ip: service.sender(target.ip) for target in targets}
        up = [target.ip for target in targets if senders[target.ip].up]
        # Each screen's own request (its layout, its monitor).
        bodies = {target.ip: _for_screen(payload(index) if callable(payload) else payload, target)
                  for index, target in enumerate(targets)}
        futures = {target.ip: senders[target.ip].submit(seq, cfg, bodies[target.ip]) for target in targets}
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
            service.confirmed = (seq, dict(live), bodies)
        elif not reached and service.live_seq == seq:
            _, confirmed_live, confirmed_bodies = service.confirmed
            service.live = dict(confirmed_live)
            for target in service.settings.targets:
                # A screen added since then has no request of its own: it shows nothing new.
                service.sender(target.ip).reset(cfg, confirmed_bodies.get(target.ip))
    if not reached:
        result, status = failure
        return reply({**result, "screens": screens}, status)
    log.info(note)
    failed = [entry for entry in screens if not entry["ok"]]
    return {"ok": True, **_live_view(service), **({"failed": failed} if failed else {})}


def _song_view(song_id: str, song: dict, plan: Optional[dict], slides: list) -> dict:
    """A song for the page: with "shown" (the slides as shown, split verses
    included) and "layout" (how the first screen draws them), if laid out."""
    view = {"id": song_id, **song, "shown": [_shown_entry(slide) for slide in slides]}
    if plan is not None and plan["shown"] is slides and plan["screens"]:
        first = plan["screens"][0]
        view["layout"] = {k: first[k] for k in ("width", "height", "px", "slides", "source")}
        view["layout_rev"] = plan["rev"]
    return view


def _shown_entry(slide: dict) -> dict:
    keep = ("key", "text", "title_slide", "part", "parts", "source", "title_row", "copyright_row")
    return {k: slide[k] for k in keep if k in slide}


def _live_view(service: SongService) -> dict:
    live = dict(service.live)
    song_id = live["song_id"]
    if song_id in service.songs:
        slides = service.slides(song_id)
        live["song"] = _song_view(song_id, service.songs[song_id], service.layouts.get(song_id), slides)
        live["layout_rev"] = live["song"].get("layout_rev", 0)
        # Slides as shown (title slide first when it is on, split verses).
        live["slide_count"] = len(slides)
    else:
        live["song"], live["layout_rev"], live["slide_count"] = None, 0, 0
    live["screens"] = service.screens()
    return live


async def show_slide(request: Request, song_id: str, slide: int):
    """Show one slide of a song (also used by program rows). Unblanks."""
    return await _step(request, lambda service: _plan_show(request, service, song_id, slide))


async def _plan_show(request: Request, service: SongService, song_id: str, slide: int):
    song = service.songs.get(song_id)
    if song is None:
        reason = "song library not loaded" if not service.songs else "unknown song"
        return JSONResponse({"ok": False, "reason": reason, "song_id": song_id}, 409 if not service.songs else 404)
    await ensure_layout(request, song_id)   # the split decides the slide numbers
    count = len(service.slides(song_id))
    if not 0 <= slide < count:
        return JSONResponse({"ok": False, "reason": "no such slide", "song_id": song_id}, 400)
    return (lambda screen: service.slide_payload(song_id, slide, screen),
            {"song_id": song_id, "slide": slide, "blank": False}, f"SONG show {song_id!r} slide {slide + 1}/{count}")


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
        live = service.live
        if live["song_id"] in service.songs:
            # The live song's slides may split differently now: keep the
            # number in range (the next show sends the new layout).
            await ensure_layout(request, live["song_id"])
            live["slide"] = min(live["slide"], len(service.slides(live["song_id"])) - 1)
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
        live = service.live
        if live["song_id"] in service.songs:
            await ensure_layout(request, live["song_id"])
            live["slide"] = min(live["slide"], len(service.slides(live["song_id"])) - 1)
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
async def song_live(request: Request, have: Optional[str] = None, rev: Optional[int] = None):
    """What the screen shows. With ?have=<song_id>&rev=<layout_rev> (the
    page already has that song in that layout) the song itself is left out
    while it is still the live one: the page polls this every 1.5 s."""
    view = _live_view(request.app.state.songs)
    if (have is not None and view["song_id"] == have and view["song"] is not None
            and (rev is None or rev == view["layout_rev"])):
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
        return _plan_show(request, service, live["song_id"], live["slide"] + (0 if live["blank"] else 1))
    return await _step(request, plan)


@router.post("/songs/prev")
async def song_prev(request: Request):
    def plan(service: SongService):
        live = service.live
        if live["song_id"] not in service.songs:
            return JSONResponse({"ok": False, "reason": "no song selected"}, 409)
        # Dark after the last slide: "back" shows the last slide again.
        return _plan_show(request, service, live["song_id"], live["slide"] if live["blank"] else max(0, live["slide"] - 1))
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


def _layout_song(body: dict) -> dict:
    """An unsaved song for /songs/layout: {title, copyright, slides: [{key, text}]}, or ValueError."""
    song = body.get("song")
    if not isinstance(song, dict):
        raise ValueError("song or song_id required")
    title, copyright, slides = song.get("title", ""), song.get("copyright", ""), song.get("slides")
    if not isinstance(title, str) or len(title) > 200 or not isinstance(copyright, str) or len(copyright) > 1000:
        raise ValueError("invalid title or copyright")
    if (not isinstance(slides, list) or len(slides) > 200
            or not all(isinstance(x, dict) and isinstance(x.get("text"), str) and len(x["text"]) <= 8000
                       and isinstance(x.get("key", ""), str) and len(x.get("key", "")) <= 20 for x in slides)):
        raise ValueError("slides must be up to 200 {key, text}")
    return {"title": title, "copyright": copyright, "books": [],
            "slides": [{"key": x.get("key", ""), "text": x["text"]} for x in slides]}


@router.post("/songs/layout")
async def song_layout(request: Request, body: dict = Body(...)):
    """How a song is laid out (slides as shown, the first screen's lines and
    sizes) with the saved settings or, for the settings preview, with the
    given ones (not saved): {song_id} or {song: {title, copyright, slides}},
    optional {settings}."""
    service: SongService = request.app.state.songs
    changes = body.get("settings") or {}
    if not isinstance(changes, dict):
        return JSONResponse({"ok": False, "reason": "settings must be an object"}, 400)
    merged = {**service.settings.model_dump(), **changes}
    if "targets" not in changes and "monitor" in changes:
        del merged["targets"]
    try:
        settings = SongSettings(**merged)
        song_id = body.get("song_id")
        if isinstance(song_id, str):
            if song_id not in service.songs:
                return JSONResponse({"ok": False, "reason": "unknown song"}, 404)
            song = service.songs[song_id]
        else:
            song_id, song = "", _layout_song(body)
    except (ValidationError, ValueError, TypeError) as exc:
        return JSONResponse({"ok": False, "reason": str(exc).splitlines()[0]}, 400)
    plan = await plan_layout(request, song_id, song, settings)
    plan["rev"] = 0
    view = _song_view(song_id, song, plan, plan["shown"])
    view.pop("layout_rev", None)
    return {"ok": True, "song": view}


@router.get("/songs/{song_id:path}")
async def get_song(song_id: str, request: Request):
    service: SongService = request.app.state.songs
    song = service.songs.get(song_id)
    if song is None:
        return JSONResponse({"ok": False, "reason": "unknown song"}, 404)
    return {"id": song_id, **song}

