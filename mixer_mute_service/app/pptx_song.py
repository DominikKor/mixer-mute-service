"""Read songs saved as PowerPoint files (.pptx) in the song library.

The display agent sends only the XML parts of a .pptx (the presentation,
its relations and the slides; desktop_agent/show_image_agent.py). Each
slide of the presentation, in its order, becomes one slide of the song with
the slide's text; hidden slides and slides without text are left out. The
title is the file name. Layout (fonts, colours, pictures) comes from the
shared song display settings, as for FoilPresenter songs.
"""

from __future__ import annotations

import posixpath
import re
import xml.etree.ElementTree as ET
from typing import List

from .foil import FoilError

_NS = {
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "p": "http://schemas.openxmlformats.org/presentationml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
}
_SLIDE_RE = re.compile(r"^ppt/slides/slide(\d+)\.xml$")


def _xml(text: str):
    # Entity definitions are never needed and could blow up.
    if "<!ENTITY" in text:
        raise FoilError("entities not allowed")
    try:
        return ET.fromstring(text.encode("utf-8"))
    except ET.ParseError as exc:
        raise FoilError(f"not valid XML: {exc}") from exc


def _slide_order(parts: dict) -> List[str]:
    """Slide part names in the order of the presentation (file numbers as a fallback)."""
    slides = [name for name in parts if _SLIDE_RE.match(name)]
    presentation, rels = parts.get("ppt/presentation.xml"), parts.get("ppt/_rels/presentation.xml.rels")
    if isinstance(presentation, str) and isinstance(rels, str):
        targets = {
            rel.get("Id"): posixpath.normpath(posixpath.join("ppt", rel.get("Target", "")))
            for rel in _xml(rels).findall("rel:Relationship", _NS)
        }
        ordered = [targets.get(slide.get(f"{{{_NS['r']}}}id"))
                   for slide in _xml(presentation).findall("p:sldIdLst/p:sldId", _NS)]
        ordered = [name for name in ordered if name in slides]
        if ordered:
            return ordered
    return sorted(slides, key=lambda name: int(_SLIDE_RE.match(name).group(1)))


def _slide_text(root) -> str:
    """The slide's text, a line per paragraph (line breaks kept), shapes in document order."""
    lines = []
    for paragraph in root.iter(f"{{{_NS['a']}}}p"):
        line = ""
        for node in paragraph:
            if node.tag in (f"{{{_NS['a']}}}r", f"{{{_NS['a']}}}fld"):
                line += "".join(t.text or "" for t in node.findall("a:t", _NS))
            elif node.tag == f"{{{_NS['a']}}}br":
                line += "\n"
        lines.append(line.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def parse_pptx(name: str, parts: dict) -> dict:
    """{title, titles, books, copyright, slides: [{key, text}]} like parse_foil, or FoilError."""
    if not isinstance(parts, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in parts.items()):
        raise FoilError("invalid PowerPoint parts")
    slides = []
    for number, part in enumerate(_slide_order(parts), start=1):
        root = _xml(parts[part])
        if root.get("show") in ("0", "false"):
            continue
        text = _slide_text(root)
        if text:
            slides.append({"key": str(number), "text": text})
    if not slides:
        raise FoilError("presentation has no text")
    title = re.sub(r"\.pptx$", "", name, flags=re.IGNORECASE).strip()
    return {"title": title, "titles": [title], "books": [], "copyright": "", "slides": slides}
