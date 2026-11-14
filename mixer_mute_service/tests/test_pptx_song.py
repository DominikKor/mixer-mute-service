"""PowerPoint songs (.pptx) in the song library: the agent sends the slides'
XML (desktop_agent/show_image_agent.py), the service makes one song slide per
presentation slide (app/pptx_song.py). Files are built here; no device is contacted."""

import tempfile
import unittest
import zipfile
from pathlib import Path

from app.foil import FoilError
from app.pptx_song import parse_pptx
from app.songs import SongService
from tests.helpers import require_tk

tkinter = require_tk()
if tkinter is not None:
    from desktop_agent import show_image_agent as agent

A = "http://schemas.openxmlformats.org/drawingml/2006/main"
P = "http://schemas.openxmlformats.org/presentationml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def slide(*shapes, hidden=False):
    body = "".join(
        f'<p:sp><p:txBody>{"".join(paragraph(p) for p in paragraphs)}</p:txBody></p:sp>' for paragraphs in shapes)
    show = ' show="0"' if hidden else ""
    return f'<p:sld xmlns:a="{A}" xmlns:p="{P}" xmlns:r="{R}"{show}><p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>'


def paragraph(text):
    runs = '<a:br/>'.join(f"<a:r><a:t>{part}</a:t></a:r>" for part in text.split("\n"))
    return f"<a:p>{runs}</a:p>"


def parts(order=("rId3", "rId2", "rId4")):
    ids = "".join(f'<p:sldId id="{256 + i}" r:id="{rid}"/>' for i, rid in enumerate(order))
    return {
        "ppt/presentation.xml": f'<p:presentation xmlns:p="{P}" xmlns:r="{R}"><p:sldIdLst>{ids}</p:sldIdLst></p:presentation>',
        "ppt/_rels/presentation.xml.rels": (
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId2" Target="slides/slide1.xml"/><Relationship Id="rId3" Target="slides/slide2.xml"/>'
            '<Relationship Id="rId4" Target="slides/slide3.xml"/></Relationships>'),
        "ppt/slides/slide1.xml": slide(["Strophe 1", "Zeile zwei\nmit Umbruch"]),
        "ppt/slides/slide2.xml": slide(["Gott, du bist da"], ["Text und Melodie: X"]),
        "ppt/slides/slide3.xml": slide(["Versteckt"], hidden=True),
    }


class ParseTests(unittest.TestCase):
    def test_one_song_slide_per_slide_in_presentation_order(self):
        song = parse_pptx("Gott du bist da.PPTX", parts())
        self.assertEqual(song["title"], "Gott du bist da")
        self.assertEqual([s["text"] for s in song["slides"]],
                         ["Gott, du bist da\nText und Melodie: X", "Strophe 1\nZeile zwei\nmit Umbruch"])
        self.assertEqual([s["key"] for s in song["slides"]], ["1", "2"], "the hidden third slide is left out")

    def test_without_presentation_order_the_file_numbers_count(self):
        data = parts()
        del data["ppt/presentation.xml"]
        self.assertEqual(parse_pptx("x.pptx", data)["slides"][0]["text"], "Strophe 1\nZeile zwei\nmit Umbruch")

    def test_bad_files(self):
        with self.assertRaises(FoilError):
            parse_pptx("leer.pptx", {"ppt/slides/slide1.xml": slide([""])})
        with self.assertRaises(FoilError):
            parse_pptx("x.pptx", {"ppt/slides/slide1.xml": '<!DOCTYPE x [<!ENTITY a "b">]><x/>'})
        with self.assertRaises(FoilError):
            parse_pptx("x.pptx", "kaputt")

    def test_library_takes_pptx_next_to_foil(self):
        songs, errors = SongService.parse_library([
            {"name": "Lied.pptx", "pptx_parts": parts()},
            {"name": "Leer.pptx", "pptx_parts": {}},
        ])
        self.assertEqual(list(songs), ["Lied.pptx"])
        self.assertEqual(errors[0]["file"], "Leer.pptx")


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class AgentTests(unittest.TestCase):
    def test_reads_only_the_slide_xml(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            with zipfile.ZipFile(folder / "Lied.pptx", "w") as archive:
                for name, text in parts().items():
                    archive.writestr(name, text)
                archive.writestr("ppt/media/image1.png", b"\x89PNG" + b"0" * 1000)
                archive.writestr("ppt/slides/_rels/slide1.xml.rels", "<x/>")
            (folder / "~$Lied.pptx").write_bytes(b"lock")
            (folder / "kaputt.pptx").write_bytes(b"not a zip")
            files, _ = agent._read_song_library(directory)
            self.assertEqual([f["name"] for f in files], ["Lied.pptx"])
            self.assertEqual(set(files[0]["pptx_parts"]), set(parts()))
            self.assertEqual(len(parse_pptx("Lied.pptx", files[0]["pptx_parts"])["slides"]), 2)

    def test_too_big_unpacked_is_skipped(self):
        with tempfile.TemporaryDirectory() as directory:
            with zipfile.ZipFile(Path(directory) / "Gross.pptx", "w", zipfile.ZIP_DEFLATED) as archive:
                archive.writestr("ppt/slides/slide1.xml", "x" * (agent.MAX_PPTX_PART_BYTES + 1))
            self.assertEqual(agent._read_song_library(directory)[0], [])


if __name__ == "__main__":
    unittest.main()
