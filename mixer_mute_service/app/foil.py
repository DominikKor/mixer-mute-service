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

import xml.etree.ElementTree as ET
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
