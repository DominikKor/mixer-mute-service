"""Read FoilPresenter song files (.foil).

A .foil file is XML: a title (sometimes several), the verses (<strophe>, each
with a key such as "1", "2" or "Ref" and its text), one order (<reihenfolge>)
whose numbers point at the verses in file order (verses may repeat, e.g. the
refrain), a copyright text and hymnbook entries. One verse in the order is
one slide.

Only what the song display needs is kept; layout details (fonts, sizes,
colours) come from the shared song display settings instead.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import List

MAX_FILE_BYTES = 512 * 1024


class FoilError(ValueError):
    pass


def _text(element) -> str:
    return (element.text or "").replace("\r\n", "\n").replace("\r", "\n").strip() if element is not None else ""


def parse_foil(data: bytes) -> dict:
    """{title, titles, books, copyright, slides: [{key, text}]} or FoilError."""
    if len(data) > MAX_FILE_BYTES:
        raise FoilError("file too large")
    # Old files name an external DTD (never loaded); entity definitions are
    # never used by the files and could blow up, so they are refused.
    if b"<!ENTITY" in data:
        raise FoilError("entities not allowed")
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise FoilError(f"not valid XML: {exc}") from exc
    if root.tag != "foilpresenterfolie":
        raise FoilError("not a FoilPresenter song")

    titles = [_text(t) for t in root.findall("titel/titelstring") if _text(t)]
    verses = [
        {"key": _text(s.find("key")), "text": _text(s.find("text"))}
        for s in root.findall("strophen/strophe")
    ]
    order: List[int] = []
    for number in root.findall("reihenfolge/strophennummer"):
        try:
            index = int(_text(number))
        except ValueError:
            continue
        if 0 <= index < len(verses):
            order.append(index)
    if not order:
        order = list(range(len(verses)))
    slides = [dict(verses[i]) for i in order if verses[i]["text"]]
    if not slides:
        raise FoilError("song has no text")

    books = []
    for entry in root.findall("buch/bucheintrag"):
        name, number = _text(entry.find("name")), _text(entry.find("nummer"))
        if name or number:
            books.append({"name": name, "number": number})

    return {
        "title": titles[0] if titles else "",
        "titles": titles,
        "books": books,
        "copyright": _text(root.find("copyright/text")),
        "slides": slides,
    }


# ---- writing a new song ------------------------------------------------------

_UMLAUTS = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "Ae", "Ö": "Oe", "Ü": "Ue", "ß": "ss"})


def song_file_name(title: str) -> str:
    """A Windows-safe file name like FoilPresenter's own: umlauts spelled out,
    other punctuation as "_" ("Gott, du bist da" -> "Gott_ du bist da.foil")."""
    name = title.translate(_UMLAUTS)
    name = re.sub(r"[^A-Za-z0-9 ._()!'+-]", "_", name)
    name = re.sub(r"\s+", " ", name).strip(" .")[:120].strip(" .")
    if not name:
        raise FoilError("title gives no usable file name")
    return name + ".foil"


def _sub(parent, tag: str, text: str = "") -> ET.Element:
    element = ET.SubElement(parent, tag)
    element.text = text
    return element


def build_foil(title: str, verses: List[dict], order: List[int], copyright: str = "") -> bytes:
    """A .foil file (FoilPresenter's format) for a new song.

    verses: [{"key": "1" / "Ref", "text": ...}] in file order; order: indices
    into verses, one per slide (the refrain may repeat).
    """
    if not title.strip():
        raise FoilError("title required")
    if not verses or not all(v.get("text", "").strip() for v in verses):
        raise FoilError("every slide needs text")
    if not order or not all(isinstance(i, int) and 0 <= i < len(verses) for i in order):
        raise FoilError("order must name existing slides")
    now = datetime.now()
    stamp = f"{now.year}.{now.month}.{now.day}.{now.hour}.{now.minute}.{now.second}"
    root = ET.Element("foilpresenterfolie", version="00300.000105")
    _sub(root, "id", f"{stamp}.{now.microsecond // 1000}")
    _sub(root, "lastchanged", stamp)
    titel = _sub(root, "titel")
    _sub(titel, "titelstring", title.strip())
    for tag, value in (("sprache", "0"), ("ccliid", ""), ("tonart", ""), ("valign", "0"), ("notiz", ""), ("versionsinfo", "")):
        _sub(root, tag, value)
    farben = _sub(root, "farben")
    _sub(farben, "cback", "0,0,0")
    _sub(farben, "ctext", "0,0,0")
    reihenfolge = _sub(root, "reihenfolge")
    _sub(reihenfolge, "name", "Standard")
    for index in order:
        _sub(reihenfolge, "strophennummer", str(index))
    strophen = _sub(root, "strophen")
    for number, verse in enumerate(verses, start=1):
        strophe = _sub(strophen, "strophe")
        for tag, value in (("align", "0"), ("font", "Verdana"), ("textsize", "14"),
                           ("bold", "0"), ("italic", "0"), ("underline", "0")):
            _sub(strophe, tag, value)
        _sub(strophe, "key", verse["key"])
        _sub(strophe, "text", verse["text"].replace("\r\n", "\n").strip())
        _sub(strophe, "sortnr", str(number))
    _sub(root, "verkn")
    block = _sub(root, "copyright")
    for tag, value in (("font", "Verdana"), ("textsize", "7"), ("anzeigedauer", "5"),
                       ("bold", "0"), ("italic", "0"), ("underline", "0")):
        _sub(block, tag, value)
    _sub(block, "text", copyright.strip())
    _sub(root, "buch")
    _sub(root, "kategorien")
    ET.indent(root, space="\t")
    xml = '<?xml version="1.0" encoding="UTF-8"?>\n' + ET.tostring(root, encoding="unicode", short_empty_elements=False) + "\n"
    return xml.replace("\n", "\r\n").encode("utf-8")
