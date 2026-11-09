"""Program templates: a typical service saved once, applied with its songs.

A template is a copy of the program rows. Rows marked as song slots
("Liedplatz") get the songs of the day when the template is applied: the
songs fill the slots in order, songs beyond the last slot are added after it
as copies of that slot. Applying replaces the whole program.

Songs usually arrive as a list of titles, one per line. POST /songs/match
finds each line in the song library (title, other titles or book number);
lines it cannot place get candidates and are left for the user to pick.

GET    /program/templates                     [{id, name, rows, slot_count}]
POST   /program/templates {name, slots, replace}
                                              save the current program; slots
                                              are the ids of its song-slot rows
DELETE /program/templates/{id}
POST   /program/templates/{id}/apply {songs: [{song_id, text}], dry_run}
                                              dry_run: only return the rows
POST   /songs/match {lines: [...]}            [{line, song_id, title, candidates}]
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
import uuid
from copy import deepcopy
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse

from .jsonstore import read_json, write_json_atomic
from .program_items import ProgramItemError, ProgramItemStore

router = APIRouter()
log = logging.getLogger("mixer.program_templates")

MAX_TEMPLATES = 50
MAX_ROWS = 100
MAX_SONGS = 40
_ROW_FIELDS = (
    "time", "title", "person", "group", "playback", "translation", "lyrics",
    "playback_description", "translation_description", "lyrics_description", "song",
)


class TemplateError(ValueError):
    pass


def _template_row(item: dict, slot: bool) -> dict:
    row = ProgramItemStore._validated_fields({field: item.get(field) for field in _ROW_FIELDS})
    if slot:
        row["song"] = None  # filled when the template is applied
    return {**row, "song_slot": slot}


def _clean_name(name) -> str:
    name = name.strip() if isinstance(name, str) else ""
    if not name or len(name) > 60 or any(ord(c) < 32 for c in name):
        raise TemplateError("template name must contain 1 to 60 characters")
    return name


class ProgramTemplateStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._templates: List[dict] = []
        for entry in read_json(path, list, default=[]):
            try:
                self._templates.append({
                    "id": str(entry["id"]),
                    "name": _clean_name(entry["name"]),
                    "rows": [_template_row(row, bool(row.get("song_slot"))) for row in entry["rows"]],
                })
            except (KeyError, TypeError, TemplateError, ProgramItemError) as exc:
                log.warning("Skipping invalid program template in %s: %s", path, exc)

    def _persist(self) -> None:
        write_json_atomic(self._path, self._templates, ensure_ascii=False)

    @staticmethod
    def _view(template: dict) -> dict:
        return {**deepcopy(template), "slot_count": sum(row["song_slot"] for row in template["rows"])}

    async def list(self) -> List[dict]:
        async with self._lock:
            return [self._view(t) for t in sorted(self._templates, key=lambda t: t["name"].casefold())]

    async def get(self, template_id: str) -> Optional[dict]:
        async with self._lock:
            template = next((t for t in self._templates if t["id"] == template_id), None)
            return self._view(template) if template else None

    async def save(self, name: str, items: List[dict], slot_ids: set, replace: bool = False) -> dict:
        """Save program rows as a template. An existing name is only replaced
        with replace=True (KeyError otherwise)."""
        name = _clean_name(name)
        if not items:
            raise TemplateError("the program is empty")
        if len(items) > MAX_ROWS:
            raise TemplateError(f"at most {MAX_ROWS} rows")
        rows = [_template_row(item, item["id"] in slot_ids) for item in items]
        async with self._lock:
            existing = next((t for t in self._templates if t["name"].casefold() == name.casefold()), None)
            if existing and not replace:
                raise KeyError(name)
            if existing:
                existing.update({"name": name, "rows": rows})
                template = existing
            else:
                if len(self._templates) >= MAX_TEMPLATES:
                    raise TemplateError(f"at most {MAX_TEMPLATES} templates")
                template = {"id": uuid.uuid4().hex, "name": name, "rows": rows}
                self._templates.append(template)
            self._persist()
            return self._view(template)

    async def delete(self, template_id: str) -> bool:
        async with self._lock:
            before = len(self._templates)
            self._templates = [t for t in self._templates if t["id"] != template_id]
            if len(self._templates) == before:
                return False
            self._persist()
            return True


def fill_template(rows: List[dict], songs: List[dict]) -> List[dict]:
    """Program rows from template rows and the songs of the day.

    songs: [{"song_id": file name or None, "text": the line as typed,
    "title": the song's title}]. Songs
    fill the slots in order; slots left over stay without a song; songs left
    over are added right after the last slot as copies of it (or at the end
    with the last row's time if the template has no slot). A song that is not
    in the library keeps its line in the row title so nothing is lost.
    """
    result = [{k: v for k, v in row.items() if k != "song_slot"} for row in deepcopy(rows)]
    slots = [i for i, row in enumerate(rows) if row.get("song_slot")]

    def put(row: dict, song: dict) -> None:
        if song.get("song_id"):
            row["song"] = song["song_id"]
        elif song.get("text"):
            row["title"] = f"{row['title']}: {song['text']}"[:120]

    for index, song in zip(slots, songs):
        put(result[index], song)
    extra = songs[len(slots):]
    if extra:
        if slots:
            model, at = result[slots[-1]], slots[-1] + 1
            # Copy of the slot as the template has it (not the song just put in).
            model = {**model, "title": rows[slots[-1]]["title"], "song": None}
        else:
            model = {field: "" for field in _ROW_FIELDS}
            model.update({"time": rows[-1]["time"] if rows else "10:00", "group": "",
                          "playback": None, "translation": None, "lyrics": None, "song": None})
            at = len(result)
        added = []
        for song in extra:
            row = dict(model)
            if slots:
                put(row, song)
            else:  # no slot to copy: a plain row named after the song
                row.update(title=(song.get("title") or song.get("text") or "Lied")[:120],
                           song=song.get("song_id"))
            added.append(row)
        result[at:at] = added
    return result


# ---- matching pasted song lines to the library ---------------------------------

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "ß": "ss"})
_LIST_MARK_RE = re.compile(r"^\s*(?:\d{1,2}[.)]\s+|[-–•*]\s*)")


def _norm(text: str) -> str:
    text = text.casefold().translate(_UMLAUTS)
    text = unicodedata.normalize("NFKD", text)
    text = "".join(c for c in text if not unicodedata.combining(c))
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def clean_line(line: str) -> str:
    return _LIST_MARK_RE.sub("", line).strip()


def _score(line: str, words: set, numbers: set, song: dict) -> int:
    best = 0
    for title in song.get("titles") or [song.get("title", "")]:
        title = _norm(title)
        if not title:
            continue
        if title == line:
            return 100
        title_words = set(title.split())
        if len(line) >= 4 and (title.startswith(line) or line.startswith(title)):
            best = max(best, 80)
        elif words and words <= title_words:
            best = max(best, 70)
        elif words:
            overlap = len(words & title_words) / max(len(words), len(title_words))
            best = max(best, int(60 * overlap))
    for book in song.get("books") or []:
        number = str(book.get("number", "")).strip()
        if number and number in numbers:
            name = _norm(book.get("name", "")).split()
            # "EG 331", "FJ3 123": the book's initials (digits kept)
            initials = "".join(w[0] if not w.isdigit() else w for w in name)
            if not name or (words & set(name)) or any(w.startswith(initials) for w in words if initials):
                best = max(best, 90)
            else:
                best = max(best, 40)
    return best


def match_lines(lines: List[str], songs: dict) -> List[dict]:
    results = []
    for raw in lines:
        line = clean_line(raw)
        norm = _norm(line)
        if not norm:
            continue
        words = set(norm.split())
        numbers = {w for w in words if w.isdigit()}
        scored = sorted(
            ((_score(norm, words, numbers, song), song_id) for song_id, song in songs.items()),
            key=lambda pair: (-pair[0], songs[pair[1]]["title"].casefold()),
        )
        scored = [pair for pair in scored if pair[0] >= 30][:5]
        song_id = None
        # Only a clear winner is taken; anything else is left to the user.
        if scored and scored[0][0] >= 70 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
            song_id = scored[0][1]
        results.append({
            "line": line,
            "song_id": song_id,
            "title": songs[song_id]["title"] if song_id else None,
            "candidates": [{"song_id": sid, "title": songs[sid]["title"]} for _, sid in scored],
        })
    return results


# ---- HTTP ------------------------------------------------------------------------

def _fail(reason: str, status: int = 400, **extra):
    return JSONResponse({"ok": False, "reason": reason, **extra}, status)


@router.post("/songs/match")
async def songs_match(request: Request, body: dict = Body(...)):
    lines = body.get("lines")
    if not isinstance(lines, list) or len(lines) > MAX_SONGS or not all(isinstance(l, str) and len(l) <= 200 for l in lines):
        return _fail(f"lines must be a list of at most {MAX_SONGS} texts")
    songs = request.app.state.songs.songs
    return {"ok": True, "library_loaded": bool(songs), "matches": match_lines(lines, songs)}


@router.get("/program/templates")
async def list_templates(request: Request):
    return {"templates": await request.app.state.program_templates.list()}


@router.post("/program/templates")
async def save_template(request: Request, body: dict = Body(...)):
    slots = body.get("slots", [])
    if not isinstance(slots, list) or not all(isinstance(s, str) for s in slots):
        return _fail("slots must be a list of program row ids")
    items = await request.app.state.program_items.list()
    unknown = set(slots) - {item["id"] for item in items}
    if unknown:
        return _fail("unknown program item", 404)
    try:
        template = await request.app.state.program_templates.save(
            body.get("name"), items, set(slots), replace=body.get("replace") is True)
    except KeyError:
        return _fail("a template with this name already exists", 409)
    except (TemplateError, ProgramItemError) as exc:
        return _fail(str(exc))
    log.info("Program template saved: %s (%s rows, %s song slots)",
             template["name"], len(template["rows"]), template["slot_count"])
    return {"ok": True, "template": template}


@router.delete("/program/templates/{template_id}")
async def delete_template(template_id: str, request: Request):
    if not await request.app.state.program_templates.delete(template_id):
        return _fail("unknown template", 404)
    return {"ok": True}


@router.post("/program/templates/{template_id}/apply")
async def apply_template(template_id: str, request: Request, body: dict = Body(...)):
    from .routes import _program_reference_error  # routes imports most of the app

    template = await request.app.state.program_templates.get(template_id)
    if template is None:
        return _fail("unknown template", 404)
    songs = body.get("songs", [])
    if not isinstance(songs, list) or len(songs) > MAX_SONGS:
        return _fail(f"songs must be a list of at most {MAX_SONGS} entries")
    library = request.app.state.songs.songs
    cleaned = []
    for song in songs:
        if not isinstance(song, dict):
            return _fail("each song must be {song_id, text}")
        song_id, text = song.get("song_id"), song.get("text") or ""
        if not isinstance(text, str) or len(text) > 200:
            return _fail("song text must be at most 200 characters")
        if song_id is not None and (not isinstance(song_id, str) or song_id not in library):
            return _fail("unknown song", 404, song_id=song_id)
        if song_id or text.strip():
            cleaned.append({"song_id": song_id, "text": clean_line(text),
                            "title": library[song_id]["title"] if song_id else clean_line(text)})
    rows = fill_template(template["rows"], cleaned)
    if len(rows) > MAX_ROWS:
        return _fail(f"at most {MAX_ROWS} rows")
    if body.get("dry_run") is True:  # preview: the rows, nothing changed
        return {"ok": True, "rows": sorted(rows, key=lambda row: row["time"])}
    try:
        items = await request.app.state.program_items.replace_all(rows)
    except ProgramItemError as exc:
        return _fail(str(exc))
    # Keys or groups deleted since the template was saved: the program is
    # applied anyway (those rows refuse to start until fixed), but say so.
    warnings = []
    for item in items:
        reason = await _program_reference_error(item, request)
        if reason:
            warnings.append({"id": item["id"], "title": item["title"], "reason": reason})
    log.info("Program template applied: %s (%s rows, %s songs)", template["name"], len(items), len(cleaned))
    return {"ok": True, "items": items, "warnings": warnings}
