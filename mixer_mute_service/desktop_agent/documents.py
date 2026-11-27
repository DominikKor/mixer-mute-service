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
CONVERT_TIMEOUT_SECONDS = 150  # one attempt (Office or LibreOffice)
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


# Only this one package (keep in step with requirements.txt): never upgrades
# Pillow while the running agent has it loaded.
PDFIUM_REQUIREMENT = "pypdfium2==5.15.2"
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


def install_missing_requirements(version: str, state_file: Path, run=None, now=None) -> bool | None:
    """pip install PDFIUM_REQUIREMENT when pypdfium2 is missing (background,
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
    command = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check", "-q", PDFIUM_REQUIREMENT]
    logger.info("pypdfium2 missing: installing it (%s)", " ".join(command))
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

class DocumentCancelled(DocumentError):
    """Closed or started again while it waited for its turn to be converted."""


# Paths go in through environment variables, never into the script text.
# The Office process this conversion started (none when the user's own
# Office was already running: COM then talks to that one) is written to
# MIXER_DOC_PIDFILE, so a conversion that hangs can end exactly that one.
_OFFICE_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
$source = $env:MIXER_DOC_SOURCE
$target = $env:MIXER_DOC_TARGET
$name = if ($env:MIXER_DOC_KIND -eq 'powerpoint') { 'POWERPNT' } else { 'WINWORD' }
$before = @(Get-Process -Name $name -ErrorAction SilentlyContinue | ForEach-Object { $_.Id })
if ($env:MIXER_DOC_KIND -eq 'powerpoint') {
    $app = New-Object -ComObject PowerPoint.Application
} else {
    $app = New-Object -ComObject Word.Application
}
$started = @(Get-Process -Name $name -ErrorAction SilentlyContinue | Where-Object { $before -notcontains $_.Id } |
    ForEach-Object { $_.Id })
if ($env:MIXER_DOC_PIDFILE) { Set-Content -LiteralPath $env:MIXER_DOC_PIDFILE -Value ($started -join ',') }
if ($env:MIXER_DOC_KIND -eq 'powerpoint') {
    try {
        $app.DisplayAlerts = 1  # ppAlertsNone
        # ReadOnly, Untitled, WithWindow = false: nothing appears on screen.
        $presentation = $app.Presentations.Open($source, -1, 0, 0)
        try { $presentation.SaveAs($target, 32) } finally { $presentation.Close() }
    } finally {
        if ($app.Presentations.Count -eq 0) { $app.Quit() }
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
    }
} else {
    $app.Visible = $false
    $app.DisplayAlerts = 0
    try {
        # ConfirmConversions = false, ReadOnly = true, AddToRecentFiles = false,
        # PasswordDocument: a protected file fails at once instead of asking.
        $document = $app.Documents.Open($source, $false, $true, $false, 'mixer-no-password')
        try { $document.ExportAsFixedFormat($target, 17) } finally { $document.Close(0) }
    } finally {
        if ($app.Documents.Count -eq 0) { $app.Quit() }
        [void][Runtime.InteropServices.Marshal]::ReleaseComObject($app)
    }
}
"""
_OFFICE_IMAGES = {"powerpoint": "POWERPNT.EXE", "word": "WINWORD.EXE"}


def _taskkill(pid: int, run=None) -> None:
    run = run or subprocess.run
    run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL, capture_output=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _image_of(pid: int, run=None) -> str:
    """The executable name of a running process ("" when gone)."""
    run = run or subprocess.run
    try:
        finished = run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"], stdin=subprocess.DEVNULL,
                       capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        return ""
    line = (finished.stdout or b"").decode("utf-8", "replace").strip().splitlines()
    return line[0].split(",")[0].strip('"') if line and line[0].startswith('"') else ""


def end_started_office(pidfile: Path, kind: str, image_of=None, kill=None) -> list:
    """After a timeout: end the Office process this conversion started (from
    the pid file), only if that pid still is POWERPNT.EXE / WINWORD.EXE. An
    Office the user opened is never in the file. Returns the pids ended."""
    image_of = image_of or _image_of
    kill = kill or _taskkill
    try:
        text = pidfile.read_text(encoding="utf-8-sig").strip()
    except OSError:
        return []
    ended = []
    for part in text.replace(" ", "").split(","):
        if not part.isdigit():
            continue
        pid = int(part)
        if image_of(pid).upper() != _OFFICE_IMAGES[kind]:
            continue
        try:
            kill(pid)
            ended.append(pid)
            logger.warning("Ended %s (pid %s): the conversion it started hung", _OFFICE_IMAGES[kind], pid)
        except (OSError, subprocess.SubprocessError):
            logger.exception("Could not end pid %s", pid)
    return ended


def _run_bounded(command, timeout: float, **kwargs):
    """subprocess.run, but a timeout ends the whole process tree (soffice.exe
    starts soffice.bin, which a plain kill would leave behind)."""
    process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), **kwargs)
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        if os.name == "nt":
            try:
                _taskkill(process.pid)
            except (OSError, subprocess.SubprocessError):
                pass
        process.kill()
        process.communicate()
        raise
    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)


def convert_with_office(kind: str, source: Path, target: Path, timeout: float = CONVERT_TIMEOUT_SECONDS,
                        run=None, end_office=None) -> None:
    """PowerPoint / Word (COM) saves the file as PDF. Raises DocumentError;
    on a timeout the Office process it started is ended."""
    run = run or _run_bounded
    end_office = end_office or end_started_office
    encoded = base64.b64encode(_OFFICE_SCRIPT.encode("utf-16-le")).decode("ascii")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
               "-EncodedCommand", encoded]
    with tempfile.TemporaryDirectory(prefix="mixer-office-") as folder:
        pidfile = Path(folder, "office.pid")
        environment = {**os.environ, "MIXER_DOC_KIND": kind, "MIXER_DOC_SOURCE": str(source),
                       "MIXER_DOC_TARGET": str(target), "MIXER_DOC_PIDFILE": str(pidfile)}
        try:
            finished = run(command, timeout=timeout, env=environment)
        except subprocess.TimeoutExpired as exc:
            end_office(pidfile, kind)
            raise DocumentError(f"Office took longer than {timeout:.0f} s") from exc
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


def convert_with_libreoffice(soffice: str, source: Path, target: Path, timeout: float = CONVERT_TIMEOUT_SECONDS,
                             run=None) -> None:
    """LibreOffice converts the file to PDF. Raises DocumentError."""
    run = run or _run_bounded
    with tempfile.TemporaryDirectory(prefix="mixer-doc-") as folder:
        # Its own profile: a LibreOffice the user has open does not block it.
        profile = Path(folder, "profile").as_uri()
        command = [soffice, f"-env:UserInstallation={profile}", "--headless", "--norestore", "--nologo",
                   "--convert-to", "pdf", "--outdir", folder, str(source)]
        try:
            finished = run(command, timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            raise DocumentError(f"LibreOffice took longer than {timeout:.0f} s") from exc
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


MIN_ATTEMPT_SECONDS = 5.0  # less time left than this: no further attempt


def pdf_for(source: Path, cache: Path, office=None, libreoffice=None, find_soffice=None,
            deadline: float | None = None, cancelled=None) -> Path:
    """The PDF to show for a document: a PDF itself, else its converted copy
    (from the cache, or converted now by Office, then LibreOffice). Everything
    ends by ``deadline`` (time.monotonic()), waiting for another conversion
    included; ``cancelled()`` true once it is its turn: DocumentCancelled.
    Raises DocumentError when neither could do it."""
    kind = document_type(source)
    if kind == "pdf":
        return source
    if kind is None:
        raise DocumentError(f"{source.suffix} is not a document")
    office = office or convert_with_office
    libreoffice = libreoffice or convert_with_libreoffice
    find_soffice = find_soffice or find_libreoffice
    deadline = deadline if deadline is not None else time.monotonic() + 2 * CONVERT_TIMEOUT_SECONDS
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / cache_name(source)
    if not _convert_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise DocumentError("another document was still being converted")
    try:
        if cancelled is not None and cancelled():
            raise DocumentCancelled("closed or started again while waiting")
        if target.is_file():
            os.utime(target)  # recently used: kept by _trim_cache
            logger.info("Document %s: converted copy from the cache", source)
            return target
        partial = target.with_suffix(".part.pdf")
        reasons = []
        attempts = [("Office", lambda left: office(kind, source, partial, timeout=left))]
        soffice = find_soffice()
        if soffice:
            attempts.append(("LibreOffice", lambda left: libreoffice(soffice, source, partial, timeout=left)))
        for name, attempt in attempts:
            left = min(CONVERT_TIMEOUT_SECONDS, deadline - time.monotonic())
            if left < MIN_ATTEMPT_SECONDS:
                reasons.append(f"{name}: no time left")
                continue
            try:
                attempt(left)
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
    finally:
        _convert_lock.release()
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
        self.closed = False

    def render(self, index: int, width: int, height: int) -> Image.Image:
        """Page ``index`` (from 0) as large as fits into width x height."""
        key = (index, width, height)
        if key in self._rendered:
            return self._rendered[key]
        with _pdfium_lock:
            if self.closed:
                raise DocumentError("the document was closed")
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
            if self.closed:
                return
            self.closed = True
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
