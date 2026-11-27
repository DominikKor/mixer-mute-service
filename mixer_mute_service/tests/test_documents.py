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

    agent._open_document_real = agent._open_document

BEAMER_PC, STREAM_PC = "192.168.188.11", "192.168.188.20"
ENV = {"DESKTOP_AGENT_TOKEN": "test-token"}
PROFILES = (
    "desktop_display:\n  enabled: true\n  profiles:\n"
    f"    predigt:\n      title: Predigt\n      kind: file\n      path: 'C:\\Predigt\\Übersetzung*.pptx'\n"
    f"      target_ip: {BEAMER_PC}\n      monitor: 3\n"
    f"    spiegel:\n      title: Übersetzung Spiegel\n      kind: file\n      path: 'C:\\Predigt\\Text.pdf'\n"
    f"      target_ip: {BEAMER_PC}\n      monitors: [2, 3]\n"
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

    def test_the_page_knows_the_same_suffixes(self):
        import re

        from app import documents as service

        script = (Path(service.__file__).parent / "static" / "js" / "doc-dock.js").read_text(encoding="utf-8")
        pattern = re.search(r"DOC_DOCK_SUFFIX_RE = /(.+)/i;", script).group(1)
        regex = re.compile(pattern, re.I)
        for suffix in service.DOCUMENT_SUFFIXES:
            self.assertTrue(regex.search(f"C:\\a\\Predigt{suffix}"), suffix)
        for name in ("Video.mp4", "Bild.png", "Lied.foil", "x.pdfa"):
            self.assertIsNone(regex.search(name), name)
        css = (Path(service.__file__).parent / "static" / "css" / "doc-dock.css").read_text(encoding="utf-8")
        keys = re.search(r"\.doc-dock button \{([^}]*)\}", css).group(1)
        self.assertIn("min-height: 52px", keys, "keys as high as the song dock's")
        script_keys = re.findall(r'class="(dd-(?:prev|next))[^"]*"', script)
        self.assertEqual(script_keys, ["dd-prev", "dd-next"])
        self.assertNotIn("program-play", script, "never looks like the song's green Weiter")
        self.assertNotIn("JSON.stringify(item)", script, "titles and notes never count")

    def test_a_pdf_is_shown_as_it_is(self):
        def never(*_args):
            raise AssertionError("a PDF is never converted")

        source = Path("C:/x/Lesung.pdf")
        self.assertEqual(documents.pdf_for(source, Path("unused"), office=never, libreoffice=never), source)

    def test_office_converts_once_then_the_cache(self):
        calls = []

        def office(kind, source, target, timeout):
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
        def office(_kind, _source, _target, timeout):
            raise documents.DocumentError("no Office")

        def libreoffice(soffice, source, target, timeout):
            self.assertEqual(soffice, "C:\\LO\\soffice.exe")
            target.write_bytes(b"%PDF")

        with tempfile.TemporaryDirectory() as name:
            source = Path(name, "Text.docx")
            source.write_bytes(b"docx")
            pdf = documents.pdf_for(source, Path(name, "c"), office=office, libreoffice=libreoffice,
                                    find_soffice=lambda: "C:\\LO\\soffice.exe")
            self.assertTrue(pdf.is_file())

    def test_neither_says_why(self):
        def office(_kind, _source, target, timeout):
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
        with tempfile.TemporaryDirectory() as name:
            state = Path(name, "pip.json")
            with patch.object(documents, "pdfium", return_value=object()):
                self.assertIsNone(documents.install_missing_requirements("v1", state, run=ran.append))
            self.assertEqual((ran, state.exists()), ([], False))
            self.assertEqual(documents.install_status["state"], "not needed")

    def test_pip_once_per_version_with_backoff(self):
        ran = []

        def pip(command, **kwargs):
            ran.append(command)
            self.assertEqual(kwargs["timeout"], documents.PIP_TIMEOUT_SECONDS)
            return Finished(1, b"no network")

        install = documents.install_missing_requirements
        with tempfile.TemporaryDirectory() as name, patch.object(documents, "pdfium", return_value=None):
            state = Path(name, "pip.json")
            self.assertFalse(install("v1", state, run=pip, now=1000.0))
            self.assertEqual(ran[0][1:5], ["-m", "pip", "install", "--disable-pip-version-check"])
            self.assertEqual(ran[0][-1], documents.PDFIUM_REQUIREMENT, "only pypdfium2: Pillow is loaded")
            self.assertEqual(documents.install_status["state"], "failed")
            self.assertIn("no network", documents.install_status["reason"])
            self.assertIsNone(install("v1", state, run=pip, now=1000.0 + 600))
            self.assertEqual(documents.install_status["state"], "waiting")
            self.assertEqual(len(ran), 1, "not on every start after a failure")
            install("v1", state, run=pip, now=1000.0 + 3601)
            self.assertEqual(len(ran), 2, "again after an hour")
            self.assertIsNone(install("v1", state, run=pip, now=4601.0 + 3601))
            self.assertEqual(len(ran), 2, "then after two hours")
            install("v2", state, run=pip, now=4601.0 + 3601)
            self.assertEqual(len(ran), 3, "a new agent version tries at once")

    def test_pip_success_and_timeout(self):
        modules = [None, object()]
        with tempfile.TemporaryDirectory() as name, \
                patch.object(documents, "pdfium", side_effect=lambda: modules.pop(0)):
            state = Path(name, "pip.json")
            self.assertTrue(documents.install_missing_requirements(
                "v1", state, run=lambda c, **k: Finished(), now=5.0))
            self.assertEqual(documents.install_status, {"state": "ok", "version": "v1"})

        def slow(command, **kwargs):
            raise documents.subprocess.TimeoutExpired(command, kwargs["timeout"])

        with tempfile.TemporaryDirectory() as name, patch.object(documents, "pdfium", return_value=None):
            self.assertFalse(documents.install_missing_requirements("v1", Path(name, "s"), run=slow))
            self.assertIn("longer than", documents.install_status["reason"])
            self.assertIn("pip", documents.capabilities(lambda: None))

    def test_the_requirement_is_pinned(self):
        lines = (Path(documents.__file__).parent / "requirements.txt").read_text().split()
        self.assertIn(documents.PDFIUM_REQUIREMENT, lines, "the agent installs the same version")
        dev = (Path(__file__).resolve().parent.parent / "requirements-dev.txt").read_text().split()
        self.assertIn(documents.PDFIUM_REQUIREMENT, dev, "the real-PDF tests run in CI")

    def test_a_hung_office_is_ended_but_never_the_users(self):
        ended = []

        def hung(command, timeout, env):
            Path(env["MIXER_DOC_PIDFILE"]).write_text("4711,815\n", encoding="utf-8")
            raise documents.subprocess.TimeoutExpired(command, timeout)

        def end_office(pidfile, kind):
            ended.extend(documents.end_started_office(
                pidfile, kind, image_of={4711: "POWERPNT.EXE", 815: "notepad.exe"}.get, kill=lambda pid: None,
                windows=lambda pid: ["#32770"]))  # only its (password) dialog: still the conversion's

        with tempfile.TemporaryDirectory() as name:
            with self.assertRaisesRegex(documents.DocumentError, "longer than 12 s"):
                documents.convert_with_office("powerpoint", Path(name, "a.pptx"), Path(name, "a.pdf"), timeout=12,
                                              run=hung, end_office=end_office)
        self.assertEqual(ended, [4711], "only the Office this conversion started (the pid is checked again)")
        with tempfile.TemporaryDirectory() as name:
            empty = Path(name, "pid")
            empty.write_text("\n", encoding="utf-8")  # COM used the user's own Office: nothing started
            self.assertEqual(documents.end_started_office(empty, "word", image_of=lambda p: "WINWORD.EXE",
                                                          kill=lambda p: self.fail("never")), [])
            self.assertEqual(documents.end_started_office(Path(name, "none"), "word"), [])

    def test_a_hung_office_the_operator_opened_a_file_in_stays(self):
        with tempfile.TemporaryDirectory() as name:
            pidfile = Path(name, "pid")
            pidfile.write_text("4711", encoding="utf-8")
            office = {"image_of": lambda pid: "POWERPNT.EXE", "kill": lambda pid: self.fail("never ended")}
            with self.assertLogs(documents.logger, "WARNING") as logs:
                self.assertEqual(documents.end_started_office(
                    pidfile, "powerpoint", windows=lambda pid: ["PPTFrameClass", "#32770"], **office), [],
                    "the operator's presentation in this instance")
                self.assertEqual(documents.end_started_office(pidfile, "powerpoint", windows=lambda pid: None,
                                                              **office), [], "windows unknown: left alone")
            self.assertIn("someone has a file open in it", "\n".join(logs.output))
            self.assertEqual(documents.end_started_office(pidfile, "word", image_of=lambda pid: "WINWORD.EXE",
                                                          kill=lambda pid: None, windows=lambda pid: []), [4711])

    def test_office_script_fails_fast_on_protected_files(self):
        self.assertIn("'mixer-no-password'", documents._OFFICE_SCRIPT, "Word: a password file fails at once")
        self.assertIn("DisplayAlerts = 1", documents._OFFICE_SCRIPT, "PowerPoint: no dialogs")
        self.assertIn("MIXER_DOC_PIDFILE", documents._OFFICE_SCRIPT)
        self.assertEqual(documents._OFFICE_SCRIPT.count("$app.DisplayAlerts = $alerts"), 2,
                         "a user's PowerPoint keeps its own setting")
        self.assertNotIn("$app.Visible", documents._OFFICE_SCRIPT, "never hides a user's window")

    def test_conversion_ends_by_the_deadline(self):
        lefts = []

        def office(_kind, _source, _target, timeout):
            lefts.append(timeout)
            raise documents.DocumentError("hung")

        def libreoffice(_soffice, _source, _target, timeout):
            lefts.append(timeout)
            raise documents.DocumentError("hung")

        with tempfile.TemporaryDirectory() as name:
            source = Path(name, "Text.docx")
            source.write_bytes(b"docx")
            deadline = documents.time.monotonic() + 60
            with self.assertRaises(documents.DocumentError):
                documents.pdf_for(source, Path(name, "c"), office=office, libreoffice=libreoffice,
                                  find_soffice=lambda: "soffice", deadline=deadline)
            self.assertTrue(all(left <= 60 for left in lefts), lefts)
            with self.assertRaisesRegex(documents.DocumentError, "no time left"):
                documents.pdf_for(source, Path(name, "c"), office=office, find_soffice=lambda: None,
                                  deadline=documents.time.monotonic() + 1)

    def test_cancelled_while_waiting_for_another_conversion(self):
        with tempfile.TemporaryDirectory() as name:
            source = Path(name, "Text.docx")
            source.write_bytes(b"docx")
            with self.assertRaises(documents.DocumentCancelled):
                documents.pdf_for(source, Path(name, "c"), office=lambda *a, **k: self.fail("not converted"),
                                  cancelled=lambda: True)
            documents._convert_lock.acquire()
            try:
                with self.assertRaisesRegex(documents.DocumentError, "still being converted"):
                    documents.pdf_for(source, Path(name, "c"), deadline=documents.time.monotonic() + 0.2)
            finally:
                documents._convert_lock.release()

    def test_the_page_cache_is_used_under_the_lock(self):
        held = []

        class Page:
            def get_size(self):
                return (100.0, 50.0)

            def render(self, scale):
                held.append(documents._pdfium_lock.locked())
                return SimpleNamespace(to_pil=lambda: Image.new("RGB", (int(100 * scale), int(50 * scale))))

            def close(self):
                pass

        class Document(list):
            def close(self):
                pass

        module = SimpleNamespace(PdfDocument=lambda path: Document([Page(), Page()]))
        pages = documents.PdfPages(Path("x.pdf"), module=module)
        import threading
        threads = [threading.Thread(target=pages.render, args=(i % 2, 200, 100)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual((held, len(pages._rendered)), ([True, True], 2), "each page rendered once, under the lock")

    def test_umlaut_paths(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name, "Übersetzung Gottesdienst")
            folder.mkdir()
            source = folder / "Predigt Größe ä.pptx"
            source.write_bytes(b"pptx")
            self.assertRegex(documents.cache_name(source), r"^[0-9a-f]{24}\.pdf$")
            pdf = documents.pdf_for(source, folder / "cache", office=lambda k, s, t, timeout: t.write_bytes(b"%PDF"),
                                    find_soffice=lambda: None)
            self.assertTrue(pdf.is_file())


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

    def test_a_real_pdf_with_umlauts_in_its_path(self):
        with tempfile.TemporaryDirectory() as name:
            folder = Path(name, "Übersetzung ä ö ü ß")
            folder.mkdir()
            pdf = two_page_pdf(folder)
            pdf = pdf.rename(folder / "Predigt – Größe.pdf")
            pages = documents.PdfPages(pdf)
            try:
                self.assertEqual(pages.render(1, 640, 360).height, 360)
            finally:
                pages.close()
            with self.assertRaises(documents.DocumentError):
                pages.render(0, 640, 360)  # closed: never a crash in pdfium

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
        if self.closed:
            raise documents.DocumentError("the document was closed")
        self.rendered.append((index, width, height))
        return f"page{index}@{width}"

    def close(self):
        self.closed = True


DOC_PROFILE = {"id": "doc-192-168-188-11", "title": "Übersetzung.pptx", "kind": "file",
               "path": "C:\\Predigt\\Übersetzung.pptx", "requested": "C:\\Predigt\\Übersetzung*.pptx",
               "args": [], "fullscreen": True, "monitor": 2, "position": "top-left", "stamp": "s1"}
SCREEN = {"monitor": 2, "area": (1920, 0, 3840, 1080), "rect": (1920, 0, 1920, 1080), "client": (1920, 1080)}


def no_office(_path, _deadline, _cancelled):
    raise documents.DocumentError("Office: not installed; LibreOffice: not installed")


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

    def open(self, profile=DOC_PROFILE, pdf_for=None, pages=None, screens=None):
        made = []

        def pages_class(path):
            made.append((pages or FakePages)(path))
            return made[-1]

        handled = agent._open_document(profile, Path(profile["path"]), screens=screens or (lambda p: [SCREEN]),
                                       pdf_for=pdf_for or (lambda p, d, c: Path("C:/cache/x.pdf")),
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
        self.assertEqual((request["images"], made[0].rendered), (["page0@1920"], [(0, 1920, 1080)]))
        self.assertEqual(request["view"].state(),
                         {"id": DOC_PROFILE["id"], "title": "Übersetzung.pptx", "page": 1, "pages": 3})

    def test_screens_like_an_image(self):
        bounds = {1: (0, 0, 1920, 1080), 2: (1920, 0, 3840, 1080)}.get
        work = {1: (0, 0, 1920, 1040), 2: (1920, 0, 3840, 1040)}.get
        full = agent._document_screens({**DOC_PROFILE, "monitors": [1, 2]}, bounds, work)
        self.assertEqual([s["rect"] for s in full], [(0, 0, 1920, 1080), (1920, 0, 1920, 1080)], "mirrors")
        corner = agent._document_screens({**DOC_PROFILE, "fullscreen": False, "position": "bottom-right"},
                                         bounds, work)[0]
        self.assertEqual(corner["rect"], (2880, 520, 960, 520), "a quarter of the work area, in its corner")
        self.assertEqual(corner["client"], (960 - agent.DOCUMENT_FRAME[0], 520 - agent.DOCUMENT_FRAME[1]))
        center = agent._document_screens({**DOC_PROFILE, "fullscreen": False, "position": "center"}, bounds, work)
        self.assertEqual(center[0]["rect"], (1920, 0, 1920, 1040), "the whole work area, never the taskbar")

    def test_mirrors_show_the_same_page_on_each(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"
        small = {**SCREEN, "monitor": 3, "client": (1280, 720)}
        self.open(screens=lambda p: [SCREEN, small])
        view = self.show()["view"]
        self.assertEqual(self.queue.empty(), True)
        agent._document_command({"action": "next"})
        self.assertEqual(self.queue.get_nowait()["images"], ["page1@1920", "page1@1280"])
        self.assertTrue(agent._valid_monitors({"kind": "file", "path": "C:\\a\\b.pdf", "monitor": 1,
                                               "monitors": [1, 2]}))
        self.assertFalse(agent._valid_monitors({"kind": "file", "path": "C:\\a\\b.mp4", "monitor": 1,
                                                "monitors": [1, 2]}))
        self.assertTrue(view.screens)

    def test_no_document_or_no_renderer_opens_the_default_app(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"  # this start is still the current one
        video = {**DOC_PROFILE, "path": "C:\\Videos\\a.mp4"}
        self.assertFalse(agent._open_document(video, Path(video["path"])))
        with self.assertLogs(agent.logger, "WARNING") as logs:
            self.assertEqual(self.open(pdf_for=no_office)[0], False)
        self.assertIn("default app", "\n".join(logs.output))
        with patch.object(documents, "pdfium", return_value=None):
            self.assertFalse(agent._open_document(DOC_PROFILE, Path(DOC_PROFILE["path"])),
                             "without pypdfium2 nothing is converted for nothing")
        self.assertTrue(self.queue.empty())

    def test_closed_or_started_again_while_converting_is_not_shown(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"

        def slow_convert(_path, _deadline, cancelled):
            # POST /close for this start comes while Office works.
            agent._close_profile_windows(DOC_PROFILE["id"], {}.get, lambda h: True, stamp="s1")
            self.assertTrue(cancelled(), "the conversion is told")
            return Path("C:/cache/x.pdf")

        handled, made = self.open(pdf_for=slow_convert)
        self.assertTrue(handled, "not opened with the default app either")
        self.assertTrue(made[0].closed)
        self.assertTrue(self.queue.empty())
        agent._cancelled.clear()
        agent._opened_stamps[DOC_PROFILE["id"]] = "s2"  # a newer start came in
        self.assertTrue(self.open()[0])
        self.assertTrue(self.queue.empty())

        def cancelled_in_line(_path, _deadline, _cancelled):
            raise documents.DocumentCancelled("closed while waiting")

        reply = agent._DocumentReply()
        self.assertTrue(self.open({**DOC_PROFILE, "reply": reply}, pdf_for=cancelled_in_line)[0])
        self.assertEqual((reply.done.is_set(), reply.shown), (True, False))

    def test_a_failed_document_closed_meanwhile_opens_nothing(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"
        reply = agent._DocumentReply()

        def fails_after_close(_path, _deadline, _cancelled):
            agent._close_profile_windows(DOC_PROFILE["id"], {}.get, lambda h: True, stamp="s1")
            raise documents.DocumentError("Office: took longer than 150 s")

        self.assertTrue(self.open({**DOC_PROFILE, "reply": reply}, pdf_for=fails_after_close)[0],
                        "no late default app: the program moved on")
        self.assertEqual((reply.done.is_set(), reply.shown), (True, False))

    def test_a_framed_window_takes_its_real_client_size(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"
        guessed = {**SCREEN, "client": (944, 481)}
        self.open({**DOC_PROFILE, "fullscreen": False}, screens=lambda p: [guessed])
        view = self.show()["view"]
        threads = []
        with patch.object(agent.threading, "Thread", lambda target, args, daemon: SimpleNamespace(
                start=lambda: threads.append((target, args)))):
            self.assertFalse(agent._fit_document_client(view, ["w"], client_size=lambda w: (945, 480)), "close enough")
            self.assertTrue(agent._fit_document_client(view, ["w"], client_size=lambda w: (936, 466)), "150 %")
        self.assertEqual(view.screens[0]["client"], (936, 466))
        target, args = threads[0]
        target(*args)
        self.assertEqual(self.queue.get_nowait()["images"], ["page0@936"], "the page again for the real size")

    def test_a_close_between_prepared_and_shown(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"
        self.open()
        request = self.queue.get_nowait()
        agent._close_profile_windows(DOC_PROFILE["id"], {}.get, lambda h: True, stamp="s1")
        self.assertFalse(agent._show_document(None, request), "closed meanwhile: never shown")
        self.assertTrue(request["view"].pages.closed)
        self.assertIsNone(agent._document_view)

    def test_paging(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"
        self.open()
        view = self.show()["view"]
        self.assertEqual(agent._document_command({"action": "next"})["page"], 2)
        page = self.queue.get_nowait()
        self.assertEqual((page["kind"], page["images"], page["view"], page["page"]),
                         ("document-page", ["page1@1920"], view, 1))
        self.assertEqual(agent._document_command({"action": "last"})["page"], 3)
        self.assertEqual(agent._document_command({"action": "next"})["page"], 3, "stays on the last page")
        self.assertEqual(agent._document_command({"action": "goto", "page": 1})["page"], 1)
        self.assertEqual(agent._document_command({"action": "status"}),
                         {"id": DOC_PROFILE["id"], "title": "Übersetzung.pptx", "page": 1, "pages": 3})
        self.assertEqual([r["images"] for r in list(self.queue.queue)], [["page2@1920"], ["page0@1920"]],
                         "a step that changes nothing draws nothing")
        self.assertIsNone(agent._document_command({"action": "next", "id": "other"}), "not that profile's")
        agent._document_view = None
        self.assertIsNone(agent._document_command({"action": "next"}))

    def test_pages_render_outside_the_lock_and_only_the_newest_shows(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"
        self.open()
        view = self.show()["view"]
        seen = []

        def render(page):
            seen.append(agent._document_lock._is_owned())
            if page == 1:
                view.page = 2  # a second step came in while this one rendered
            return [f"page{page}"]

        with patch.object(view, "render", render):
            agent._document_command({"action": "next"})
        self.assertEqual(seen, [False], "the lock is free while pdfium renders")
        self.assertTrue(self.queue.empty(), "an older step never reaches the window")
        with patch.object(agent, "_document_windows", []):
            agent._show_document_page({"view": view, "page": 1, "images": ["old"]})  # ignored, no windows touched
        view.pages.close()
        self.assertEqual(agent._document_command({"action": "next"})["page"], 3, "hidden meanwhile: no crash")

    def test_keys_never_turn_pages_by_typing(self):
        keys = [key for group in agent.DOCUMENT_KEYS.values() for key in group]
        for key in ("<space>", "<Return>", "<BackSpace>"):
            self.assertNotIn(key, keys)
        for key in ("<Next>", "<Prior>", "<Right>", "<Left>", "<Home>", "<End>"):
            self.assertIn(key, keys, "presenters send Page Up / Down or arrows")

    def test_focus_only_when_nothing_else_has_it(self):
        mine = agent.os.getpid()
        classes = {88: "PPTFrameClass", 99: "Progman", 98: "WorkerW", 97: "Shell_TrayWnd"}.get
        self.assertTrue(agent._may_take_focus(0), "nothing in front")
        self.assertTrue(agent._may_take_focus(77, {77: mine}.get, classes), "the agent's own window in front")
        self.assertFalse(agent._may_take_focus(88, {88: mine + 1}.get, classes), "someone works on that PC")
        for shell in (99, 98, 97):
            self.assertTrue(agent._may_take_focus(shell, lambda h: mine + 1, classes),
                            "the desktop or taskbar in front: a presenter works without a click")

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

    def test_the_start_is_answered_once_the_first_page_is_on_screen(self):
        reply = agent._DocumentReply()
        profile = {**DOC_PROFILE, "reply": reply}
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"
        self.open(profile)
        self.assertFalse(reply.done.is_set(), "prepared, not yet on screen")
        request = self.queue.get_nowait()
        self.assertIs(request["reply"], reply)
        with patch.object(agent, "_show_document", return_value=True):
            agent.handle_request(None, None, None, request)
        self.assertEqual((reply.done.is_set(), reply.shown), (True, True))
        failed = agent._DocumentReply()
        with patch.object(agent, "_show_document", side_effect=RuntimeError("no window")):
            with self.assertRaises(RuntimeError):
                agent.handle_request(None, None, None, {**request, "reply": failed})
        self.assertEqual((failed.done.is_set(), failed.shown), (True, False))
        closed = agent._DocumentReply()
        with patch.object(agent, "_show_document", return_value=False):
            agent.handle_request(None, None, None, {**request, "reply": closed})
        self.assertEqual(closed.shown, False, "closed before it was shown")

    def test_not_shown_by_the_agent_answers_shown_false(self):
        agent._opened_stamps[DOC_PROFILE["id"]] = "s1"
        reply = agent._DocumentReply()
        profile = {**DOC_PROFILE, "reply": reply, "path": "C:\\Predigt\\Text.docx"}
        with patch.object(agent, "_open_document", lambda p, path: agent.__dict__["_open_document_real"](
                p, path, screens=lambda p: [SCREEN], pdf_for=no_office, pages_class=FakePages)), \
                patch.object(agent, "_visible_windows", side_effect=RuntimeError("default app")), \
                self.assertLogs(agent.logger, "WARNING"):
            agent._launch_profile(profile)
        self.assertEqual((reply.done.is_set(), reply.shown), (True, False))

    def test_only_documents_wait_for_their_page(self):
        with patch.object(documents, "pdfium", return_value=object()):
            self.assertIsNotNone(agent._document_reply({"kind": "file"}, Path("C:/a/Predigt.pptx")))
            self.assertIsNone(agent._document_reply({"kind": "file"}, Path("C:/a/Video.mp4")))
            self.assertIsNone(agent._document_reply({"kind": "image"}, Path("C:/a/Bild.pdf")))
        with patch.object(documents, "pdfium", return_value=None):
            self.assertIsNone(agent._document_reply({"kind": "file"}, Path("C:/a/Predigt.pdf")),
                              "opened with its default app: answered at once")

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
            folder = Path(name, "Übersetzung")
            folder.mkdir()
            pdf = two_page_pdf(folder)
            profile = {**DOC_PROFILE, "path": str(pdf), "requested": str(pdf), "stamp": None}
            screen = {**SCREEN, "client": (1280, 720)}
            self.assertTrue(agent._open_document(profile, pdf, screens=lambda p: [screen]))
            request = requests.get_nowait()
            agent._document_view = request["view"]
            try:
                self.assertEqual(request["images"][0].size[1], 720)
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

    def test_a_document_through_a_desktop_key_takes_the_keys_screen(self):
        from app.row_files import start_id

        sent = []

        def post(url, _token, payload, timeout):
            sent.append((url, payload, timeout))
            if "monitors" in payload and old:
                raise urllib.error.HTTPError(url, 400, "x", {}, None)
            return {"ok": True, "shown": True}

        lesung = "C:\\Predigt\\Lesung.pdf"
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", post):
            old = False
            item = client.post("/program/items", json=row("Lesung", desktop=[f"via:predigt|{lesung}"])).json()["item"]
            self.play(client, item["id"])
            (url, body, timeout), = [entry for entry in sent if entry[0].endswith("/display")]
            self.assertIn(BEAMER_PC, url)
            self.assertEqual({k: body[k] for k in ("id", "kind", "fullscreen", "monitor", "path")}, {
                "id": start_id("predigt", lesung), "kind": "file", "fullscreen": True, "monitor": 3, "path": lesung},
                "the key's PC, monitor and full screen, the step's file")
            self.assertEqual(timeout, 20.0, "waited for until the first page is on screen")
            self.assertEqual(client.app.state.document_shown, (BEAMER_PC, start_id("predigt", lesung)), "paged there")
            # A key on several monitors: the same page on each; an agent from before documents: one.
            sent[:] = []
            item = client.post("/program/items", json=row("Spiegel", desktop=[f"via:spiegel|{lesung}"])).json()["item"]
            self.play(client, item["id"])
            self.assertEqual([body["monitors"] for url, body, _t in sent if url.endswith("/display")], [[2, 3]])
            old, sent[:] = True, []
            item = client.post("/program/items", json=row("Alt", desktop=[f"via:spiegel|{lesung}"])).json()["item"]
            self.play(client, item["id"])
            starts = [body for url, body, _t in sent if url.endswith("/display")]
            self.assertNotIn("monitors", starts[-1])
            # Not a document: no waiting, no paging there.
            client.app.state.document_shown = None
            sent[:] = []
            item = client.post("/program/items", json=row("Bild", desktop=["via:predigt|C:\\Bilder\\a.png"])).json()["item"]
            self.play(client, item["id"])
            self.assertLess([t for url, _b, t in sent if url.endswith("/display")][0], 20.0)
            self.assertIsNone(client.app.state.document_shown)

    def test_an_old_file_field_becomes_a_step_through_the_key_on_that_pc(self):
        with app_client(PROFILES, env=ENV) as (client, _folder):
            item = client.post("/program/items", json=row("Predigt", translation=DOC_A)).json()["item"]
            # Übersetzung: the key on that PC that shows on monitor 2 (the beamer).
            self.assertEqual(item["desktop"], ["via:spiegel|C:\\Predigt\\Übersetzung.pptx"])

    def test_an_old_file_step_goes_full_screen_to_the_song_monitor(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            item = client.post("/program/items", json=row("Predigt", desktop=[DOC_A])).json()["item"]
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
            self.assertEqual(answer["item"]["desktop"], [f"file:{STREAM_PC}|C:\\a.pdf"], "no Desktop key on that PC")
            self.play(client, answer["item"]["id"])
            self.assertEqual(agents.of("/display")[0][1]["monitor"], 1)

    def test_an_old_agent_gets_the_start_without_the_new_fields(self):
        agents = FakeAgents(display_status=400)
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            item = client.post("/program/items", json=row("Predigt", desktop=[DOC_A])).json()["item"]
            self.play(client, item["id"])
            starts = agents.of("/display")
            self.assertEqual(len(starts), 2)
            self.assertNotIn("stamp", starts[1][1])

    def test_the_next_row_keeps_the_same_file_and_closes_another(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            ids = [client.post("/program/items", json=row(title, desktop=[value])).json()["item"]["id"]
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
            # It replaces the one before (one document per PC); a close sent
            # meanwhile names the earlier start, which the agent ignores once
            # the new one is there.
            earlier = agents.of("/display")[1][1]["stamp"]
            self.assertTrue(all(body["stamp"] == earlier for _pc, body in agents.of("/close")))
            before = len(agents.of("/close"))
            self.play(client, ids[3])
            closes = agents.wait_closes(before + 1)[before:]
            self.assertEqual([body["id"] for _pc, body in closes], ["doc-192-168-188-11"], "a row without it")
            self.assertEqual(closes[0][1]["stamp"], agents.of("/display")[2][1]["stamp"], "the last start's")

    def test_the_next_row_keeps_the_same_file_through_the_same_key(self):
        from app.row_files import start_id

        agents = FakeAgents()
        lesung, text = "C:\\Predigt\\Lesung.pdf", "C:\\Predigt\\Text.pdf"
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            ids = [client.post("/program/items", json=row(title, desktop=[value])).json()["item"]["id"]
                   for title, value in (("Lesung 1", f"via:predigt|{lesung}"), ("Lesung 2", f"via:predigt|{lesung}"),
                                        ("Text", f"via:predigt|{text}"), ("Segen", "@song"))]
            self.play(client, ids[0])
            self.play(client, ids[1])
            time.sleep(0.1)
            self.assertEqual(agents.of("/close"), [], "the same file through the same key stays, on its page")
            self.assertTrue(agents.of("/display")[1][1]["keep_if_open"])
            self.play(client, ids[2])
            self.assertNotIn("keep_if_open", agents.of("/display")[2][1], "another file: another start")
            closes = agents.wait_closes(1)
            self.assertEqual([body["id"] for _pc, body in closes], [start_id("predigt", lesung)])
            self.play(client, ids[3])
            closes = agents.wait_closes(2)
            self.assertEqual([body["id"] for _pc, body in closes[1:]], [start_id("predigt", text)])

    def test_a_document_profile_is_paged_too(self):
        agents = FakeAgents()
        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", agents.post):
            self.assertTrue(client.post("/desktop/predigt/open").json()["ok"])
            (_pc, sent), = agents.of("/display")
            self.assertEqual((sent["monitor"], sent["fullscreen"]), (3, True), "the profile's own monitor")
            answer = client.post("/documents/next").json()
            self.assertEqual((answer["ok"], answer["pc"], answer["page"], answer["pages"]), (True, BEAMER_PC, 3, 12))
            self.assertEqual(agents.of("/document")[-1], (BEAMER_PC, {"action": "next"}))

    def test_a_document_on_several_monitors_and_an_old_agent(self):
        sent = []

        def post(url, _token, payload, timeout):
            sent.append((payload, timeout))
            if "monitors" in payload and old:
                raise urllib.error.HTTPError(url, 400, "x", {}, None)
            return {"ok": True, "shown": True}

        with app_client(PROFILES, env=ENV) as (client, _folder), patch("app.display_agent._post", post):
            old = False
            self.assertTrue(client.post("/desktop/spiegel/open").json()["ok"])
            self.assertEqual((sent[0][0]["monitors"], sent[0][1]), ([2, 3], 20.0),
                             "the same page on both; waited for until it is on screen")
            old, sent[:] = True, []
            self.assertTrue(client.post("/desktop/spiegel/open").json()["ok"])
            self.assertNotIn("monitors", sent[-1][0], "an agent from before documents: one monitor")
            body = {"id": "doku2", "title": "Doku", "path": "C:\\x\\Lesung.docx", "target_ip": BEAMER_PC,
                    "monitor": 1, "monitors": [1, 2]}
            self.assertEqual(client.post("/desktop/profiles", json=body).status_code, 200,
                             "documents may be saved with several monitors")
            video = {**body, "id": "video2", "path": "C:\\x\\Film.mp4"}
            self.assertEqual(client.post("/desktop/profiles", json=video).status_code, 400)

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
