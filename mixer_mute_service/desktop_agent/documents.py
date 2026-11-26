"""Documents (PDF, PowerPoint, Word) shown page by page in the agent's own window.

A file profile (or a file in a program row) that is a document is not handed
to its Windows default app: such apps open windowed, on the screen they
like, and cannot be paged from the service. Instead the agent shows the pages
itself, full screen on the profile's monitor (the same numbering as images),
and pages through them on POST /document or a key in its window.

- PDF: rendered with pypdfium2 (a pip wheel, nothing else to install).
- PowerPoint and Word files: converted to PDF once, then shown as a PDF.
  The installed Office does it (COM, from a hidden PowerShell: no Python
  package needed), else LibreOffice (soffice --headless --convert-to pdf).
  The PDF is kept in a cache keyed by path, size and change time, so a
  document shown again comes up at once.

Nothing here touches the screen: the display agent (show_image_agent.py)
puts the rendered pages into its window. When nothing can render a document
(no pypdfium2, neither Office nor LibreOffice), DocumentError says why and
the agent opens the file with its default app as before.
"""

from __future__ import annotations

import base64
import hashlib
import importlib
import json
import logging
import os
import shutil
import site
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path, PureWindowsPath

from PIL import Image

logger = logging.getLogger("display-agent")

PDF_SUFFIXES = {".pdf"}
POWERPOINT_SUFFIXES = {".ppt", ".pptx", ".pptm", ".pps", ".ppsx", ".ppsm", ".odp"}
WORD_SUFFIXES = {".doc", ".docx", ".docm", ".odt", ".rtf"}
CONVERT_TIMEOUT_SECONDS = 180
MAX_CACHED_PDFS = 40
MAX_PAGES = 2000
# Rendered pages kept per document (the current one and its neighbours).
PAGE_CACHE = 5


class DocumentError(Exception):
    """The document cannot be shown by the agent itself (the reason is logged)."""


def document_type(path) -> str | None:
    """"pdf", "powerpoint", "word" or None for anything else."""
    suffix = PureWindowsPath(str(path)).suffix.lower()
    if suffix in PDF_SUFFIXES:
        return "pdf"
    if suffix in POWERPOINT_SUFFIXES:
        return "powerpoint"
    if suffix in WORD_SUFFIXES:
        return "word"
    return None


# -- pypdfium2 ---------------------------------------------------------------------
# The Update key replaces the agent's files but runs no pip; an agent that
# lacks pypdfium2 installs its requirements itself once per start (in the
# background, see install_missing_requirements).

_pdfium = None
_pdfium_lock = threading.Lock()  # pdfium is not thread safe: every call under this lock


def pdfium():
    """The pypdfium2 module, or None when it is not installed."""
    global _pdfium
    if _pdfium is None:
        try:
            importlib.invalidate_caches()
            _pdfium = importlib.import_module("pypdfium2")
        except ImportError:
            return None
    return _pdfium


PIP_TIMEOUT_SECONDS = 600
PIP_RETRY_SECONDS = 3600.0       # after a failed pip run (same agent version): one hour,
PIP_RETRY_MAX_SECONDS = 86400.0  # doubling each time, up to a day
# The last pip run for /info: {"state": "ok" | "failed" | "running" | "waiting" | "not needed", ...}
install_status: dict = {"state": "not needed"}


def _read_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def install_missing_requirements(requirements: Path, version: str, state_file: Path, run=None,
                                 now=None) -> bool | None:
    """pip install -r requirements.txt when pypdfium2 is missing (background,
    with a timeout). Once per agent version; after a failure only again
    after PIP_RETRY_SECONDS (doubling). None when nothing was run, else
    whether pypdfium2 is importable afterwards. The outcome is logged and
    kept in install_status (/info)."""
    global install_status
    if pdfium() is not None:
        install_status = {"state": "not needed"}
        return None
    now = time.time() if now is None else now
    state = _read_state(state_file)
    if state.get("version") == version:
        failures = int(state.get("failures") or 0)
        if state.get("ok"):
            # Installed for this version, yet not importable: not again until a new version.
            install_status = {"state": "failed", "reason": "installed but not importable", "version": version}
            return None
        if failures:
            wait = min(PIP_RETRY_MAX_SECONDS, PIP_RETRY_SECONDS * 2 ** (failures - 1))
            if now < float(state.get("last") or 0) + wait:
                install_status = {"state": "waiting", "failures": failures, "reason": state.get("reason", ""),
                                  "retry_at": float(state.get("last") or 0) + wait}
                logger.info("pypdfium2 missing; pip failed %s time(s) for %s, next try in %.0f s", failures,
                            version, float(state.get("last") or 0) + wait - now)
                return None
    else:
        failures = 0
    run = run or subprocess.run
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", str(requirements)]
    logger.info("pypdfium2 missing: installing the agent's requirements (%s)", " ".join(command))
    install_status = {"state": "running", "started": now}
    reason = ""
    try:
        finished = run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=PIP_TIMEOUT_SECONDS,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        if finished.returncode != 0:
            reason = f"pip exit {finished.returncode}: " + \
                (finished.stderr or b"").decode("utf-8", "replace").strip()[-300:]
    except subprocess.TimeoutExpired:
        reason = f"pip took longer than {PIP_TIMEOUT_SECONDS} s"
    except (OSError, subprocess.SubprocessError) as exc:
        reason = f"pip could not be started: {exc}"
    # A user installation goes to a folder that may not have existed at start.
    try:
        user_site = site.getusersitepackages()
        if user_site and Path(user_site).is_dir() and user_site not in sys.path:
            site.addsitedir(user_site)
    except Exception:  # noqa: BLE001 - only a second place to look
        pass
    available = pdfium() is not None
    if available:
        install_status = {"state": "ok", "version": version}
        logger.info("pypdfium2 installed by pip")
        record = {"version": version, "ok": True, "last": now}
    else:
        reason = reason or "pip ran, pypdfium2 still not importable"
        failures += 1
        install_status = {"state": "failed", "failures": failures, "reason": reason}
        logger.warning("pypdfium2 still missing after pip (%s); documents open with their default app", reason)
        record = {"version": version, "ok": False, "failures": failures, "last": now, "reason": reason}
    try:
        state_file.parent.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(record), encoding="utf-8")
    except OSError:
        logger.exception("Could not note the pip result")
    return available


# -- converting PowerPoint and Word to PDF ---------------------------------------------

# Paths go in through environment variables, never into the script text.
_OFFICE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$source = $env:MIXER_DOC_SOURCE
$target = $env:MIXER_DOC_TARGET
if ($env:MIXER_DOC_KIND -eq 'powerpoint') {
    $app = New-Object -ComObject PowerPoint.Application
    try {
        # ReadOnly, Untitled, WithWindow = false: nothing appears on screen.
        $presentation = $app.Presentations.Open($source, -1, 0, 0)
        try { $presentation.SaveAs($target, 32) } finally { $presentation.Close() }
    } finally {
        if ($app.Presentations.Count -eq 0) { $app.Quit() }
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
    }
} else {
    $app = New-Object -ComObject Word.Application
    $app.Visible = $false
    $app.DisplayAlerts = 0
    try {
        # ConfirmConversions = false, ReadOnly = true, AddToRecentFiles = false
        $document = $app.Documents.Open($source, $false, $true, $false)
        try { $document.ExportAsFixedFormat($target, 17) } finally { $document.Close(0) }
    } finally {
        if ($app.Documents.Count -eq 0) { $app.Quit() }
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
    }
}
"""


def _hidden(run, command, **kwargs):
    return run(command, stdin=subprocess.DEVNULL, capture_output=True, timeout=CONVERT_TIMEOUT_SECONDS,
               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)


def convert_with_office(kind: str, source: Path, target: Path, run=None) -> None:
    """PowerPoint / Word (COM) saves the file as PDF. Raises DocumentError."""
    run = run or subprocess.run
    encoded = base64.b64encode(_OFFICE_SCRIPT.encode("utf-16-le")).decode("ascii")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-EncodedCommand", encoded]
    environment = {**os.environ, "MIXER_DOC_KIND": kind, "MIXER_DOC_SOURCE": str(source),
                   "MIXER_DOC_TARGET": str(target)}
    try:
        finished = _hidden(run, command, env=environment)
    except subprocess.TimeoutExpired as exc:
        raise DocumentError(f"Office took longer than {CONVERT_TIMEOUT_SECONDS} s") from exc
    except OSError as exc:
        raise DocumentError(f"PowerShell could not be started: {exc}") from exc
    if finished.returncode != 0 or not target.is_file():
        error = (finished.stderr or b"").decode("utf-8", "replace").strip()
        raise DocumentError(f"Office could not convert it: {error[:300] or finished.returncode}")


def find_libreoffice(env=None, exists=None) -> str | None:
    """soffice.exe of an installed LibreOffice, or None."""
    env = os.environ if env is None else env
    exists = exists or os.path.isfile
    places = [env.get("LIBREOFFICE_PATH", "")]
    for variable in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        if env.get(variable):
            places.append(str(PureWindowsPath(env[variable], "LibreOffice", "program", "soffice.exe")))
    places += [shutil.which("soffice.exe") or "", shutil.which("soffice") or ""]
    return next((place for place in places if place and exists(place)), None)


def convert_with_libreoffice(soffice: str, source: Path, target: Path, run=None) -> None:
    """LibreOffice converts the file to PDF. Raises DocumentError."""
    run = run or subprocess.run
    with tempfile.TemporaryDirectory(prefix="mixer-doc-") as folder:
        # Its own profile: a LibreOffice the user has open does not block it.
        profile = Path(folder, "profile").as_uri()
        command = [soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore", "--nologo",
                   "--convert-to", "pdf", "--outdir", folder, str(source)]
        try:
            finished = _hidden(run, command)
        except subprocess.TimeoutExpired as exc:
            raise DocumentError(f"LibreOffice took longer than {CONVERT_TIMEOUT_SECONDS} s") from exc
        except OSError as exc:
            raise DocumentError(f"LibreOffice could not be started: {exc}") from exc
        produced = Path(folder, source.stem + ".pdf")
        if finished.returncode != 0 or not produced.is_file():
            error = (finished.stderr or b"").decode("utf-8", "replace").strip()
            raise DocumentError(f"LibreOffice could not convert it: {error[:300] or finished.returncode}")
        shutil.move(str(produced), str(target))


def cache_name(source: Path) -> str:
    """The cached PDF's name: changes with the file's path, size and time."""
    stat = source.stat()
    key = f"{str(source).lower()}|{stat.st_size}|{stat.st_mtime_ns}"
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:24] + ".pdf"


def _trim_cache(folder: Path, keep: Path) -> None:
    pdfs = sorted(folder.glob("*.pdf"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in pdfs[MAX_CACHED_PDFS:]:
        if old != keep:
            try:
                old.unlink()
            except OSError:
                pass


_convert_lock = threading.Lock()  # one conversion at a time (Office does not like two)


def pdf_for(source: Path, cache: Path, office=None, libreoffice=None, find_soffice=None) -> Path:
    """The PDF to show for a document: a PDF itself, else its converted copy
    (from the cache, or converted now by Office, then LibreOffice). Raises
    DocumentError when neither could do it."""
    kind = document_type(source)
    if kind == "pdf":
        return source
    if kind is None:
        raise DocumentError(f"{source.suffix} is not a document")
    office = office or convert_with_office
    libreoffice = libreoffice or convert_with_libreoffice
    find_soffice = find_soffice or find_libreoffice
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / cache_name(source)
    with _convert_lock:
        if target.is_file():
            os.utime(target)  # recently used: kept by _trim_cache
            logger.info("Document %s: converted copy from the cache", source)
            return target
        partial = target.with_suffix(".part.pdf")
        reasons = []
        attempts = [("Office", lambda: office(kind, source, partial))]
        soffice = find_soffice()
        if soffice:
            attempts.append(("LibreOffice", lambda: libreoffice(soffice, source, partial)))
        for name, attempt in attempts:
            try:
                attempt()
                os.replace(partial, target)
                logger.info("Document %s converted to PDF by %s", source, name)
                _trim_cache(cache, target)
                return target
            except DocumentError as exc:
                reasons.append(f"{name}: {exc}")
                logger.warning("Document %s: %s could not convert it: %s", source, name, exc)
            finally:
                try:
                    partial.unlink()
                except OSError:
                    pass
    if not soffice:
        reasons.append("LibreOffice: not installed")
    raise DocumentError("; ".join(reasons))


# -- the pages ---------------------------------------------------------------------

class PdfPages:
    """A PDF's pages, rendered to fit a window (letterboxed, never cut)."""

    def __init__(self, path: Path, module=None) -> None:
        self._module = module or pdfium()
        if self._module is None:
            raise DocumentError("pypdfium2 is not installed")
        with _pdfium_lock:
            try:
                self._document = self._module.PdfDocument(str(path))
                self.count = min(len(self._document), MAX_PAGES)
            except Exception as exc:  # noqa: BLE001 - pdfium raises its own errors
                raise DocumentError(f"the PDF could not be read: {exc}") from exc
        if self.count < 1:
            self.close()
            raise DocumentError("the PDF has no pages")
        self._rendered: dict[tuple, Image.Image] = {}

    def render(self, index: int, width: int, height: int) -> Image.Image:
        """Page ``index`` (from 0) as large as fits into width x height."""
        key = (index, width, height)
        if key in self._rendered:
            return self._rendered[key]
        with _pdfium_lock:
            page = self._document[index]
            try:
                page_width, page_height = page.get_size()
                scale = min(width / max(page_width, 1.0), height / max(page_height, 1.0))
                image = page.render(scale=max(scale, 0.01)).to_pil().convert("RGB")
            finally:
                page.close()
        if len(self._rendered) >= PAGE_CACHE:
            self._rendered.pop(next(iter(self._rendered)))
        self._rendered[key] = image
        return image

    def close(self) -> None:
        with _pdfium_lock:
            try:
                self._document.close()
            except Exception:  # noqa: BLE001 - closing what may be broken
                pass


def step(page: int, count: int, action: str, target: int | None = None) -> int:
    """The page (from 0) after next / prev / first / last / goto (1-based
    ``target``); it stays within the document."""
    if action == "next":
        page += 1
    elif action == "prev":
        page -= 1
    elif action == "first":
        page = 0
    elif action == "last":
        page = count - 1
    elif action == "goto" and target is not None:
        page = target - 1
    return max(0, min(count - 1, page))


DOCUMENT_ACTIONS = ("next", "prev", "first", "last", "goto", "status")


def valid_document_request(body) -> bool:
    """POST /document {"action", optional "page" (goto, from 1), optional "id"}."""
    if not isinstance(body, dict) or not set(body) <= {"action", "page", "id"}:
        return False
    if body.get("action") not in DOCUMENT_ACTIONS:
        return False
    if body["action"] == "goto":
        page = body.get("page")
        if not isinstance(page, int) or isinstance(page, bool) or not 1 <= page <= MAX_PAGES:
            return False
    elif "page" in body:
        return False
    if "id" in body:
        profile_id = body["id"]
        if not isinstance(profile_id, str) or not profile_id.replace("-", "").replace("_", "").isalnum():
            return False
    return True


def capabilities(find_soffice=None) -> dict:
    """For /info: what this agent can show documents with."""
    find_soffice = find_soffice or find_libreoffice
    return {"pdf": pdfium() is not None, "libreoffice": find_soffice(), "pip": dict(install_status)}
