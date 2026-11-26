"""Import of the filled "Gottesdienstablauf" PDF (AcroForm text fields).

The PDF names who does what (Begrüßung, Textlesung, Predigt, ...) and the
songs (Lied1..Lied6, usually hymn book numbers). Importing it does not touch
the program yet: the page puts the songs into the template dialog and sends
the persons along when a template is applied; assign_persons() then finds
the matching template row for each point by its title, in order.

The last imported PDF is kept (program_pdf.json), so another program (e.g.
the planner that fills the PDF) can send it and the iPad picks it up later.

POST /program/pdf?filename=...   body: the PDF (application/pdf)
                                 -> {ablauf: {filename, date, songs, points, imported_at}}
GET  /program/pdf                -> {ablauf: the last import or null}
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, Request

from .jsonstore import read_json, write_json_atomic
from .responses import fail

router = APIRouter()
log = logging.getLogger("mixer.program_pdf")

MAX_PDF_BYTES = 5 * 1024 * 1024
MAX_PERSON = 80

# (id, title, PDF field names (normalized), keywords for the template row title).
# The first keyword is the strong one; the order is the order of the service.
_SONG = "song"
ABLAUF: Tuple[Tuple[str, str, Tuple[str, ...], Tuple[str, ...]], ...] = (
    ("begruessung1", "Begrüßung", ("begruessung1stunde", "begruessung1"), ("begruessung", "willkommen")),
    ("textlesung", "Textlesung", ("textlesung",), ("lesung",)),
    ("lied1", "Lied", ("lied1",), (_SONG,)),
    ("gebet_knie", "Gebet im Knien", ("gebetknie",), ("knie", "gebet")),
    ("missionsbericht", "Missionsbericht", ("missionsbericht",), ("missionsbericht", "bericht", "mission")),
    ("missionsgaben", "Sammlung der Missionsgaben", ("missionsgaben",),
     ("missionsgaben", "sammlung", "gaben", "kollekte")),
    ("dankgebet", "Dankgebet für die Missionsgaben", ("dankgebetmission",), ("dankgebet", "gebet")),
    ("lied2", "Lied", ("lied2",), (_SONG,)),
    ("bibelgespraech", "Bibelgespräch",
     ("bibelgespraech1", "bibelgespraech2", "bibelgespraechjugend", "kindersabbatschule"),
     ("bibelgespraech", "gespraech", "bibel", "sabbatschule")),
    ("lied3", "Lied", ("lied3",), (_SONG,)),
    ("gebet_ende1", "Gebet eines Gesprächsleiters", ("gebetende1stunde", "gebetende1"), ("gebet",)),
    ("begruessung2", "Begrüßung zur Predigtstunde", ("begruessung2stunde", "begruessung2"),
     ("begruessung", "willkommen")),
    ("bekanntmachungen", "Bekanntmachungen", ("bekanntmachungen", "bekannmachungen"),
     ("bekanntmachung", "bekannmachung", "abkuendigung", "ankuendigung", "info")),
    ("gemeindegaben", "Sammlung der Gemeindegaben", ("gemeindegaben",),
     ("gemeindegaben", "sammlung", "gaben", "kollekte")),
    ("stilles_gebet", "Stilles Gebet", ("stillesgebet",), ("still", "gebet")),
    ("lied4", "Lied", ("lied4",), (_SONG,)),
    ("gebet_predigt", "Gebet für die Predigt", ("gebetpredigtanfang",), ("gebet",)),
    ("musik1", "Musikbeitrag 1", ("musikbeitrag1",), ("musikbeitrag", "musik")),
    ("predigt", "Predigt", ("predigt",), ("predigt",)),
    ("musik2", "Musikbeitrag 2", ("musikbeitrag2",), ("musikbeitrag", "musik")),
    ("lied5", "Lied", ("lied5",), (_SONG,)),
    ("gebet_abschluss", "Gebet des Predigers", ("gebetpredigtende",), ("gebet",)),
    ("lied6", "Lied", ("lied6",), (_SONG,)),
    ("segen", "Segenswort", ("segenswort",), ("segen",)),
    ("verabschiedung", "Verabschiedung", ("verabschiedung",), ("verabschiedung", "abschied")),
    ("klaviermusik", "Klaviermusik zum Ausklang", ("klaviermusik",), ("klavier", "ausklang", "nachspiel")),
)
POINT_IDS = {point[0] for point in ABLAUF if point[3] != (_SONG,)}
# Extra people in the Bibelgespräch: which group they lead.
_GROUP_LABELS = {"bibelgespraechjugend": "Jugend", "kindersabbatschule": "Kinder"}
_DATE_RE = re.compile(r"(20\d\d)-?(\d\d)-?(\d\d)")


class PdfImportError(ValueError):
    pass


def _key(text: str) -> str:
    """Lower case, umlauts spelled out, only letters and digits."""
    text = text.casefold().replace("ä", "ae").replace("ö", "oe").replace("ü", "ue").replace("ß", "ss")
    text = unicodedata.normalize("NFKD", text)
    return "".join(c for c in text if c.isascii() and c.isalnum())


def _clean(value) -> str:
    return " ".join(str(value or "").split())


def read_form_fields(data: bytes) -> Dict[str, str]:
    """The filled text fields of a PDF form: {normalized name: value}."""
    if not data.startswith(b"%PDF"):
        raise PdfImportError("not a PDF file")
    if len(data) > MAX_PDF_BYTES:
        raise PdfImportError(f"PDF larger than {MAX_PDF_BYTES // (1024 * 1024)} MB")
    from pypdf import PdfReader  # only needed here; keeps the service start light

    try:
        fields = PdfReader(io.BytesIO(data)).get_fields() or {}
    except Exception as exc:  # noqa: BLE001 - any broken PDF is just "unreadable"
        raise PdfImportError(f"PDF unreadable: {type(exc).__name__}") from exc
    result = {}
    for name, field in fields.items():
        value = field.get("/V") if hasattr(field, "get") else None
        if isinstance(value, (str, bytes)) or value is None:
            text = _clean(value.decode("latin-1") if isinstance(value, bytes) else value)
            key = _key(str(name))
            if text or key not in result:
                result[key] = text
    return result


def parse_ablauf(fields: Dict[str, str]) -> dict:
    """Songs and persons of the service from the form fields."""
    known = {name for point in ABLAUF for name in point[2]}
    if not known & set(fields):
        raise PdfImportError("no fields of the Gottesdienstablauf form found")
    songs, points = [], []
    for point_id, title, names, keywords in ABLAUF:
        values = []
        for name in names:
            value = fields.get(name, "")
            if value and name in _GROUP_LABELS:
                value = f"{value} ({_GROUP_LABELS[name]})"
            if value and value not in values:
                values.append(value)
        if keywords == (_SONG,):
            if values:
                songs.append(values[0][:200])
        else:
            points.append({"id": point_id, "title": title, "person": ", ".join(values)[:MAX_PERSON]})
    return {"songs": songs, "points": points}


def _row_score(point_id: str, title: str) -> int:
    keywords = next(point[3] for point in ABLAUF if point[0] == point_id)
    title = _key(title)
    if keywords[0] in title:
        return 10
    return 5 if any(k in title for k in keywords[1:]) else 0


def assign_persons(rows: List[dict], persons: List[dict]) -> Tuple[List[dict], List[dict]]:
    """Put the persons of the PDF into the template rows.

    persons: [{"id": point id, "person": name}] in the order of the service.
    Each point goes to at most one row and the order is kept (best overall
    match of the titles), so a template without e.g. "Gebet im Knien" does
    not shift everything after it. Song slots are left out. Returns the rows
    (copies) and the points with a name that found no row.
    """
    candidates = [i for i, row in enumerate(rows) if not row.get("song_slot")]
    n, m = len(persons), len(candidates)
    score = [[_row_score(p["id"], rows[i]["title"]) for i in candidates] for p in persons]
    best = [[0] * (m + 1) for _ in range(n + 1)]
    for a in range(n - 1, -1, -1):
        for b in range(m - 1, -1, -1):
            take = score[a][b] + best[a + 1][b + 1] if score[a][b] else 0
            best[a][b] = max(take, best[a + 1][b], best[a][b + 1])
    result = [dict(row) for row in rows]
    unassigned = []
    a = b = 0
    while a < n:
        if b < m and score[a][b] and best[a][b] == score[a][b] + best[a + 1][b + 1]:
            if persons[a]["person"]:
                result[candidates[b]]["person"] = persons[a]["person"]
            a, b = a + 1, b + 1
        elif b < m and best[a][b] == best[a][b + 1]:
            b += 1
        else:
            if persons[a]["person"]:
                unassigned.append(persons[a])
            a += 1
    return result, unassigned


def validated_persons(value) -> List[dict]:
    if not isinstance(value, list) or len(value) > len(POINT_IDS):
        raise PdfImportError("persons must be a list of {id, person}")
    result = []
    for entry in value:
        if (not isinstance(entry, dict) or entry.get("id") not in POINT_IDS
                or not isinstance(entry.get("person", ""), str)):
            raise PdfImportError("persons must be a list of {id, person}")
        person = _clean(entry.get("person"))
        if len(person) > MAX_PERSON:
            raise PdfImportError(f"person must be at most {MAX_PERSON} characters")
        title = next(p[1] for p in ABLAUF if p[0] == entry["id"])
        result.append({"id": entry["id"], "title": title, "person": person})
    order = [p[0] for p in ABLAUF]
    return sorted(result, key=lambda p: order.index(p["id"]))


def _date_from(filename: str) -> Optional[str]:
    match = _DATE_RE.search(filename)
    if not match:
        return None
    try:
        return datetime(*map(int, match.groups())).date().isoformat()
    except ValueError:
        return None


class ProgramPdfStore:
    """The last imported Ablauf, kept across restarts."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        last = read_json(path, dict, default={})
        self._last = last if last.get("points") else None

    async def last(self) -> Optional[dict]:
        async with self._lock:
            return dict(self._last) if self._last else None

    async def save(self, ablauf: dict) -> None:
        async with self._lock:
            self._last = dict(ablauf)
            write_json_atomic(self._path, self._last, ensure_ascii=False)


# ---- HTTP ------------------------------------------------------------------------

@router.post("/program/pdf")
async def import_pdf(request: Request, filename: str = ""):
    store = request.app.state.program_pdf
    if store is None:
        return fail("PDF import unavailable", 503)
    length = request.headers.get("content-length", "")
    if length.isdigit() and int(length) > MAX_PDF_BYTES:
        return fail(f"PDF larger than {MAX_PDF_BYTES // (1024 * 1024)} MB", 413)
    data = bytearray()
    async for chunk in request.stream():
        data += chunk
        if len(data) > MAX_PDF_BYTES:
            return fail(f"PDF larger than {MAX_PDF_BYTES // (1024 * 1024)} MB", 413)
    filename = _clean(Path(filename.replace("\\", "/")).name)[:120]
    try:
        fields = await asyncio.to_thread(read_form_fields, bytes(data))
        ablauf = parse_ablauf(fields)
    except PdfImportError as exc:
        return fail(str(exc))
    ablauf.update(filename=filename, date=_date_from(filename),
                  imported_at=datetime.now().astimezone().isoformat(timespec="seconds"))
    await store.save(ablauf)
    log.info("Gottesdienstablauf imported: %s (%s songs, %s persons)", filename or "(no name)",
             len(ablauf["songs"]), sum(bool(p["person"]) for p in ablauf["points"]))
    return {"ok": True, "ablauf": ablauf}


@router.get("/program/pdf")
async def last_pdf(request: Request):
    store = request.app.state.program_pdf
    return {"ablauf": await store.last() if store is not None else None}
