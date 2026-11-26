"""Documents (PDF, PowerPoint, Word) shown page by page by a display agent.

A program row's file field (Übersetzung, ...) or a file profile that is a
document is not handed to Windows' default app: the agent renders its pages
itself, full screen on the right monitor (desktop_agent/documents.py), and
pages through them on request:

POST /documents/{next|prev|first|last}   {"pc": ip} optional
GET  /documents/current                  {"shown": bool, "pc", "title", "page", "pages"}

Starting a document (/display) is answered once its first page is on screen,
with "shown": true (the beamer freeze of a row comes after it), or after at
most 15 s with "shown": false (a conversion still running, or the agent opened
it with its default app). Older agents answer at once, without "shown".

Without "pc" they go to the PC that showed a document last (one document per
agent, a new one replaces it). An agent from before documents answers 404:
the answer then says so.

File fields (file:<ip>|<path>) open a document on the monitor of that PC's
Liedanzeige screen (the projector), else monitor 1, always full screen, under
one profile id per PC ("doc-192-168-188-11"): the program's next row closes
it (app/profile_stops.py) unless it shows the same file again; then it stays
on its page.
"""

from __future__ import annotations

import logging
from pathlib import PureWindowsPath
from typing import Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from . import display_agent
from .display_agent import AgentError

router = APIRouter()
log = logging.getLogger("mixer.documents")

# Keep in sync with desktop_agent/documents.py.
DOCUMENT_SUFFIXES = {".pdf", ".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".ppsm", ".odp",
                     ".doc", ".docx", ".docm", ".odt", ".rtf"}
ACTIONS = ("next", "prev", "first", "last")
DOCUMENT_TIMEOUT_SECONDS = 5.0  # the agent renders the page before it answers
# A document's /display is answered once its first page is on screen ("shown":
# true), after at most 15 s ("shown": false, e.g. a conversion still running;
# desktop_agent DOCUMENT_ACK_SECONDS).
DISPLAY_TIMEOUT_SECONDS = 20.0
OLD_AGENT_REASON = "Der Display Service auf diesem PC ist zu alt zum Blättern; bitte aktualisieren."


def is_document(path: str) -> bool:
    return PureWindowsPath(path).suffix.lower() in DOCUMENT_SUFFIXES


def document_id(target_ip: str) -> str:
    """The profile id a file field's document has on that PC (one per PC)."""
    return "doc-" + target_ip.replace(".", "-")


def document_monitor(state, target_ip: str) -> int:
    """The monitor for a file field's document: the Liedanzeige screen's on
    that PC (the projector), else 1."""
    songs = getattr(state, "songs", None)
    try:
        for target in songs.settings.targets:
            if target.ip == target_ip:
                return int(target.monitor)
    except AttributeError:  # no song service (tests, a broken songs file)
        pass
    return 1


def note_shown(state, target_ip: str, profile_id: str) -> None:
    """A document was opened there: paging goes to that PC."""
    state.document_shown = (target_ip, profile_id)


def _target(request: Request, body: Optional[dict]) -> Optional[str]:
    pc = (body or {}).get("pc")
    if pc is not None:
        return pc if isinstance(pc, str) and display_agent.allowed_target(pc) else ""
    shown = getattr(request.app.state, "document_shown", None)
    return shown[0] if shown else None


async def _call(request: Request, target: str, payload: dict):
    try:
        answer = await display_agent.call(request.app.state.cfg.desktop_display, target, "/document", payload,
                                          timeout=DOCUMENT_TIMEOUT_SECONDS)
    except AgentError as exc:
        if exc.agent_status == 404:
            return JSONResponse({"ok": False, "reason": OLD_AGENT_REASON, "pc": target}, 409)
        if exc.agent_status == 409:
            return JSONResponse({"ok": False, "reason": "Auf diesem PC wird kein Dokument angezeigt.",
                                 "pc": target, "shown": False}, 409)
        return JSONResponse({**exc.answer(), "pc": target}, exc.status)
    return {**answer, "ok": answer.get("ok", True) is not False, "pc": target, "shown": True}


@router.post("/documents/{action}")
async def page_document(action: str, request: Request, body: Optional[dict] = Body(None)):
    if action not in ACTIONS:
        return JSONResponse({"ok": False, "reason": "unknown document action"}, 404)
    target = _target(request, body)
    if target == "":
        return JSONResponse({"ok": False, "reason": "pc must be a LAN address"}, 400)
    if target is None:
        return JSONResponse({"ok": False, "reason": "Es wird kein Dokument angezeigt.", "shown": False}, 409)
    return await _call(request, target, {"action": action})


@router.get("/documents/current")
async def current_document(request: Request):
    target = _target(request, None)
    if target is None:
        return {"ok": True, "shown": False}
    answer = await _call(request, target, {"action": "status"})
    if isinstance(answer, JSONResponse):
        # Nothing shown there (any more), an old agent, an agent that is off.
        return {"ok": True, "shown": False, "pc": target}
    return answer
