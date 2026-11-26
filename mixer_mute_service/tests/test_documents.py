"""Documents (PDF, PowerPoint, Word) shown page by page, full screen, by the
display agent instead of their default app (desktop_agent/documents.py,
app/documents.py).

Office, LibreOffice, pip and the screen are faked; the PDF rendering itself
runs with pypdfium2 where it is installed (skipped otherwise). The agents are
faked through app.display_agent._post."""

import queue
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

from desktop_agent import documents
from tests.helpers import app_client, require_tk

tkinter = require_tk()
if tkinter is not None:
    from desktop_agent import show_image_agent as agent

BEAMER_PC, STREAM_PC = "192.168.188.11", "192.168.188.20"
ENV = {"DESKTOP_AGENT_TOKEN": "test-token"}
PROFILES = (
    "desktop_display:\n  enabled: true\n  profiles:\n"
    f"    predigt:\n      title: Predigt\n      kind: file\n      path: 'C:\\Predigt\\Übersetzung*.pptx'\n"
    f"      target_ip: {BEAMER_PC}\n      monitor: 3\n"
)
DOC_A = f"file:{BEAMER_PC}|C:\\Predigt\\Übersetzung.pptx"
DOC_B = f"file:{BEAMER_PC}|C:\\Predigt\\Lesung.pdf"


def row(title, **extra):
    return {"time": "10:00", "title": title, "person": "", "group": "", **extra}


def two_page_pdf(folder: Path) -> Path:
    path = folder / "zwei.pdf"
    Image.new("RGB", (800, 600), "red").save(path, save_all=True,
                                             append_images=[Image.new("RGB", (600, 800), "blue")])
    return path


class Finished:
    def __init__(self, returncode=0, stderr=b""):
        self.returncode, self.stderr = returncode, stderr


class ConversionTests(unittest.TestCase):
    def test_document_types(self):
        self.assertEqual(documents.document_type("C:\\a\\Predigt.PDF"), "pdf")
        for name in ("x.pptx", "x.ppt", "x.ppsx", "x.pps", "x.odp"):
            self.assertEqual(documents.document_type(name), "powerpoint", name)
        for name in ("x.docx", "x.doc", "x.odt", "x.rtf"):
            self.assertEqual(documents.document_type(name), "word", name)
        for name in ("x.mp4", "x.png", "x.txt", "x"):
            self.assertIsNone(documents.document_type(name), name)

    def test_the_service_knows_the_same_suffixes(self):
        from app import documents as service

        agent_suffixes = documents.PDF_SUFFIXES | documents.POWERPOINT_SUFFIXES | documents.WORD_SUFFIXES
        self.assertEqual(service.DOCUMENT_SUFFIXES, agent_suffixes)

    def test_a_pdf_is_shown_as_it_is(self):
        def never(*_args):
            raise AssertionError("a PDF is never converted")

        source = Path("C:/x/Lesung.pdf")
        self.assertEqual(documents.pdf_for(source, Path("unused"), office=never, libreoffice=never), source)

    def test_office_converts_once_then_the_cache(self):
        calls = []

        def office(kind, source, target):
            calls.append(kind)
            target.write_bytes(b"%PDF-1.4")

        with tempfile.TemporaryDirectory() as name:
            folder = Path(name)
            source = folder / "Übersetzung.pptx"
            source.write_bytes(b"pptx")
            cache = folder / "cache"
            first = documents.pdf_for(source, cache, office=office, find_soffice=lambda: None)
            second = documents.pdf_for(source, cache, office=office, find_soffice=lambda: None)
            self.assertEqual((first, calls), (second, ["powerpoint"]), "the converted copy is reused")
            self.assertEqual(first.read_bytes(), b"%PDF-1.4")
            self.assertEqual([p.name for p in cache.iterdir()], [first.name], "no partial file left")
            source.write_bytes(b"pptx, changed")
            documents.pdf_for(source, cache, office=office, find_soffice=lambda: None)
            self.assertEqual(calls, ["powerpoint", "powerpoint"], "a changed file is converted again")

    def test_libreoffice_when_office_cannot(self):
        def office(_kind, _source, _target):
            raise documents.DocumentError("no Office")

        def libreoffice(soffice, source, target):
            self.assertEqual(soffice, "C:\\LO\\soffice.exe")
            target.write_bytes(b"%PDF")

        with tempfile.TemporaryDirectory() as name:
            source = Path(name, "Text.docx")
            source.write_bytes(b"docx")
            pdf = documents.pdf_for(source, Path(name, "c"), office=office, libreoffice=libreoffice,
                                    find_soffice=lambda: "C:\\LO\\soffice.exe")
            self.assertTrue(pdf.is_file())

    def test_neither_says_why(self):
        def office(_kind, _source, target):
            target.write_bytes(b"half")  # a partial result is not kept
            raise documents.DocumentError("COM failed")

        with tempfile.TemporaryDirectory() as name:
            source = Path(name, "Text.docx")
            source.write_bytes(b"docx")
            with self.assertRaisesRegex(documents.DocumentError, "Office: COM failed; LibreOffice: not installed"):
                documents.pdf_for(source, Path(name, "c"), office=office, find_soffice=lambda: None)
            self.assertEqual(list(Path(name, "c").iterdir()), [])

    def test_office_gets_the_paths_through_the_environment(self):
        seen = {}

        def run(command, **kwargs):
            seen.update(command=command, env=kwargs["env"])
            Path(kwargs["env"]["MIXER_DOC_TARGET"]).write_bytes(b"%PDF")
            return Finished()

        with tempfile.TemporaryDirectory() as name:
            source, target = Path(name, "a'; Remove-Item x; '.pptx"), Path(name, "out.pdf")
            documents.convert_with_office("powerpoint", source, target, run=run)
            self.assertEqual(seen["env"]["MIXER_DOC_SOURCE"], str(source))
            self.assertEqual(seen["env"]["MIXER_DOC_KIND"], "powerpoint")
            self.assertNotIn("Remove-Item", " ".join(seen["command"]), "never inside the script")
            with self.assertRaisesRegex(documents.DocumentError, "Office could not convert it: boom"):
                documents.convert_with_office("word", source, Path(name, "none.pdf"),
                                              run=lambda c, **k: Finished(1, b"boom"))

    def test_libreoffice_converts_into_its_own_folder(self):
        def run(command, **_kwargs):
            outdir = Path(command[command.index("--outdir") + 1])
            self.assertIn("--headless", command)
            self.assertTrue(command[1].startswith("-env:UserInstallation=file:"), "its own profile")
            (outdir / "Text.pdf").write_bytes(b"%PDF")
            return Finished()

        with tempfile.TemporaryDirectory() as name:
            target = Path(name, "out.pdf")
            documents.convert_with_libreoffice("soffice", Path(name, "Text.odt"), target, run=run)
            self.assertEqual(target.read_bytes(), b"%PDF")

    def test_libreoffice_is_found_where_it_installs(self):
        place = "C:\\Program Files\\LibreOffice\\program\\soffice.exe"
        self.assertEqual(documents.find_libreoffice({"ProgramFiles": "C:\\Program Files"}, {place}.__contains__),
                         place)
        self.assertIsNone(documents.find_libreoffice({}, lambda _p: False))

    def test_paging_stays_within_the_document(self):
        self.assertEqual(documents.step(0, 3, "next"), 1)
        self.assertEqual(documents.step(2, 3, "next"), 2)
        self.assertEqual(documents.step(0, 3, "prev"), 0)
        self.assertEqual(documents.step(1, 3, "last"), 2)
        self.assertEqual(documents.step(2, 3, "first"), 0)
        self.assertEqual(documents.step(0, 3, "goto", 3), 2)
        self.assertEqual(documents.step(0, 3, "goto", 99), 2)

    def test_document_requests(self):
        valid = documents.valid_document_request
        self.assertTrue(valid({"action": "next"}))
        self.assertTrue(valid({"action": "goto", "page": 4, "id": "doc-192-168-188-11"}))
        self.assertTrue(valid({"action": "status"}))
        for body in ({"action": "jump"}, {"action": "goto"}, {"action": "goto", "page": 0},
                     {"action": "goto", "page": True}, {"action": "next", "page": 2},
                     {"action": "next", "id": "a/b"}, {"action": "next", "x": 1}, ["next"]):
            self.assertFalse(valid(body), body)

    def test_pip_only_when_pdfium_is_missing(self):
        ran = []
        with patch.object(documents, "pdfium", return_value=object()):
            self.assertIsNone(documents.install_missing_requirements(Path("req.txt"), run=ran.append))
        self.assertEqual(ran, [])
        with patch.object(documents, "pdfium", return_value=None):
            result = documents.install_missing_requirements(
                Path("req.txt"), run=lambda command, **_k: ran.append(command) or Finished())
        self.assertFalse(result)
        self.assertEqual(ran[0][1:5], ["-m", "pip", "install", "--disable-pip-version-check"])
        self.assertEqual(ran[0][-2:], ["-r", "req.txt"])


@unittest.skipIf(documents.pdfium() is None, "pypdfium2 is not installed here")
class RenderTests(unittest.TestCase):
    def test_pages_fit_the_window(self):
        with tempfile.TemporaryDirectory() as name:
            pages = documents.PdfPages(two_page_pdf(Path(name)))
            try:
                self.assertEqual(pages.count, 2)
                first = pages.render(0, 1920, 1080)
                self.assertEqual(first.height, 1080, "4:3 fills the height")
                self.assertLessEqual(first.width, 1920)
                second = pages.render(1, 1920, 1080)
                self.assertEqual(second.height, 1080)
                self.assertLess(second.width, 1080, "a portrait page is never cut")
                self.assertGreater(second.getpixel((second.width // 2, 540))[2], 200, "blue")
                self.assertIs(pages.render(0, 1920, 1080), first, "rendered once")
            finally:
                pages.close()

    def test_a_broken_pdf_is_a_document_error(self):
        with tempfile.TemporaryDirectory() as name:
            broken = Path(name, "x.pdf")
            broken.write_bytes(b"not a pdf")
            with self.assertRaises(documents.DocumentError):
                documents.PdfPages(broken)


class FakePages:
    def __init__(self, path, count=3):
        self.path, self.count, self.closed, self.rendered = path, count, False, []

    def render(self, index, width, height):
        self.rendered.append((index, width, height))
        return f"page{index}"

    def close(self):
        self.closed = True


DOC_PROFILE = {"id": "doc-192-168-188-11", "title": "Übersetzung.pptx", "kind": "file",
               "path": "C:\\Predigt\\Übersetzung.pptx", "requested": "C:\\Predigt\\Übersetzung*.pptx",
               "args": [], "fullscreen": True, "monitor": 2, "position": "top-left", "stamp": "s1"}


@unittest.skipIf(tkinter is None, "the desktop agent needs tkinter")
class AgentDocumentTests(unittest.TestCase):
    def setUp(self):
        agent._opened_stamps.clear()
        agent._cancelled.clear()
        agent._launching.clear()
        agent._document_view = None
        self.queue = queue.Queue()
        patcher = patch.object(agent, "requests", self.queue)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(agent._launching.clear)

    def open(self, profile=DOC_PROFILE, pdf_for=None, pages=None):
        made = []

        def pages_class(path):
            made.append((pages or FakePages)(path))
            return made[-1]

        handled = agent._open_document(profile, Path(profile["path"]), window=lambda m, f: (1920, 1080, 1920, 0),
                                       pdf_for=pdf_for or (lambda p: Path("C:/cache/x.pdf")),
                                       pages_class=pages_class)
        return handled, made

    def show(self):
        """The tk thread's part, without a screen."""
        request = self.queue.get_nowait()
        self.assertEqual(request["kind"], "document")
        agent._document_view = request["view"]
        return request

    def test_the_first_page_goes_to_the_window(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"
        handled, made = self.open()
        self.assertTrue(handled)
        request = self.show()
        self.assertEqual((request["image"], made[0].rendered), ("page0", [(0, 1920, 1080)]))
        self.assertEqual(request["view"].state(),
                         {"id": DOC_PROFILE["id"], "title": "Übersetzung.pptx", "page": 1, "pages": 3})

    def test_no_document_or_no_renderer_opens_the_default_app(self):
        video = {**DOC_PROFILE, "path": "C:\\Videos\\a.mp4"}
        self.assertFalse(agent._open_document(video, Path(video["path"])))

        def no_office(_path):
            raise documents.DocumentError("Office: not installed; LibreOffice: not installed")

        with self.assertLogs(agent.logger, "WARNING") as logs:
            self.assertEqual(self.open(pdf_for=no_office)[0], False)
        self.assertIn("default app", "\n".join(logs.output))
        with patch.object(documents, "pdfium", return_value=None):
            self.assertFalse(agent._open_document(DOC_PROFILE, Path(DOC_PROFILE["path"])),
                             "without pypdfium2 nothing is converted for nothing")
        self.assertTrue(self.queue.empty())

    def test_closed_or_started_again_while_converting_is_not_shown(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"

        def slow_convert(_path):
            # POST /close for this start comes while Office works.
            agent._close_profile_windows(DOC_PROFILE["id"], {}.get, lambda h: True, stamp="s1")
            return Path("C:/cache/x.pdf")

        handled, made = self.open(pdf_for=slow_convert)
        self.assertTrue(handled, "not opened with the default app either")
        self.assertTrue(made[0].closed)
        self.assertTrue(self.queue.empty())
        agent._cancelled.clear()
        agent._opened_stamps[DOC_PROFILE["id"]] = "s2"  # a newer start came in
        self.assertTrue(self.open()[0])
        self.assertTrue(self.queue.empty())

    def test_paging(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"
        self.open()
        view = self.show()["view"]
        self.assertEqual(agent._document_command({"action": "next"})["page"], 2)
        page = self.queue.get_nowait()
        self.assertEqual((page["kind"], page["image"], page["view"]), ("document-page", "page1", view))
        self.assertEqual(agent._document_command({"action": "last"})["page"], 3)
        self.assertEqual(agent._document_command({"action": "next"})["page"], 3, "stays on the last page")
        self.assertEqual(agent._document_command({"action": "goto", "page": 1})["page"], 1)
        self.assertEqual(agent._document_command({"action": "status"}),
                         {"id": DOC_PROFILE["id"], "title": "Übersetzung.pptx", "page": 1, "pages": 3})
        self.assertEqual([r["image"] for r in list(self.queue.queue)], ["page2", "page0"],
                         "a step that changes nothing draws nothing")
        self.assertIsNone(agent._document_command({"action": "next", "id": "other"}), "not that profile's")
        agent._document_view = None
        self.assertIsNone(agent._document_command({"action": "next"}))

    def test_kept_when_the_next_row_shows_the_same_file(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"
        self.open()
        view = self.show()["view"]
        view.page = 4
        self.assertFalse(agent._keep_document(DOC_PROFILE["id"], "C:\\Predigt\\Lesung.pdf", "s2"),
                         "another file on that PC is opened")
        self.assertTrue(agent._keep_document(DOC_PROFILE["id"], "c:\\predigt\\übersetzung*.pptx", "s2"))
        self.assertEqual((view.stamp, agent._opened_stamps[DOC_PROFILE["id"]], view.page), ("s2", "s2", 4))
        self.assertFalse(agent._keep_document("other", DOC_PROFILE["requested"], "s3"))

    def test_close_hides_it_by_profile_and_stamp(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s2"
        self.open({**DOC_PROFILE, "stamp": "s2"})
        view = self.show()["view"]
        agent.handle_request(None, None, None, {"kind": "close", "id": "predigt"})
        agent.handle_request(None, None, None, {"kind": "close", "id": DOC_PROFILE["id"], "stamp": "s1"})
        self.assertIs(agent._document_view, view, "another profile's close, or an earlier start's: stays")
        agent.handle_request(None, None, None, {"kind": "close", "id": DOC_PROFILE["id"], "stamp": "s2"})
        self.assertIsNone(agent._document_view)
        self.assertTrue(view.pages.closed)


@unittest.skipIf(tkinter is None or documents.pdfium() is None, "needs tkinter and pypdfium2")
class AgentRealPdfTests(unittest.TestCase):
    def test_a_real_pdf_end_to_end_without_a_screen(self):
        with tempfile.TemporaryDirectory() as name, patch.object(agent, "requests", queue.Queue()) as requests:
            pdf = two_page_pdf(Path(name))
            profile = {**DOC_PROFILE, "path": str(pdf), "requested": str(pdf), "stamp": None}
            self.assertTrue(agent._open_document(profile, pdf, window=lambda m, f: (1280, 720, 0, 0)))
            request = requests.get_nowait()
            agent._document_view = request["view"]
            try:
                self.assertEqual(request["image"].size[1], 720)
                self.assertEqual(agent._document_command({"action": "next"})["page"], 2)
            finally:
                agent._hide_document()


class FakeAgents:
    """Answers like the agents; /document like one showing a 12 page document."""

    def __init__(self, document_status=200, display_status=200):
        self.calls = []
        self.document_status, self.display_status = document_status, display_status

    def post(self, url, _token, payload, _timeout):
        pc, path = url.removeprefix("http://").split(":", 1)
        path = "/" + path.split("/", 1)[1]
        self.calls.append((pc, path, payload))
        if path == "/document" and self.document_status != 200:
            raise urllib.error.HTTPError(url, self.document_status, "x", {}, None)
        if path == "/display" and self.display_status != 200 and ("stamp" in payload):
            raise urllib.error.HTTPError(url, self.display_status, "x", {}, None)
        if path == "/document":
            return {"ok": True, "id": "doc", "title": "Übersetzung.pptx", "page": 3, "pages": 12}
        return {"ok": True}

    def of(self, path):
        return [(pc, payload) for pc, p, payload in self.calls if p == path]

    def wait_closes(self, count, seconds=3.0):
        end = time.monotonic() + seconds
        while time.monotonic() < end and len(self.of("/close")) < count:
            time.sleep(0.02)
        return self.of("/close")


class ServiceTests(unittest.TestCase):
    def play(self, client, item_id):
        answer = client.post(f"/program/items/{item_id}/play").json()
        self.assertTrue(answer["ok"], answer)

    def test_a_file_field_document_goes_full_screen_to_the_song_monitor(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            item = client.post("/program/items", json=row("Predigt", translation=DOC_A)).json()["item"]
            self.play(client, item["id"])
            (pc, sent), = agents.of("/display")
            self.assertEqual(pc, BEAMER_PC)
            self.assertEqual({k: sent[k] for k in ("id", "kind", "fullscreen", "monitor", "path")}, {
                "id": "doc-192-168-188-11", "kind": "file", "fullscreen": True,
                "monitor": 2, "path": "C:\\Predigt\\Übersetzung.pptx"})
            self.assertIn("stamp", sent)
            self.assertEqual(client.app.state.document_shown, (BEAMER_PC, "doc-192-168-188-11"))

    def test_a_pc_without_a_song_screen_uses_monitor_1(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            answer = client.post("/program/items", json=row("x", translation=f"file:{STREAM_PC}|C:\\a.pdf")).json()
            self.play(client, answer["item"]["id"])
            self.assertEqual(agents.of("/display")[0][1]["monitor"], 1)

    def test_an_old_agent_gets_the_start_without_the_new_fields(self):
        agents = FakeAgents(display_status=400)
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            item = client.post("/program/items", json=row("Predigt", translation=DOC_A)).json()["item"]
            self.play(client, item["id"])
            starts = agents.of("/display")
            self.assertEqual(len(starts), 2)
            self.assertNotIn("stamp", starts[1][1])

    def test_the_next_row_keeps_the_same_file_and_closes_another(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            ids = [client.post("/program/items", json=row(title, translation=value)).json()["item"]["id"]
                   for title, value in (("Predigt 1", DOC_A), ("Predigt 2", DOC_A), ("Lesung", DOC_B))]
            ids.append(client.post("/program/items", json=row("Segen")).json()["item"]["id"])
            self.play(client, ids[0])
            self.play(client, ids[1])
            time.sleep(0.1)
            self.assertEqual(agents.of("/close"), [], "the same file stays, on its page")
            self.assertTrue(agents.of("/display")[1][1]["keep_if_open"])
            self.play(client, ids[2])
            self.assertNotIn("keep_if_open", agents.of("/display")[2][1], "another file is opened, never kept")
            time.sleep(0.1)
            self.assertEqual(agents.of("/close"), [], "it replaces the one before (one document per PC)")
            self.play(client, ids[3])
            closes = agents.wait_closes(1)
            self.assertEqual([body["id"] for _pc, body in closes], ["doc-192-168-188-11"], "a row without it")
            self.assertEqual(closes[0][1]["stamp"], agents.of("/display")[2][1]["stamp"], "the last start's")

    def test_a_document_profile_is_paged_too(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            self.assertTrue(client.post("/desktop/predigt/open").json()["ok"])
            (_pc, sent), = agents.of("/display")
            self.assertEqual((sent["monitor"], sent["fullscreen"]), (3, True), "the profile's own monitor")
            answer = client.post("/documents/next").json()
            self.assertEqual((answer["ok"], answer["pc"], answer["page"], answer["pages"]), (True, BEAMER_PC, 3, 12))
            self.assertEqual(agents.of("/document")[-1], (BEAMER_PC, {"action": "next"}))

    def test_paging_answers(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            self.assertEqual(client.post("/documents/next").status_code, 409, "nothing shown yet")
            self.assertEqual(client.get("/documents/current").json(), {"ok": True, "shown": False})
            self.assertEqual(client.post("/documents/jump").status_code, 404)
            self.assertEqual(client.post("/documents/prev", json={"pc": "8.8.8.8"}).status_code, 400)
            answer = client.post("/documents/prev", json={"pc": STREAM_PC}).json()
            self.assertEqual((answer["pc"], answer["page"]), (STREAM_PC, 3))
            client.app.state.document_shown = (BEAMER_PC, "doc-192-168-188-11")
            self.assertEqual(client.get("/documents/current").json()["pages"], 12)
            agents.document_status = 404
            old = client.post("/documents/next")
            self.assertEqual(old.status_code, 409)
            self.assertIn("zu alt", old.json()["reason"])
            agents.document_status = 409
            self.assertEqual(client.post("/documents/next").json()["shown"], False)
            self.assertEqual(client.get("/documents/current").json()["shown"], False)


if __name__ == "__main__":
    unittest.main()
