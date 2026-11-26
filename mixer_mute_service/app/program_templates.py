"""Program templates: a typical service saved once, applied with its songs.

A template is a copy of the program rows. Rows marked as song slots
("Liedplatz") get the songs of the day when the template is applied: the
songs fill the slots in order, songs beyond the last slot are added after it
as copies of that slot. A program that is already there keeps everything on its
rows (camera presets, light, desktop profiles ...): applying only changes the
person and the song of a row (apply_in_place); a program without rows is built
from the template. The Missionsbericht person "Mission Spotlight" shows the
Mission Spotlight row and hides the Missionsbericht, any other name the reverse
(switch_spotlight).

Songs usually arrive as a list of titles, one per line. POST /songs/match
finds each line in the song library (title, other titles or book number);
lines it cannot place get candidates and are left for the user to pick.

GET    /program/templates                     [{id, name, rows, slot_count}]
POST   /program/templates {name, slots, replace}
                                              save the current program; slots
                                              are the ids of its song-slot rows
DELETE /program/templates/{id}
POST   /program/templates/{id}/apply {songs: [{song_id, text}], persons, dry_run}
                                              dry_run: only return the rows;
                                              persons: [{id, person}] from the
                                              Ablauf PDF (app/program_pdf.py)
POST   /songs/match {lines: [...]}            [{line, song_id, title, preferred_book, candidates}]
"""

from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
import uuid
from copy import deepcopy
from difflib import SequenceMatcher
from pathlib import Path
from typing import List, Optional

from fastapi import APIRouter, Body, Request


from .actions import program_reference_error
from .jsonstore import read_json, write_json_atomic
from .responses import fail
from .program_items import (
    _ACTION_FIELDS, ProgramItemError, ProgramItemStore, keep_references, log_unlink_migration, unlink_shortcut,
)
from .program_pdf import PdfImportError, _key, assign_persons, is_spotlight_title, validated_persons
from .target_pcs import retarget_file_action

router = APIRouter()
log = logging.getLogger("mixer.program_templates")

MAX_TEMPLATES = 50
MAX_ROWS = 100
MAX_SONGS = 40
_ROW_FIELDS = (
    "time", "title", "person", "group", "playback", "translation", "lyrics",
    "playback_description", "translation_description", "lyrics_description", "song", "light",
    "cam1_preset", "cam2_preset", "camera_wait_seconds", "desktop", "cam1_scene", "cam2_scene", "beamer",
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
    def __init__(self, path: Path, shortcuts: Optional[dict] = None) -> None:
        """shortcuts: id -> shortcut, for rows saved with a reference to one;
        None when the shortcuts could not be read: references stay then."""
        self._path = path
        self._lock = asyncio.Lock()
        self._templates: List[dict] = []
        unlinked = []
        for entry in read_json(path, list, default=[]):
            try:
                rows = []
                for old in entry["rows"]:
                    slot = bool(old.get("song_slot"))
                    if shortcuts is None:
                        rows.append(keep_references(old, _template_row(old, slot)))
                        continue
                    row, status = unlink_shortcut(
                        f"Template row {entry['name']}", old, _template_row(old, slot), shortcuts,
                        lambda copied, slot=slot: _template_row(copied, slot))
                    rows.append(row)
                    if status:
                        unlinked.append(status)
                self._templates.append({"id": str(entry["id"]), "name": _clean_name(entry["name"]), "rows": rows})
            except (KeyError, TypeError, TemplateError, ProgramItemError) as exc:
                log.warning("Skipping invalid program template in %s: %s", path, exc)
        self.migration = None
        if unlinked:
            # As the program rows (app/program_items.py): once, then saved
            # without the references.
            self.migration = log_unlink_migration("Template row", unlinked)
            self._persist()

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

    async def retarget_files(self, moves: dict) -> int:
        """Point file actions on a PC whose address changed at the new one."""
        async with self._lock:
            changed = 0
            for template in self._templates:
                for row in template["rows"]:
                    for field in _ACTION_FIELDS:
                        new = retarget_file_action(row.get(field), moves)
                        if new != row.get(field):
                            row[field] = new
                            changed += 1
            if changed:
                self._persist()
            return changed

    async def rename(self, template_id: str, name) -> dict:
        """KeyError: unknown template; FileExistsError: name taken."""
        name = _clean_name(name)
        async with self._lock:
            template = next((t for t in self._templates if t["id"] == template_id), None)
            if template is None:
                raise KeyError(template_id)
            if any(t is not template and t["name"].casefold() == name.casefold() for t in self._templates):
                raise FileExistsError(name)
            template["name"] = name
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


def fill_template(rows: List[dict], songs: List[dict], trace: bool = False) -> List[dict]:
    """Program rows from template rows and the songs of the day.

    songs: [{"song_id": file name or None, "text": the line as typed,
    "title": the song's title}]. Songs
    fill the slots in order; slots left over stay without a song; songs left
    over are added right after the last slot as copies of it (or at the end
    with the last row's time if the template has no slot). A song that is not
    in the library keeps its line in the row title so nothing is lost.
    """
    result = [{k: v for k, v in row.items() if k != "song_slot"} for row in deepcopy(rows)]
    if trace:  # each row remembers the template row it came from (apply_in_place)
        for row, source in zip(result, rows):
            row["_tpl"] = deepcopy(source)
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
            if trace:
                model["_tpl"] = {**deepcopy(rows[slots[-1]]), "song": None}
        else:
            model = {field: "" for field in _ROW_FIELDS}
            model.update({"time": rows[-1]["time"] if rows else "10:00", "group": "",
                          "playback": None, "translation": None, "lyrics": None, "song": None,
                          "light": None})
            at = len(result)
        added = []
        for song in extra:
            row = dict(model)
            if slots:
                put(row, song)
            else:  # no slot to copy: a plain row named after the song
                row.update(title=(song.get("title") or song.get("text") or "Lied")[:120],
                           song=song.get("song_id"))
            if trace and "_tpl" not in row:
                row["_tpl"] = dict(row)
            added.append(row)
        result[at:at] = added
    return result


# ---- the template applied to the program that is already there ----------------

def _row_key(title: str) -> str:
    # Without the ": song text" a row gets when its song is not in the library.
    return _key(title.split(":")[0])


def _aligned(filled: List[dict], current: List[dict]) -> dict:
    """{index in filled: index in current}: the rows that are the same row, found
    by their titles in order (the earliest of equal titles first, so extra songs
    come last). Between two found rows, as many changed titles on both sides are
    paired by position (a renamed row keeps its settings); anything else is new
    or gone."""
    a = [_row_key(row["_tpl"]["title"]) for row in filled]
    b = [_row_key(row["title"]) for row in current]
    n, m = len(a), len(b)
    best = [[0] * (m + 1) for _ in range(n + 1)]
    for i in range(n - 1, -1, -1):
        for j in range(m - 1, -1, -1):
            best[i][j] = (best[i + 1][j + 1] + 1) if a[i] == b[j] else max(best[i + 1][j], best[i][j + 1])
    matches, i, j = [], 0, 0
    while i < n and j < m:
        if a[i] == b[j]:
            matches.append((i, j))
            i, j = i + 1, j + 1
        elif best[i + 1][j] >= best[i][j + 1]:
            i += 1
        else:
            j += 1
    pairs = dict(matches)
    previous = (-1, -1)
    for i, j in matches + [(n, m)]:
        if i - previous[0] == j - previous[1]:  # the gap has as many rows on both sides
            pairs.update(zip(range(previous[0] + 1, i), range(previous[1] + 1, j)))
        previous = (i, j)
    return pairs


def apply_in_place(filled: List[dict], current: List[dict]) -> List[dict]:
    """The rows of the program after the template was applied to the one that
    exists: a row that is already there keeps everything it has (time, camera
    presets, OBS scenes, wait time, desktop profiles, light, group, beamer,
    shortcut, hidden) and only takes the person the order names and, for a song
    slot, the song and its title. Rows the program does not have yet come from
    the template; rows the template does not have any more are dropped.

    filled: fill_template(..., trace=True) rows; current: ProgramItemStore rows
    ordered like the program.
    """
    pairs = _aligned(filled, current)
    result = []
    for index, row in enumerate(filled):
        source = row["_tpl"]
        row = {k: v for k, v in row.items() if k != "_tpl"}
        old = current[pairs[index]] if index in pairs else None
        if old is not None:
            merged = {field: old[field] for field in _ROW_FIELDS if field in old and field != "shortcuts"}
            merged["hidden"] = old.get("hidden") is True
            if row.get("_named"):  # the order names someone for this row
                merged["person"] = row["person"]
            if source.get("song_slot"):
                merged["song"], merged["title"] = row["song"], row["title"]
            if merged.get("song"):
                merged["lyrics"] = None  # a song or a Liedtext file, never both
            row = {**row, **merged}
        result.append(row)
    return result


# ---- Missionsbericht or Mission Spotlight ------------------------------------------

def _is_spotlight_person(person: str) -> bool:
    return _key(person) == "missionspotlight"


def _spotlight_row(row: dict, profiles: dict) -> bool:
    if is_spotlight_title(row["title"]):
        return True
    return any(getattr(profiles.get(p), "mission_spotlight", False) for p in row.get("desktop") or [])


def switch_spotlight(rows: List[dict], person: Optional[str], profiles: dict) -> Optional[str]:
    """Show the Mission Spotlight row and hide the Missionsbericht (person
    "Mission Spotlight") or the other way round (any other person). No person:
    nothing changes. Returns "spotlight", "normal" or None."""
    if not person:
        return None
    spotlight = _is_spotlight_person(person)
    for row in rows:
        if _spotlight_row(row, profiles):
            row["hidden"] = not spotlight
        elif row.get("_point") == "missionsbericht":
            row["hidden"] = spotlight
    return "spotlight" if spotlight else "normal"


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
            # A bare number ("101" from the Ablauf PDF) counts for any book.
            if (not name or words == numbers or (words & set(name))
                    or any(w.startswith(initials) for w in words if initials)):
                best = max(best, 90)
            else:
                best = max(best, 40)
    return best


# When one number exists in several songbooks and the line names no book, this
# one is taken (the church mostly sings from it). Matches the book's name or its
# initials: "GHS" = "Glauben Hoffen Singen".
PREFERRED_BOOK = "GHS"


def _is_preferred_book(name: str) -> bool:
    words = _norm(name).split()
    wanted = _norm(PREFERRED_BOOK).replace(" ", "")
    return bool(words) and ("".join(words) == wanted or "".join(w[0] for w in words) == wanted)


def _preferred_choice(top: List[str], numbers: set, songs: dict) -> Optional[str]:
    """The one tied song that has a number of the line in the preferred book, else None."""
    if not numbers:
        return None
    found = [sid for sid in top if any(
        _is_preferred_book(book.get("name", "")) and str(book.get("number", "")).strip() in numbers
        for book in songs[sid].get("books") or [])]
    return found[0] if len(found) == 1 else None


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
        preferred = False
        # Only a clear winner is taken; anything else is left to the user ...
        if scored and scored[0][0] >= 70 and (len(scored) == 1 or scored[1][0] < scored[0][0]):
            song_id = scored[0][1]
        elif scored and scored[0][0] >= 70:
            # ... except a tie between songbooks, where the preferred one wins if it is the only one.
            top = [sid for score, sid in scored if score == scored[0][0]]
            song_id = _preferred_choice(top, numbers, songs)
            preferred = song_id is not None
        results.append({
            "line": line,
            "song_id": song_id,
            "title": songs[song_id]["title"] if song_id else None,
            "preferred_book": PREFERRED_BOOK if preferred else None,
            "candidates": [{"song_id": sid, "title": songs[sid]["title"]} for _, sid in scored],
        })
    return results


# ---- HTTP ------------------------------------------------------------------------

@router.post("/songs/match")
async def songs_match(request: Request, body: dict = Body(...)):
    lines = body.get("lines")
    if not isinstance(lines, list) or len(lines) > MAX_SONGS or not all(isinstance(l, str) and len(l) <= 200 for l in lines):
        return fail(f"lines must be a list of at most {MAX_SONGS} texts")
    songs = request.app.state.songs.songs
    return {"ok": True, "library_loaded": bool(songs), "matches": match_lines(lines, songs)}


@router.get("/program/templates")
async def list_templates(request: Request):
    return {"templates": await request.app.state.program_templates.list()}


@router.post("/program/templates")
async def save_template(request: Request, body: dict = Body(...)):
    slots = body.get("slots", [])
    if not isinstance(slots, list) or not all(isinstance(s, str) for s in slots):
        return fail("slots must be a list of program row ids")
    items = await request.app.state.program_items.list()
    unknown = set(slots) - {item["id"] for item in items}
    if unknown:
        return fail("unknown program item", 404)
    try:
        template = await request.app.state.program_templates.save(
            body.get("name"), items, set(slots), replace=body.get("replace") is True)
    except KeyError:
        return fail("a template with this name already exists", 409)
    except (TemplateError, ProgramItemError) as exc:
        return fail(str(exc))
    log.info("Program template saved: %s (%s rows, %s song slots)",
             template["name"], len(template["rows"]), template["slot_count"])
    return {"ok": True, "template": template}


@router.delete("/program/templates/{template_id}")
async def delete_template(template_id: str, request: Request):
    if not await request.app.state.program_templates.delete(template_id):
        return fail("unknown template", 404)
    return {"ok": True}


@router.post("/program/templates/{template_id}/apply")
async def apply_template(template_id: str, request: Request, body: dict = Body(...)):
    template = await request.app.state.program_templates.get(template_id)
    if template is None:
        return fail("unknown template", 404)
    songs = body.get("songs", [])
    if not isinstance(songs, list) or len(songs) > MAX_SONGS:
        return fail(f"songs must be a list of at most {MAX_SONGS} entries")
    library = request.app.state.songs.songs
    cleaned = []
    for song in songs:
        if not isinstance(song, dict):
            return fail("each song must be {song_id, text}")
        song_id, text = song.get("song_id"), song.get("text") or ""
        if not isinstance(text, str) or len(text) > 200:
            return fail("song text must be at most 200 characters")
        if song_id is not None and (not isinstance(song_id, str) or song_id not in library):
            return fail("unknown song", 404, song_id=song_id)
        if song_id or text.strip():
            cleaned.append({"song_id": song_id, "text": clean_line(text),
                            "title": library[song_id]["title"] if song_id else clean_line(text)})
    try:
        persons = validated_persons(body.get("persons") or [])
    except PdfImportError as exc:
        return fail(str(exc))
    # "Mission Spotlight" as the Missionsbericht's person is not a name for the row:
    # it switches which of the two rows is shown (switch_spotlight below).
    mission = next((p["person"] for p in persons if p["id"] == "missionsbericht"), "")
    if _is_spotlight_person(mission):
        persons = [{**p, "person": ""} if p["id"] == "missionsbericht" else p for p in persons]
    template_rows, unassigned = assign_persons(template["rows"], persons)
    rows = fill_template(template_rows, cleaned, trace=True)
    current = await request.app.state.program_items.list()
    if current:  # the program is kept as it is, only person and song change
        rows = apply_in_place(rows, current)
    else:
        rows = [{k: v for k, v in row.items() if k != "_tpl"} for row in rows]
    spotlight = switch_spotlight(rows, mission, request.app.state.cfg.desktop_display.profiles)
    for row in rows:
        row.pop("_point", None)
        row.pop("_named", None)
    if len(rows) > MAX_ROWS:
        return fail(f"at most {MAX_ROWS} rows")
    if body.get("dry_run") is True:  # preview: the rows, nothing changed
        return {"ok": True, "rows": sorted(rows, key=lambda row: row["time"]), "unassigned": unassigned,
                "mission": spotlight}
    try:
        items = await request.app.state.program_items.replace_all(rows, preserve_hidden=True)
    except ProgramItemError as exc:
        return fail(str(exc))
    # Keys or groups deleted since the template was saved: the program is
    # applied anyway (those rows refuse to start until fixed), but say so.
    warnings = []
    for item in items:
        reason = await program_reference_error(request, item)
        if reason:
            warnings.append({"id": item["id"], "title": item["title"], "reason": reason})
    log.info("Program template applied: %s (%s rows, %s songs, %s persons)",
             template["name"], len(items), len(cleaned), sum(bool(p["person"]) for p in persons) - len(unassigned))
    return {"ok": True, "items": items, "warnings": warnings, "unassigned": unassigned, "mission": spotlight}
