"""Files opened straight from a program row, without a desktop profile.

A program action (playback, translation, lyrics) may hold
"file:<ip>|<path>" instead of a key: the file at <path> on the PC <ip> (whose
display agent runs it). Music files play there with mpv (like an audio
profile), .exe files start, anything else opens with its Windows default app.
"|" cannot occur in a Windows path, so it separates the two parts.

GET /files/browse?path=...  folders and files on the streaming PC, so a file
can be picked instead of typed. Only for a browser on the streaming PC
itself (a request from localhost): nobody else on the network gets to
look around its disks.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import string
from pathlib import Path, PureWindowsPath
from typing import Optional, Tuple

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import display_agent
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
    if path.startswith("\\\\") and (PureWindowsPath(path).suffix.lower() == ".exe" or path.endswith("*")):
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
        result = await display_agent.call(cfg, target, route, payload, timeout=5)
    except AgentError as exc:
        if exc.agent_status == 503:
            return {"ok": False, "reason": "file not found on the target PC", "file": name}, 502
        return {**exc.answer(), "file": name}, exc.status
    log.info("FILE %s %s on %s", "play" if kind == "audio" else "open", path, target)
    return {"ok": result.get("ok", True) is not False, "file": name, "kind": kind, "target": target}, 200


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
