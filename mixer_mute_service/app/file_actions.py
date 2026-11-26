"""Files opened straight from a program row, without a desktop profile.

A program action (playback, translation, lyrics) may hold
"file:<ip>|<path>" instead of a key: the file at <path> on the PC <ip> (whose
display agent runs it). Music files play there with mpv (like an audio
profile), .exe files start, anything else opens with its Windows default app.
"|" cannot occur in a Windows path, so it separates the two parts.

POST /files/pick {"start": folder}  opens the real Windows "open file" dialog
on the streaming PC's screen (through its display agent, which runs in the
signed-in session) and returns the chosen path. A browser cannot tell a page
the path of a file picked in its own dialog, and the service itself has no
screen. When the agent cannot show it (an older agent, agent not running),
the answer says "fallback" and the page uses the folder list instead:

GET /files/browse?path=...  folders and files on the streaming PC, so a file
can be picked instead of typed.

Both only for a browser on the streaming PC itself (a request from
localhost): nobody else on the network gets to look around its disks or
open dialogs on its screen.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import string
from datetime import date
from pathlib import Path, PureWindowsPath
from typing import Optional, Tuple

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from . import agent_update, display_agent, mission_spotlight
from .display_agent import AgentError

router = APIRouter()
log = logging.getLogger("mixer.file_actions")

FILE_PREFIX = "file:"
MAX_PATH = 400
AUDIO_SUFFIXES = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus", ".wma"}
# The target that means "the streaming PC itself" (its own display agent).
THIS_PC = "127.0.0.1"
# C:\... or \\server\share\... (POSIX paths only matter for development)
_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+|/)")
_SKIP_NAMES = {"$recycle.bin", "system volume information", "$windows.~bt", "$windows.~ws"}
MAX_ENTRIES = 1000
# Starting these (directly or with "open with its default app") runs code;
# never from a network share. The display agent checks the same after "*"
# is filled in.
RUNNABLE_SUFFIXES = {".exe", ".com", ".bat", ".cmd", ".msi", ".scr", ".ps1", ".vbs", ".vbe",
                     ".js", ".jse", ".wsf", ".wsh", ".lnk", ".url", ".hta", ".cpl", ".pif"}


def runs_from_network_share(path: str, may_be_program: bool = False) -> bool:
    """A runnable file on a network share (or, for programs, a "*" name that
    may resolve to one)."""
    if not (path.startswith("\\\\") or path.startswith("//")):
        return False
    return PureWindowsPath(path).suffix.lower() in RUNNABLE_SUFFIXES or (may_be_program and path.endswith("*"))


def is_file_action(value) -> bool:
    return isinstance(value, str) and value.startswith(FILE_PREFIX)


def parse_file_action(value: str) -> Tuple[str, str]:
    """(target ip, path) of a "file:<ip>|<path>" value, or ValueError."""
    target, sep, path = value[len(FILE_PREFIX):].partition("|")
    if not sep:
        raise ValueError("file action needs a target and a path")
    if not display_agent.allowed_target(target):
        raise ValueError("target must be a LAN address")
    if not path or len(path) > MAX_PATH or not _ABSOLUTE_RE.match(path) or any(ord(c) < 32 for c in path):
        raise ValueError("file path must be a full path on the target PC")
    if runs_from_network_share(path, may_be_program=True):
        # Never start a program from a network share (anyone who can write
        # there could swap it).
        raise ValueError("programs must be on the target PC, not on a network share")
    return target, path


def file_action_error(value: str) -> Optional[str]:
    try:
        parse_file_action(value)
    except ValueError as exc:
        return str(exc)
    return None


def file_kind(path: str) -> str:
    suffix = PureWindowsPath(path).suffix.lower()
    if suffix in AUDIO_SUFFIXES:
        return "audio"
    return "program" if suffix == ".exe" else "file"


async def run_file_action(request: Request, value: str) -> Tuple[dict, int]:
    """Have the target PC's display agent play, start or open the file."""
    target, path = parse_file_action(value)
    cfg = request.app.state.cfg.desktop_display
    kind = file_kind(path)
    name = PureWindowsPath(path).name
    if kind == "audio":
        route, payload = "/audio", {"id": "program-file", "action": "play", "path": path}
    else:
        route, payload = "/display", {
            "id": "program-file", "title": name, "kind": kind, "path": path,
            "args": [], "fullscreen": False, "monitor": 1, "position": "top-left",
        }
    try:
        result = await display_agent.call(cfg, target, route, payload, timeout=display_agent.AUDIO_TIMEOUT_SECONDS)
    except AgentError as exc:
        if exc.agent_status == 503:
            # The agent says why the player did not start (no audio device, ...);
            # anything else at 503 means the file is missing there.
            agent_reason = (exc.body or {}).get("reason")
            if isinstance(agent_reason, str) and agent_reason.startswith(PLAYER_FAILURE_PREFIX):
                return {"ok": False, "reason": agent_reason, "file": name}, 502
            return {"ok": False, "reason": await no_file_reason(request, target, path), "file": name}, 502
        return {**exc.answer(), "file": name}, exc.status
    log.info("FILE %s %s on %s", "play" if kind == "audio" else "open", path, target)
    return {"ok": result.get("ok", True) is not False, "file": name, "kind": kind, "target": target}, 200


# The first Display Service that fills in "*" in a path.
WILDCARD_MIN_VERSION = (1, 2, 1, 135)
PLAYER_FAILURE_PREFIX = "audio player could not be started"


def agent_version_tuple(version) -> Optional[Tuple[int, ...]]:
    """(1, 2, 1, 237) from "1.2.1-237" (or "v1.2.1-237"); None when it is no version."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)-(\d+)", str(version or "").strip())
    return tuple(int(part) for part in match.groups()) if match else None


def spotlight_search(today: Optional[date] = None) -> dict:
    """The week and quarter a Mission Spotlight profile looks for today."""
    now = mission_spotlight.current(today)
    return {"week": now["week"], "quarter": now["quarter"], "year": now["year"]}


def not_found_reason(path: str, pc: str = "Ziel-PC", spotlight: Optional[dict] = None,
                     old_agent: bool = False) -> str:
    """Why the agent found nothing (German, for the display): the pattern and
    folder it looked in, on which PC, and for Mission Spotlight the week and
    quarter. old_agent adds that the PC's Display Service is too old for "*"."""
    if "*" not in path:
        return f"Datei auf dem {pc} nicht gefunden."
    parent = PureWindowsPath(path)
    reason = f"Keine Datei „{parent.name.replace('*', '…')}“ in {parent.parent} auf dem {pc} gefunden"
    if spotlight:
        reason += f" (Woche {spotlight['week']:02d}, {spotlight['quarter']}. Quartal {spotlight['year']})"
    reason += "."
    if old_agent:
        minimum = "v" + ".".join(map(str, WILDCARD_MIN_VERSION[:3])) + f"-{WILDCARD_MIN_VERSION[3]}"
        reason += f" Der Display Service auf diesem PC ist älter als {minimum} und kennt „*“ in Pfaden noch nicht; bitte aktualisieren."
    return reason


async def no_file_reason(request: Request, target: str, path: str, spotlight: Optional[dict] = None) -> str:
    """not_found_reason for a PC: its configured name, and the old-agent hint
    only when that PC's Display Service really is older than "*" support (one
    /info call, only made for a path with "*")."""
    pc = "Ziel-PC"
    try:
        pc = next((item["name"] for item in await agent_update.agent_pcs(request) if item["pc"] == target), pc)
    except Exception:  # the name is nice to have, the reason must still come out
        log.debug("Name of %s not available", target, exc_info=True)
    old_agent = False
    if "*" in path:
        try:
            info = await agent_update._agent_info(request, target)
            if info.get("present"):
                version = agent_version_tuple(info.get("version"))
                # No /info at all or a version below the first with "*": old.
                old_agent = info.get("version") is None or (version is not None and version < WILDCARD_MIN_VERSION)
        except Exception:
            log.debug("Version of the agent on %s not available", target, exc_info=True)
    return not_found_reason(path, pc, spotlight, old_agent)


def _from_this_pc(request: Request) -> bool:
    return bool(request.client) and request.client.host in ("127.0.0.1", "::1")


def _list_folder(path: str) -> dict:
    if not path:
        if os.name == "nt":
            roots = [f"{d}:\\" for d in string.ascii_uppercase if os.path.exists(f"{d}:\\")]
        else:
            roots = ["/"]
        return {"ok": True, "path": "", "parent": None, "dirs": [{"name": r, "path": r} for r in roots], "files": []}
    folder = Path(path)
    if not folder.is_absolute() or not folder.is_dir():
        raise FileNotFoundError(path)
    dirs, files = [], []
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        name = entry.name
        if name.startswith(".") or name.casefold() in _SKIP_NAMES:
            continue
        try:
            if entry.is_dir():
                dirs.append({"name": name, "path": str(entry)})
            elif entry.is_file():
                files.append({"name": name, "path": str(entry), "kind": file_kind(name)})
        except OSError:
            continue
        if len(dirs) + len(files) >= MAX_ENTRIES:
            break
    parent = folder.parent
    return {
        "ok": True, "path": str(folder),
        # The drive root goes back to the drive list.
        "parent": "" if parent == folder else str(parent),
        "dirs": dirs, "files": files,
    }


@router.get("/files/browse-available")
async def browse_available(request: Request):
    return {"available": _from_this_pc(request), "this_pc": THIS_PC}


@router.get("/files/browse")
async def browse(request: Request, path: str = ""):
    if not _from_this_pc(request):
        return JSONResponse({"ok": False, "reason": "the file browser only works on the streaming PC itself"}, 403)
    try:
        return await asyncio.to_thread(_list_folder, path)
    except FileNotFoundError:
        return JSONResponse({"ok": False, "reason": "folder not found"}, 404)
    except OSError as exc:
        return JSONResponse({"ok": False, "reason": f"folder could not be read: {exc.strerror or exc}"}, 403)


# The agent waits up to 10 minutes for a pick (desktop_agent PICK_TIMEOUT_SECONDS).
PICK_TIMEOUT_SECONDS = 620


@router.post("/files/pick")
async def pick(request: Request, body: Optional[dict] = Body(None)):
    if not _from_this_pc(request):
        return JSONResponse({"ok": False, "reason": "the file dialog only works on the streaming PC itself"}, 403)
    start = (body or {}).get("start") or ""
    if not isinstance(start, str) or len(start) > MAX_PATH or any(ord(c) < 32 for c in start):
        return JSONResponse({"ok": False, "reason": "invalid start folder"}, 400)
    try:
        result = await display_agent.call(
            request.app.state.cfg.desktop_display, THIS_PC, "/pick-file", {"start": start},
            timeout=PICK_TIMEOUT_SECONDS)
    except AgentError as exc:
        busy = exc.agent_status in (408, 409)
        reason = (exc.body or {}).get("reason") if busy else exc.reason
        # Busy or timed out: say so. Anything else (no agent, an agent without
        # the dialog): the page shows its own folder list instead.
        return JSONResponse({"ok": False, "reason": reason or exc.reason, "fallback": not busy},
                            exc.agent_status if busy else 503)
    path = result.get("path")
    if path is not None and (not isinstance(path, str) or not _ABSOLUTE_RE.match(path) or len(path) > MAX_PATH):
        return JSONResponse({"ok": False, "reason": "the dialog returned no usable path", "fallback": True}, 502)
    return {"ok": True, "path": path}
