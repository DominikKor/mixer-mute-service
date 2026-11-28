"""Authenticated LAN agent that displays configured images or launches profiles,
and shows FoilPresenter songs slide by slide (read from the local library)."""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import queue
import getpass
import sys
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path, PureWindowsPath

import glob
import re
import tkinter as tk
import tkinter.font as tkfont
from PIL import Image, ImageOps, ImageTk

# Documents shown page by page (documents.py next to this file). Without it
# (a partial update) documents open with their default app as before.
try:
    from . import documents  # imported as desktop_agent.show_image_agent (tests)
except ImportError:
    try:
        import documents  # run as a script: the file next to this one
    except ImportError:
        documents = None


HOST = "0.0.0.0"
PORT = 8766
requests: queue.Queue[dict] = queue.Queue()

# Self-update: the service sends the agent files of its own release (POST
# /update); they replace ours, version.txt records the version (POST /info
# reports it), and the agent starts again through its scheduled task.
AGENT_DIR = Path(__file__).resolve().parent
VERSION_FILE = AGENT_DIR / "version.txt"
MAX_UPDATE_BYTES = 4 * 1024 * 1024
_UPDATE_FILE_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\.(?:py|txt|ps1)$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9._+-]{1,80}$")
MAX_SONG_FILES = 5000
MAX_SONG_FILE_BYTES = 512 * 1024
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# A new song's file name, as the service makes it (no folders, no odd characters).
_SONG_NAME_RE = re.compile(r"^[A-Za-z0-9 ._()!'+-]{1,120}\.foil$")
MAX_MONITORS = 5
logger = logging.getLogger("display-agent")
# Names of variables filled in from the user's environment at start (/info).
RESTORED_ENV: list = []
# When this agent process started (/info): the service sees a restart by it.
STARTED_AT = time.time()


def _monitors() -> list[tuple[tuple[int, int, int, int], tuple[int, int, int, int], str, int]]:
    """All monitors as (full bounds, work area, device name such as
    \\\\.\\DISPLAY2), position in Windows' own order), sorted left to right,
    top to bottom: "monitor N" of a profile is the N-th of them (images,
    windows and videos alike). Windows' own order is what mpv numbers its
    screens by (--screen=N on Windows: the N-th EnumDisplayMonitors entry,
    from 0; its --screen-name is not implemented there)."""
    if os.name != "nt":
        raise RuntimeError("This display agent is intended for Windows")

    import ctypes
    from ctypes import wintypes

    class MONITORINFOEXW(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                    ("szDevice", wintypes.WCHAR * 32)]

    user32 = ctypes.windll.user32
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
    monitors: list[tuple[tuple[int, int, int, int], tuple[int, int, int, int], str, int]] = []

    @ctypes.WINFUNCTYPE(
        ctypes.c_int,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    def callback(monitor, _dc, rect, _data):
        bounds = rect.contents
        full = (bounds.left, bounds.top, bounds.right, bounds.bottom)
        info = MONITORINFOEXW(cbSize=ctypes.sizeof(MONITORINFOEXW))
        raw = len(monitors)  # this call's place in Windows' order
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            work = info.rcWork
            monitors.append((full, (work.left, work.top, work.right, work.bottom), info.szDevice, raw))
        else:
            monitors.append((full, full, "", raw))
        return 1

    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise OSError("Could not enumerate displays")
    return _order_monitors(monitors)


def _order_monitors(monitors: list) -> list:
    """Windows' order (each entry keeps its place in it) to ours: left to
    right, top to bottom."""
    return sorted(monitors, key=lambda entry: (entry[0][0], entry[0][1]))


def _monitor_areas(display_number: int) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """The monitor's full bounds and its work area (without the taskbar)."""
    monitors = _monitors()
    if display_number < 1 or display_number > len(monitors):
        raise RuntimeError(f"Display {display_number} not found; detected {len(monitors)}")
    return monitors[display_number - 1][:2]


def monitor_device_name(display_number: int) -> str | None:
    """The device name of "monitor N" (e.g. \\\\.\\DISPLAY2), None if unknown."""
    monitors = _monitors()
    if display_number < 1 or display_number > len(monitors):
        raise RuntimeError(f"Display {display_number} not found; detected {len(monitors)}")
    return monitors[display_number - 1][2] or None


def mpv_screen(display_number: int) -> tuple[int, str | None]:
    """mpv's --screen number of "monitor N" (its place in Windows' order,
    from 0) and its device name (for the log)."""
    monitors = _monitors()
    if display_number < 1 or display_number > len(monitors):
        raise RuntimeError(f"Display {display_number} not found; detected {len(monitors)}")
    entry = monitors[display_number - 1]
    return entry[3], entry[2] or None


def monitor_bounds(display_number: int) -> tuple[int, int, int, int]:
    return _monitor_areas(display_number)[0]


def monitor_work_area(display_number: int) -> tuple[int, int, int, int]:
    return _monitor_areas(display_number)[1]


def _invisible_border(hwnd) -> tuple[int, int, int, int]:
    """Left, top, right, bottom width of the invisible resize border Windows
    10/11 put around a framed window (0s where there is none)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.PhysicalToLogicalPointForPerMonitorDPI.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    dwm = ctypes.windll.dwmapi
    dwm.DwmGetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    outer = wintypes.RECT()
    visible = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(outer)):
        return 0, 0, 0, 0
    # DWMWA_EXTENDED_FRAME_BOUNDS: the visible frame, always in physical pixels.
    if dwm.DwmGetWindowAttribute(hwnd, 9, ctypes.byref(visible), ctypes.sizeof(visible)) != 0:
        return 0, 0, 0, 0
    top_left = wintypes.POINT(visible.left, visible.top)
    bottom_right = wintypes.POINT(visible.right, visible.bottom)
    user32.PhysicalToLogicalPointForPerMonitorDPI(hwnd, ctypes.byref(top_left))
    user32.PhysicalToLogicalPointForPerMonitorDPI(hwnd, ctypes.byref(bottom_right))
    return (
        max(0, top_left.x - outer.left),
        max(0, top_left.y - outer.top),
        max(0, outer.right - bottom_right.x),
        max(0, outer.bottom - bottom_right.y),
    )


def _visible_windows() -> dict[int, int]:
    """Visible, unowned top-level windows with a title: hwnd -> process id."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found: dict[int, int] = {}

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _data):
        if user32.IsWindowVisible(hwnd) and not user32.GetWindow(hwnd, 4) and user32.GetWindowTextLengthW(hwnd):
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            found[hwnd] = owner.value
        return True

    user32.EnumWindows(callback, 0)
    return found


def _choose_window(windows: dict[int, int], process_id: int, before: set, foreground, iconic) -> int | None:
    """The started process's window; otherwise a window that appeared since
    the start (a launcher handed over, or the app was already running)."""
    candidates = [h for h, pid in windows.items() if process_id and pid == process_id]
    if not candidates:
        candidates = [h for h, pid in windows.items() if h not in before and pid != os.getpid()]
    if foreground in candidates:
        return foreground
    for hwnd in candidates:
        if not iconic(hwnd):
            return hwnd
    return candidates[0] if candidates else None


def _find_window(process_id: int, before: set):
    import ctypes

    user32 = ctypes.windll.user32
    return _choose_window(_visible_windows(), process_id, before, user32.GetForegroundWindow(), user32.IsIconic)


def _shell_open_file(path: Path) -> int:
    import ctypes
    from ctypes import wintypes

    class ShellExecuteInfo(ctypes.Structure):
        _fields_ = [
            ("cbSize", wintypes.DWORD),
            ("fMask", wintypes.ULONG),
            ("hwnd", wintypes.HWND),
            ("lpVerb", wintypes.LPCWSTR),
            ("lpFile", wintypes.LPCWSTR),
            ("lpParameters", wintypes.LPCWSTR),
            ("lpDirectory", wintypes.LPCWSTR),
            ("nShow", ctypes.c_int),
            ("hInstApp", wintypes.HANDLE),
            ("lpIDList", ctypes.c_void_p),
            ("lpClass", wintypes.LPCWSTR),
            ("hkeyClass", wintypes.HANDLE),
            ("dwHotKey", wintypes.DWORD),
            ("hIcon", wintypes.HANDLE),
            ("hProcess", wintypes.HANDLE),
        ]

    info = ShellExecuteInfo()
    info.cbSize = ctypes.sizeof(info)
    info.fMask = 0x40
    info.lpVerb = "open"
    info.lpFile = str(path)
    info.lpDirectory = str(path.parent)
    info.nShow = 1
    if not ctypes.windll.shell32.ShellExecuteExW(ctypes.byref(info)):
        raise ctypes.WinError()
    if not info.hProcess:
        return 0  # handed to a running app: its new window is found instead
    try:
        return ctypes.windll.kernel32.GetProcessId(info.hProcess)
    finally:
        ctypes.windll.kernel32.CloseHandle(info.hProcess)


def _window_position(
    bounds: tuple[int, int, int, int], width: int, height: int, position: str
) -> tuple[int, int]:
    left, top, right, bottom = bounds
    positions = {
        "top-left": (left, top),
        "top-right": (right - width, top),
        "bottom-left": (left, bottom - height),
        "bottom-right": (right - width, bottom - height),
        "center": (
            left + (right - left - width) // 2,
            top + (bottom - top - height) // 2,
        ),
    }
    try:
        return positions[position]
    except KeyError as exc:
        raise ValueError(f"Unsupported window position: {position}") from exc


def _framed_window_position(
    area: tuple[int, int, int, int], width: int, height: int,
    invisible: tuple[int, int, int, int], position: str,
) -> tuple[int, int]:
    """Where a window of this outer size goes so its visible frame sits in the
    corner, as when it is snapped there (the invisible border lies outside)."""
    inv_left, inv_top, inv_right, inv_bottom = invisible
    x, y = _window_position(area, width - inv_left - inv_right, height - inv_top - inv_bottom, position)
    return x - inv_left, y - inv_top


def _place_process_window(
    process_id: int, before: set, monitor: int, fullscreen: bool, position: str, remember_as: str | None = None
) -> None:
    """Move the started app's window to its place, and keep it there while
    the app settles: many apps centre or restore their window right after
    showing it, or replace a splash screen by the main window."""
    bounds = monitor_bounds(monitor)
    placed: dict[int, tuple[int, int, int, int]] = {}
    remembered: list[int] = []
    try:
        _place_loop(process_id, before, bounds, fullscreen, position, monitor, placed, remember_as, remembered)
    finally:
        if remember_as and not remembered:
            _remember_unknown(remember_as)


def _remember_if_own(profile_id: str, hwnd: int, process_id: int, window_pid=None) -> bool:
    """Only a window of the started process itself is closed later; one
    found otherwise (a new window of an app that was already running, a
    launcher's) could belong to anything, OBS included."""
    window_pid = window_pid or _window_process_id
    if not process_id or window_pid(hwnd) != process_id:
        return False
    _remember_window(profile_id, hwnd, process_id, window_pid)
    return True


def _place_loop(process_id, before, bounds, fullscreen, position, monitor, placed, remember_as, remembered) -> None:
    deadline = time.monotonic() + 20
    settle_until = None
    corrections = 0
    while True:
        hwnd = _find_window(process_id, before)
        if hwnd and hwnd not in placed:
            # At once, so a /close coming while it settles finds it.
            if remember_as and _remember_if_own(remember_as, hwnd, process_id):
                remembered.append(hwnd)
                if _close_if_cancelled(remember_as):
                    return
            target = _apply_placement(hwnd, bounds, fullscreen, position)
            if target:
                placed[hwnd] = target
                settle_until = time.monotonic() + PLACEMENT_SETTLE_SECONDS
                logger.info("Positioned window pid=%s hwnd=%s monitor=%s fullscreen=%s position=%s rect=%s",
                            process_id, hwnd, monitor, fullscreen, position, target)
        elif hwnd and corrections < 10 and not _rect_close(_window_rect(hwnd), placed[hwnd]):
            corrections += 1
            logger.info("Window hwnd=%s moved by the app to %s; placing it again", hwnd, _window_rect(hwnd))
            _apply_placement(hwnd, bounds, fullscreen, position, placed[hwnd])
        now = time.monotonic()
        if settle_until is not None and now >= settle_until:
            return
        if settle_until is None and now >= deadline:
            raise RuntimeError("Application window was not found within 20 seconds")
        time.sleep(0.25)


PLACEMENT_SETTLE_SECONDS = 4.0


def _rect_close(rect, target, tolerance: int = 4) -> bool:
    return rect is None or all(abs(a - b) <= tolerance for a, b in zip(rect, target))


def _window_rect(hwnd) -> tuple[int, int, int, int] | None:
    """x, y, width, height; None once the window is gone."""
    import ctypes
    from ctypes import wintypes

    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top


def _apply_placement(hwnd, bounds, fullscreen: bool, position: str, target=None):
    """Place the window; returns the rect it was given, None if it is gone."""
    import ctypes

    user32 = ctypes.windll.user32
    left, top, right, bottom = bounds
    if target is None:
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE: a maximised window cannot be moved
        current = _window_rect(hwnd)
        if current is None:
            return None
        if fullscreen:
            width, height = right - left, bottom - top
        else:
            width, height = min(current[2], right - left), min(current[3], bottom - top)
        x, y = _window_position(bounds, width, height, position)
        target = (x, y, width, height)
    if fullscreen:
        style = user32.GetWindowLongW(hwnd, -16)
        user32.SetWindowLongW(hwnd, -16, (style & ~0x00CF0000) | 0x80000000)
        insert_after, flags = -1, 0x0040 | 0x0020
    else:
        insert_after, flags = -2, 0x0040
    if not user32.SetWindowPos(hwnd, insert_after, *target, flags):
        if not user32.IsWindow(hwnd):
            return None
        raise ctypes.WinError()
    return target


def resolve_path(path: str, folder: bool = False, suffixes: tuple = ()) -> Path:
    """A configured path, where "*" stands for any part of a name that is not
    known (any characters, within one folder level; Windows names never
    contain "*"). With several matches the most recently changed one wins.
    Raises FileNotFoundError when nothing matches."""
    target = Path(path)
    if "*" not in path:
        return target
    if not target.is_absolute():
        raise FileNotFoundError(path)
    # Only "*" is a wildcard: "?" and "[" in a name are meant literally.
    pattern = re.sub(r"[?\[]", lambda m: f"[{m.group(0)}]", path)
    matches = [Path(found) for found in glob.glob(pattern)]
    matches = [p for p in matches if (p.is_dir() if folder else p.is_file())
               and (not suffixes or p.suffix.lower() in suffixes)]
    if not matches:
        raise FileNotFoundError(path)
    chosen = max(matches, key=lambda p: (p.stat().st_mtime, str(p)))
    if len(matches) > 1:
        logger.info("Path %s matches %s entries; using the newest: %s", path, len(matches), chosen)
    return chosen


# Starting these (directly or with "open with its default app") runs code.
RUNNABLE_SUFFIXES = {".exe", ".com", ".bat", ".cmd", ".msi", ".scr", ".ps1", ".vbs", ".vbe",
                     ".js", ".jse", ".wsf", ".wsh", ".lnk", ".url", ".hta", ".cpl", ".pif"}


def runs_from_network_share(target: Path) -> bool:
    """A runnable file on a network share: never started, whatever the
    profile kind (anyone who can write to the share could swap it)."""
    text = str(target)
    on_share = text.startswith("\\\\") or text.startswith("//")
    return on_share and target.suffix.lower() in RUNNABLE_SUFFIXES


def _launch_profile(profile: dict) -> None:
    document = False
    try:
        path = Path(profile["path"])
        if profile["kind"] == "file" and _open_document(profile, path):
            document = True  # its /display answer comes from the tk thread (or the cancel)
            return
        before = set(_visible_windows())
        mpv = _video_player(profile, path)
        if profile["kind"] == "program":
            process_id = subprocess.Popen(
                [str(path), *profile["args"]], cwd=str(path.parent), shell=False
            ).pid
        elif mpv:
            _launch_video(mpv, path, profile, before)
            return
        else:
            process_id = _shell_open_file(path)
        # A file's window (a video player, a document) can be closed again
        # with POST /close; a program's never is.
        _place_process_window(
            process_id, before, profile["monitor"], profile["fullscreen"], profile["position"],
            remember_as=profile["id"] if profile["kind"] == "file" else None,
        )
    except Exception:
        logger.exception("Profile %s failed", profile["id"])
    finally:
        if profile["kind"] == "file":
            with _opened_lock:
                # Only this launch's state: a newer start keeps its own.
                if _launching.get(profile["id"], (0.0, None))[1] == profile.get("launch_token"):
                    _launching.pop(profile["id"], None)
                    _cancelled.discard(profile["id"])
        if not document:
            _finish_reply(profile, False)  # not shown by the agent (default app, failed)


# -- videos in mpv -----------------------------------------------------------------
# A file profile (or a file in a row's field) that resolves to a video plays
# in mpv started by the agent instead of the PC's default app, when a real
# mpv.exe is there: the agent then owns the process, so its window is known
# for keep_if_open and POST /close, and mpv itself puts it full screen on
# the right monitor (picked by its device name, e.g. \\.\DISPLAY2, from the
# same monitor list images use). The agent never moves or restyles mpv's
# window: mpv would undo it, and it is not DPI aware. Without a real mpv.exe
# (none, MPV.NET, a Store app), the default app as before.

VIDEO_SUFFIXES = {".mp4", ".m4v", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".mpg", ".mpeg", ".ts"}
# A window not full screen: where on its monitor (mpv's own --geometry, in
# percent of that screen).
_MPV_POSITIONS = {"top-left": "0%:0%", "top-right": "100%:0%", "bottom-left": "0%:100%",
                  "bottom-right": "100%:100%", "center": "50%:50%"}


def _real_mpv(path: str | None) -> bool:
    """mpv.exe itself: not MPV.NET (mpvnet.exe), not a Store app's alias."""
    if not path:
        return False
    return PureWindowsPath(path).name.lower() == "mpv.exe" and "windowsapps" not in path.replace("/", "\\").lower()


def _video_player(profile: dict, path: Path) -> str | None:
    if profile["kind"] != "file" or path.suffix.lower() not in VIDEO_SUFFIXES:
        return None
    mpv = _play_mpv()
    if _real_mpv(mpv):
        return mpv
    logger.warning("Video %s: no mpv.exe found (%s), opened with the default app instead", path, mpv)
    return None


def _video_command(mpv: str, path: Path, profile: dict, screen: int | None = None) -> list:
    """mpv for a video on the profile's monitor (screen: mpv's number of it,
    mpv_screen()); it holds the last frame at the end (keep-open) until
    POST /close or the next start ends it."""
    command = [
        mpv, "--no-config", "--force-window=yes", "--keep-open=yes", "--no-terminal", "--hwdec=auto-safe",
        f"--title={profile['title']}", f"--log-file={agent_log_directory() / 'mpv-video.log'}",
    ]
    if screen is not None:
        command += [f"--screen={screen}"]
        if profile["fullscreen"]:
            command += [f"--fs-screen={screen}", "--fs"]
        else:
            command += ["--autofit=50%", f"--geometry={_MPV_POSITIONS.get(profile['position'], '50%:50%')}"]
    else:
        # The monitor is not there: mpv's window starts where it likes.
        if profile["fullscreen"]:
            command += ["--fs"]
    return command + ["--", str(path)]


def _launch_video(mpv: str, path: Path, profile: dict, before: set) -> None:
    """Start mpv for the video; an earlier one of the same profile ends
    first (a start by hand, a double press: never two players)."""
    profile_id = profile["id"]
    _close_remembered(profile_id)
    with _opened_lock:
        earlier = _video_processes.pop(profile_id, None)
    if earlier is not None and earlier.poll() is None:
        # Still starting (its window not found yet): ended directly.
        earlier.terminate()
        logger.info("Video profile=%s: the earlier mpv pid=%s ended", profile_id, earlier.pid)
    try:
        screen, device = mpv_screen(profile["monitor"])
    except Exception:  # noqa: BLE001 - mpv picks a screen then
        logger.exception("Monitor %s for the video not found", profile["monitor"])
        screen, device = None, None
    command = _video_command(mpv, path, profile, screen)
    logger.info("Video %s in mpv on monitor %s (mpv screen %s, %s): %s", path, profile["monitor"], screen,
                device or "no device name", subprocess.list2cmdline(command))
    process = subprocess.Popen(
        command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    with _opened_lock:
        _video_processes[profile_id] = process
    _remember_video_window(profile_id, process.pid, before)


def _remember_video_window(profile_id: str, process_id: int, before: set, find=None, timeout: float = 20.0) -> None:
    """Find mpv's window to remember it (for keep and close); it is never
    placed by the agent."""
    find = find or _find_window
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        hwnd = find(process_id, before)
        if hwnd and _remember_if_own(profile_id, hwnd, process_id):
            logger.info("Video window pid=%s hwnd=%s remembered for profile=%s", process_id, hwnd, profile_id)
            _close_if_cancelled(profile_id)
            return
        time.sleep(0.25)
    _remember_unknown(profile_id)
    logger.warning("Video window of pid=%s (profile=%s) not found within %.0f s", process_id, profile_id, timeout)


# -- documents page by page ------------------------------------------------------
# A file profile (or a file in a row's field) that is a PDF, PowerPoint or
# Word document is shown by the agent itself (documents.py renders the
# pages) on the profile's monitor (the same numbering as images), in its own
# window above the image / song window, so whatever was there shows again
# when it closes. Like an image: full screen, or a framed window in the work
# area at the profile's position (a corner: a quarter of it), and with
# "monitors" the same page on each of them. POST /document and the keys in
# its window page through it (arrows, Page Up / Page Down as a presenter
# sends them, Home, End; Escape hides it). The agent takes the keyboard
# focus only when nothing else has it (its own windows or none in front): it
# never takes it from someone working on that PC; a click on the page gives
# it the focus (it never turns a page). POST /close hides it like an image
# (by profile and stamp); keep_if_open keeps it, on its page, when the next
# row shows the same file. One document at a time: a new one replaces it.
# When the agent cannot render it (no pypdfium2, neither Office nor
# LibreOffice) the file opens with its default app as before.

DOCUMENT_PENDING_SECONDS = 240.0  # how long a close cancels a document still being prepared
# Converting (waiting for another conversion included) ends well before that.
DOCUMENT_CONVERT_SECONDS = 200.0
# A framed window's border and title bar at 100 %, for the first rendering;
# once the window is placed its real client size counts (_fit_document_client).
DOCUMENT_FRAME = (16, 39)


class _DocumentView:
    def __init__(self, profile: dict, pages, screens: list) -> None:
        self.profile_id = profile["id"]
        self.stamp = profile.get("stamp")
        self.requested = profile.get("requested", profile["path"])  # as sent (with "*")
        self.path = profile["path"]
        self.title = profile["title"]
        self.fullscreen = profile["fullscreen"]
        self.position = profile["position"]
        self.pages = pages
        self.page = 0
        # [{"monitor", "area": monitor or work area, "rect": (x, y, w, h) of the window, "client": (w, h)}]
        self.screens = screens

    def state(self) -> dict:
        return {"id": self.profile_id, "title": self.title, "page": self.page + 1, "pages": self.pages.count}

    def render(self, page: int) -> list:
        """The page for each screen (one render per size)."""
        return [self.pages.render(page, *screen["client"]) for screen in self.screens]


_document_lock = threading.RLock()
_document_view: _DocumentView | None = None  # what the document windows show (set on the tk thread)
_document_windows: list = []  # [(Toplevel, Label)], one per screen, made on first use


def document_cache_directory() -> Path:
    return agent_log_directory() / "documents"


def _document_screens(profile: dict, bounds=None, work_area=None) -> list:
    """Where the document goes on each of its monitors, like an image: the
    whole monitor, or in the work area the whole of it (center) or a quarter
    in the profile's corner. Raises RuntimeError for a missing monitor."""
    bounds = bounds or monitor_bounds
    work_area = work_area or monitor_work_area
    screens = []
    for monitor in profile.get("monitors") or [profile["monitor"]]:
        if profile["fullscreen"]:
            left, top, right, bottom = area = bounds(monitor)
            width, height = right - left, bottom - top
            screens.append({"monitor": monitor, "area": area, "rect": (left, top, width, height),
                            "client": (width, height)})
            continue
        left, top, right, bottom = area = work_area(monitor)
        if profile["position"] == "center":
            width, height = right - left, bottom - top
        else:
            width, height = (right - left) // 2, (bottom - top) // 2
        x, y = _window_position(area, width, height, profile["position"])
        screens.append({"monitor": monitor, "area": area, "rect": (x, y, width, height),
                        "client": (max(1, width - DOCUMENT_FRAME[0]), max(1, height - DOCUMENT_FRAME[1]))})
    return screens


def _document_superseded(profile_id: str, stamp: str | None) -> bool:
    """Closed (POST /close) or started again while it was being prepared."""
    with _opened_lock:
        if profile_id in _cancelled:
            return True
        return stamp is not None and _opened_stamps.get(profile_id) != stamp


DOCUMENT_ACK_SECONDS = 15.0  # the longest a /display of a document waits for its first page


class _DocumentReply:
    """The /display answer of a document waits for this: set once its first
    page is on screen (shown) or once it will not be (default app, closed)."""

    def __init__(self) -> None:
        self.done = threading.Event()
        self.shown = False

    def finish(self, shown: bool) -> None:
        if not self.done.is_set():
            self.shown = shown
            self.done.set()


def _document_reply(body: dict, target: Path) -> _DocumentReply | None:
    """A reply to wait for when the agent will show this file as a document."""
    if (body["kind"] != "file" or documents is None or documents.document_type(target) is None
            or documents.pdfium() is None):
        return None
    return _DocumentReply()


def _finish_reply(profile: dict, shown: bool) -> None:
    reply = profile.get("reply")
    if reply is not None:
        reply.finish(shown)


def _open_document(profile: dict, path: Path, screens=None, pdf_for=None, pages_class=None) -> bool:
    """Prepare a document's first page (background thread) and hand it to
    the tk thread. False when it is no document or cannot be rendered here:
    the caller opens it with its default app then."""
    if documents is None or documents.document_type(path) is None:
        return False
    if documents.pdfium() is None and pages_class is None:
        logger.warning("Document %s: pypdfium2 is not installed, opened with its default app", path)
        return False
    profile_id, stamp = profile["id"], profile.get("stamp")
    screens = screens or _document_screens
    deadline = time.monotonic() + DOCUMENT_CONVERT_SECONDS
    pdf_for = pdf_for or (lambda source, deadline, cancelled: documents.pdf_for(
        source, document_cache_directory(), deadline=deadline, cancelled=cancelled))
    pages_class = pages_class or documents.PdfPages
    with _opened_lock:
        # A conversion takes longer than a window opening: a close meanwhile
        # cancels it (only this launch's entry: a newer start keeps its own).
        current = _launching.get(profile_id)
        if current is None or current[1] == profile.get("launch_token"):
            _launching[profile_id] = (time.monotonic() + DOCUMENT_PENDING_SECONDS, profile.get("launch_token"))
    try:
        placed = screens(profile)
        pages = pages_class(pdf_for(path, deadline, lambda: _document_superseded(profile_id, stamp)))
    except documents.DocumentCancelled:
        logger.info("Document %s (profile=%s) closed or started again before its conversion; not shown",
                    path, profile_id)
        _finish_reply(profile, False)
        return True
    except Exception as exc:  # noqa: BLE001 - DocumentError, a missing monitor, a broken file
        if _closed_meanwhile(profile, path):
            return True
        logger.warning("Document %s cannot be shown by the agent (%s); opened with its default app", path, exc)
        return False
    view = _DocumentView(profile, pages, placed)
    try:
        images = view.render(0)
    except Exception:  # noqa: BLE001 - pdfium errors
        pages.close()
        if _closed_meanwhile(profile, path):
            return True
        logger.exception("Document %s: first page could not be rendered; opened with its default app", path)
        return False
    if _document_superseded(profile_id, stamp):
        pages.close()
        logger.info("Document %s (profile=%s) closed or started again while it was prepared; not shown",
                    path, profile_id)
        _finish_reply(profile, False)
        return True
    logger.info("Document %s: %s pages, monitors %s", path, pages.count,
                ", ".join(f"{s['monitor']} ({s['client'][0]}x{s['client'][1]})" for s in placed))
    requests.put({"kind": "document", "id": profile_id, "view": view, "images": images,
                  "reply": profile.get("reply")})
    return True


def _closed_meanwhile(profile: dict, path: Path) -> bool:
    """A failed document that was closed or started again meanwhile: no late
    default app for it (the program has moved on)."""
    if not _document_superseded(profile["id"], profile.get("stamp")):
        return False
    logger.info("Document %s (profile=%s) failed after it was closed or started again; nothing opened",
                path, profile["id"])
    _finish_reply(profile, False)
    return True


def _keep_document(profile_id: str, requested: str, stamp: str | None) -> bool:
    """keep_if_open for a document: the same file of the same profile is
    still shown; it stays (on its page) and takes the new start's stamp."""
    with _document_lock:
        view = _document_view
        if view is None or view.profile_id != profile_id or view.requested.lower() != requested.lower():
            return False
        if stamp is not None:
            view.stamp = stamp
            with _opened_lock:
                _opened_stamps[profile_id] = stamp
        return True


def _document_command(body: dict) -> dict | None:
    """Page through the shown document (POST /document, its keys). None when
    no document (or not the one named by "id") is shown. The page is rendered
    outside the lock; only the newest step reaches the window."""
    with _document_lock:
        view = _document_view
        if view is None or ("id" in body and body["id"] != view.profile_id):
            return None
        action = body["action"]
        if action == "status":
            return view.state()
        page = documents.step(view.page, view.pages.count, action, body.get("page"))
        if page == view.page:
            return view.state()
        view.page = page
        state = view.state()
    try:
        images = view.render(page)
    except documents.DocumentError:
        return state  # hidden or replaced meanwhile
    with _document_lock:
        if _document_view is view and view.page == page:
            requests.put({"kind": "document-page", "id": view.profile_id, "view": view, "page": page,
                          "images": images})
            logger.info("Document %s page %s/%s", view.profile_id, page + 1, view.pages.count)
    return state


def _document_key(action: str) -> None:
    """A key in the document window: rendered off the tk thread."""
    def run():
        try:
            _document_command({"action": action})
        except Exception:  # noqa: BLE001 - a key must never end the agent
            logger.exception("Document key %s failed", action)
    threading.Thread(target=run, daemon=True).start()


# Not Space, Return or Backspace: typing on that PC must never turn a page.
DOCUMENT_KEYS = {
    "next": ("<Right>", "<Down>", "<Next>"),
    "prev": ("<Left>", "<Up>", "<Prior>"),
    "first": ("<Home>",),
    "last": ("<End>",),
}


def _document_widgets(root, count: int) -> list:
    """At least ``count`` document windows (made once, kept hidden)."""
    while len(_document_windows) < count:
        window = tk.Toplevel(root, background="black")
        window.withdraw()
        window.protocol("WM_DELETE_WINDOW", lambda: _hide_document())
        window.bind("<Escape>", lambda _event: _hide_document())
        for action, keys in DOCUMENT_KEYS.items():
            for key in keys:
                window.bind(key, lambda _event, action=action: _document_key(action))
        # A click (mouse, touch screen) gives the keys to the document; it turns no page.
        window.bind("<Button-1>", lambda _event, window=window: window.focus_force())
        label = tk.Label(window, background="black", borderwidth=0)
        label.pack(fill="both", expand=True)
        _document_windows.append((window, label))
    return _document_windows


def _set_document_image(label, image) -> None:
    photo = ImageTk.PhotoImage(image)
    label.configure(image=photo)
    label.image = photo


# The desktop and the taskbar in front: nobody is working in a window.
SHELL_WINDOW_CLASSES = {"Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd"}


def _window_class(hwnd: int) -> str:
    import ctypes

    name = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, name, 256)
    return name.value


def _may_take_focus(foreground=None, window_pid=None, window_class=None) -> bool:
    """Only when nothing else has the keyboard: no window in front, the
    desktop or taskbar, or one of the agent's own (an image, the song, the
    document)."""
    if os.name != "nt" and foreground is None:
        return False
    if foreground is None:
        import ctypes

        foreground = ctypes.windll.user32.GetForegroundWindow()
    if not foreground:
        return True
    window_pid = window_pid or _window_process_id
    window_class = window_class or _window_class
    return window_pid(foreground) == os.getpid() or window_class(foreground) in SHELL_WINDOW_CLASSES


def _place_document_window(window, screen: dict, fullscreen: bool, position: str) -> None:
    """Windows only: the window's outer frame exactly where it belongs (full
    screen and topmost, or framed in the work area like an image)."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    hwnd = window.winfo_id()
    hwnd = user32.GetParent(hwnd) or hwnd
    x, y, width, height = screen["rect"]
    if fullscreen:
        # HWND_TOPMOST, SWP_SHOWWINDOW | SWP_FRAMECHANGED
        insert_after, flags = -1, 0x0040 | 0x0020
    else:
        x, y = _framed_window_position(screen["area"], width, height, _invisible_border(hwnd), position)
        insert_after, flags = -2, 0x0040  # HWND_NOTOPMOST, SWP_SHOWWINDOW
    if not user32.SetWindowPos(hwnd, insert_after, x, y, width, height, flags):
        raise ctypes.WinError()


def _show_document(root, request: dict, may_take_focus=None) -> bool:
    """Tk thread: show a prepared document (replacing one shown before).
    False when it was closed or started again since it was prepared."""
    global _document_view
    view = request["view"]
    if _document_superseded(view.profile_id, view.stamp):
        view.pages.close()
        logger.info("Document profile=%s closed or started again before it was shown", view.profile_id)
        return False
    with _document_lock:
        old, _document_view = _document_view, view
    if old is not None and old is not view:
        old.pages.close()
    windows = _document_widgets(root, len(view.screens))
    for window, _label in windows[len(view.screens):]:
        window.withdraw()
    for (window, label), screen, image in zip(windows, view.screens, request["images"]):
        x, y, width, height = screen["rect"]
        if not view.fullscreen:
            width, height = screen["client"]
        _set_document_image(label, image)
        window.withdraw()
        window.overrideredirect(view.fullscreen)
        window.attributes("-topmost", view.fullscreen)
        window.title(view.title)
        window.geometry(f"{width}x{height}+{x}+{y}")
        window.deiconify()
        window.state("normal")
        window.update_idletasks()
        window.lift()
        if os.name == "nt":
            _place_document_window(window, screen, view.fullscreen, view.position)
    _fit_document_client(view, [window for window, _label in windows[:len(view.screens)]])
    # The keys (a presenter on this PC) go to the document, unless someone works there.
    if (may_take_focus or _may_take_focus)():
        windows[0][0].focus_force()
    logger.info("Document shown profile=%s monitors=%s fullscreen=%s position=%s pages=%s", view.profile_id,
                [s["monitor"] for s in view.screens], view.fullscreen, view.position, view.pages.count)
    return True


def _client_size(window) -> tuple[int, int]:
    """The real client area of a placed window (its frame's, as Windows has it)."""
    if os.name != "nt":
        window.update_idletasks()
        return window.winfo_width(), window.winfo_height()
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    hwnd = window.winfo_id()
    hwnd = user32.GetParent(hwnd) or hwnd
    rect = wintypes.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return 0, 0
    return rect.right - rect.left, rect.bottom - rect.top


def _fit_document_client(view: _DocumentView, windows: list, client_size=None) -> bool:
    """Tk thread: a framed window's real client size (borders and title bar
    depend on the display scaling) replaces the guess; the page is rendered
    again for it in the background. True when something changed."""
    if view.fullscreen:
        return False
    client_size = client_size or _client_size
    changed = False
    for window, screen in zip(windows, view.screens):
        actual = client_size(window)
        if actual[0] > 1 and actual[1] > 1 and any(abs(a - b) > 2 for a, b in zip(actual, screen["client"])):
            screen["client"] = actual
            changed = True
    if changed:
        threading.Thread(target=_refresh_document, args=(view,), daemon=True).start()
    return changed


def _refresh_document(view: _DocumentView) -> None:
    """The current page again (a window's size changed)."""
    with _document_lock:
        page = view.page
    try:
        images = view.render(page)
    except Exception:  # noqa: BLE001 - hidden meanwhile, pdfium errors: the old picture stays
        return
    with _document_lock:
        if _document_view is view and view.page == page:
            requests.put({"kind": "document-page", "id": view.profile_id, "view": view, "page": page,
                          "images": images})


def _show_document_page(request: dict) -> None:
    """Tk thread: another page of the document still shown (the newest step only)."""
    with _document_lock:
        view = _document_view
        current = view is request["view"] and view.page == request["page"]
    if current:
        for (window, label), image in zip(_document_windows, request["images"]):
            _set_document_image(label, image)
            window.lift()


def _hide_document(profile_id: str | None = None, stamp: str | None = None) -> bool:
    """Tk thread: hide the document windows (Escape, the close button, or
    POST /close naming its profile and start). True if it was hidden."""
    global _document_view
    with _document_lock:
        view = _document_view
        if view is None:
            return False
        if profile_id is not None and (view.profile_id != profile_id
                                       or (stamp is not None and view.stamp not in (None, stamp))):
            return False
        _document_view = None
    view.pages.close()
    for window, _label in _document_windows:
        window.withdraw()
    logger.info("Document hidden profile=%s", view.profile_id)
    return True


# -- closing what a profile opened (POST /close) ---------------------------------
# When the program moves on to its next row, the service ends what the rows
# before showed: the windows a "file" profile opened (a video player, e.g.
# Mission Spotlight) get WM_CLOSE, and an image is hidden if it is still the
# one in the window. Programs are never closed; songs are not profiles.

_opened_windows: dict[str, set[tuple[int, int]]] = {}  # profile id -> {(hwnd, process id)}
# Profiles whose window could not be told apart (the file went to an app
# that was already running, e.g. a single-instance player): /close says so.
_unknown_windows: set[str] = set()
_opened_lock = threading.Lock()
_shown_image: str | None = None  # the profile whose image the window shows (tk thread only)
_shown_image_stamp: str | None = None
# Start stamps: the service names each start of a profile (a random id); a
# stop or close names the start it means and is ignored once a newer start
# (or a kept one) of that profile came in since.
_opened_stamps: dict[str, str] = {}  # profile id -> stamp of its latest open (file kind)
# A file profile being opened (until its launch is done): a second program
# row in that time keeps it instead of opening it twice.
# profile id -> (until (time.monotonic()), the launch's token): only that
# launch ends it, an older one finishing late leaves a newer one alone.
_launching: dict[str, tuple[float, str]] = {}
# The mpv each video profile runs in (a new start ends the earlier one).
_video_processes: dict[str, subprocess.Popen] = {}
# Closed (POST /close) while still being opened: its window is closed as
# soon as it appears.
_cancelled: set[str] = set()
LAUNCH_PENDING_SECONDS = 25.0


def _valid_stamp(value) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 64 and value.replace("-", "").replace("_", "").isalnum()


# POST /close waits this long for the tk thread to say what it hid.
CLOSE_REPLY_SECONDS = 0.8


def _launch_pending(profile_id: str) -> bool:
    """Being opened (call with _opened_lock held)."""
    return _launching.get(profile_id, (0.0, ""))[0] > time.monotonic()


def _keep_open(profile_id: str, stamp: str | None, window_pid=None) -> bool:
    """keep_if_open: whether the profile's window is still there (or still
    being opened, unless a close came meanwhile); then it is kept and takes
    the new start's stamp. One section under the lock, so a close cannot
    slip in between."""
    window_pid = window_pid or _window_process_id
    with _opened_lock:
        if profile_id in _cancelled:
            return False
        opening = _launch_pending(profile_id)
        if not opening and not any(window_pid(h) == pid for h, pid in _opened_windows.get(profile_id, ())):
            return False
        if stamp is not None:
            _opened_stamps[profile_id] = stamp
        return True


def _remember_window(profile_id: str, hwnd: int, process_id: int, window_pid=None) -> None:
    """Note the window for POST /close; windows gone since are dropped."""
    window_pid = window_pid or _window_process_id
    with _opened_lock:
        for other in list(_opened_windows):
            alive = {(h, pid) for h, pid in _opened_windows[other] if window_pid(h) == pid}
            if alive:
                _opened_windows[other] = alive
            else:
                del _opened_windows[other]
        _opened_windows.setdefault(profile_id, set()).add((hwnd, process_id))
        _unknown_windows.discard(profile_id)


def _remember_unknown(profile_id: str) -> None:
    with _opened_lock:
        if profile_id not in _opened_windows:
            _unknown_windows.add(profile_id)


def _window_process_id(hwnd: int) -> int:
    """The process a window belongs to; 0 when the window is gone."""
    import ctypes
    from ctypes import wintypes

    owner = wintypes.DWORD()
    if not ctypes.windll.user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(owner)):
        return 0
    return owner.value


def _post_close(hwnd: int) -> bool:
    import ctypes
    from ctypes import wintypes

    WM_CLOSE = 0x0010
    return bool(ctypes.windll.user32.PostMessageW(wintypes.HWND(hwnd), WM_CLOSE, 0, 0))


def _close_profile_windows(profile_id: str, window_pid=None, post_close=None, stamp: str | None = None) -> int:
    """Ask the windows a profile opened to close (as their close button
    does). A window that is gone, or whose handle now belongs to another
    process, is left alone; so is everything when the close names an older
    start than the latest open of the profile. A profile still being opened
    is closed once its window appears. Returns how many were asked."""
    with _opened_lock:
        latest = _opened_stamps.get(profile_id)
        if stamp is not None and latest is not None and latest != stamp:
            logger.info("Close profile=%s ignored: opened again since", profile_id)
            return 0
        _opened_stamps.pop(profile_id, None)
        if _launch_pending(profile_id):
            _cancelled.add(profile_id)
            logger.info("Close profile=%s: still opening, closed once its window is there", profile_id)
    return _close_remembered(profile_id, window_pid, post_close)


def _close_if_cancelled(profile_id: str, window_pid=None, post_close=None) -> bool:
    """A close came while the profile was being opened: close it now."""
    with _opened_lock:
        if profile_id not in _cancelled:
            return False
        _cancelled.discard(profile_id)
    _close_remembered(profile_id, window_pid, post_close)
    return True


def _close_remembered(profile_id: str, window_pid=None, post_close=None) -> int:
    """WM_CLOSE to the profile's remembered windows still owned by their process."""
    window_pid = window_pid or _window_process_id
    post_close = post_close or _post_close
    with _opened_lock:
        windows = _opened_windows.pop(profile_id, set())
        unknown = profile_id in _unknown_windows
        _unknown_windows.discard(profile_id)
    if unknown and not windows:
        logger.warning("Close profile=%s: its window is not known (the file went to an app that was "
                       "already running); nothing closed", profile_id)
    closed = 0
    for hwnd, process_id in windows:
        if window_pid(hwnd) == process_id and post_close(hwnd):
            closed += 1
    if windows:
        logger.info("Close profile=%s: %s of %s windows", profile_id, closed, len(windows))
    return closed


# -- finding mpv and yt-dlp ----------------------------------------------------
# The agent may run without the user's environment: after a self-update it is
# started again through WMI, whose processes get neither the user's PATH (where
# winget links mpv) nor the user's own variables. So the search also reads the
# user's and the machine's variables from the registry and looks where the
# usual installers put the programs.

def _registry_environment() -> dict:
    """{"machine": {...}, "user": {...}}: the variables as set in Windows
    (upper-case names, values not yet expanded)."""
    result = {"machine": {}, "user": {}}
    if os.name != "nt":
        return result
    import winreg

    keys = (
        ("machine", winreg.HKEY_LOCAL_MACHINE, r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
        ("user", winreg.HKEY_CURRENT_USER, "Environment"),
    )
    for scope, root, key in keys:
        try:
            with winreg.OpenKey(root, key) as handle:
                index = 0
                while True:
                    try:
                        name, value, _kind = winreg.EnumValue(handle, index)
                    except OSError:
                        break
                    index += 1
                    if isinstance(value, str):
                        result[scope][name.upper()] = value
        except OSError:
            continue
    return result


def _known_folders() -> dict:
    """LOCALAPPDATA / USERPROFILE / APPDATA of the user this process runs as,
    from Windows itself (right even when the environment is not)."""
    if os.name != "nt":
        return {}
    import ctypes
    from ctypes import wintypes

    class GUID(ctypes.Structure):
        _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD), ("Data3", wintypes.WORD),
                    ("Data4", ctypes.c_ubyte * 8)]

    def guid(text: str) -> GUID:
        import uuid
        raw = uuid.UUID(text).bytes_le
        return GUID.from_buffer_copy(raw)

    folders = {}
    for name, text in (("LOCALAPPDATA", "F1B32785-6FBA-4FCF-9D55-7B8E7F157091"),
                       ("USERPROFILE", "5E6C858F-0E22-4760-9AFE-EA3317B67173"),
                       ("APPDATA", "3EB685DB-65F9-4CF6-A03A-E3EF65729F3D")):
        path = ctypes.c_wchar_p()
        try:
            if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(guid(text)), 0, None, ctypes.byref(path)) == 0:
                folders[name] = path.value
        except (OSError, AttributeError):
            pass
        finally:
            if path:
                ctypes.windll.ole32.CoTaskMemFree(path)
    return folders


# Variables that belong to the signed-in user: never taken from the machine.
_PER_USER_NAMES = {"USERNAME", "USERPROFILE", "USERDOMAIN", "USERDOMAIN_ROAMINGPROFILE",
                   "HOMEDRIVE", "HOMEPATH", "LOCALAPPDATA", "APPDATA"}


def _valid_variable_name(name: str) -> bool:
    """A name os.environ accepts (a "(Standard)" value under HKCU\\Environment
    has an empty name)."""
    return bool(name) and "=" not in name and "\0" not in name


def _expand(value: str, env: dict) -> str:
    """%NAME% as Windows expands it (names case-insensitive; unknown stay)."""
    return re.sub(r"%([^%]+)%", lambda m: env.get(m.group(1).upper(), m.group(0)), value)


def _windows_environment(process_env=None, registry=None, folders=None) -> dict:
    """The environment the user's programs normally see: this process's
    variables, plus the machine's and the user's from the registry where they
    are missing; PATH holds every entry of all three; the user folders come
    from Windows. Upper-case names."""
    env = {k.upper(): v for k, v in (os.environ if process_env is None else process_env).items()}
    registry = _registry_environment() if registry is None else registry
    folders = _known_folders() if folders is None else folders
    for name, value in folders.items():
        current = env.get(name, "")
        # WMI-started processes may point at the system profile instead.
        if value and (not current or "systemprofile" in current.lower()):
            env[name] = value
    # HKLM holds e.g. USERNAME=SYSTEM: per-user names never come from there.
    machine = {k: v for k, v in registry.get("machine", {}).items() if k not in _PER_USER_NAMES}
    merged = {**machine, **registry.get("user", {})}
    for name, value in merged.items():
        if name != "PATH" and name not in env and _valid_variable_name(name):
            env[name] = _expand(value, env)
    paths, seen = [], set()
    for source in (env.get("PATH", ""), registry.get("machine", {}).get("PATH", ""),
                   registry.get("user", {}).get("PATH", "")):
        for entry in _expand(source, env).split(os.pathsep):
            entry = entry.strip().strip('"')
            if entry and entry.lower() not in seen:
                seen.add(entry.lower())
                paths.append(entry)
    env["PATH"] = os.pathsep.join(paths)
    return env


def restore_user_environment() -> list:
    """Fill in what this process is missing of the user's environment (see
    above): variables like MPV_PATH or DESKTOP_AGENT_TOKEN set for the user,
    the user's PATH entries, the user folders. Returns the names changed."""
    if os.name != "nt":
        return []
    full = _windows_environment()
    changed = []
    for name, value in full.items():
        if os.environ.get(name) == value or not _valid_variable_name(name) or "\0" in value:
            continue
        try:
            os.environ[name] = value
        except (ValueError, OSError):
            logger.warning("Could not take over the variable %r from the user's environment", name)
            continue
        changed.append(name)
    return sorted(changed)


# /info reports the players found; that search reads the registry and globs
# install folders, too slow and too risky for a request the service makes
# every 30 s with a 2 s timeout. So it runs at start and in the background
# when older than TOOL_CACHE_SECONDS; /info answers with the last result.
TOOL_CACHE_SECONDS = 300.0
_tool_cache = {"at": 0.0, "mpv": None, "ytdlp": None, "mpv_search": [], "variables": None}
# The variables naming a player: a play takes the cached path only while
# they are as they were at the search.
_TOOL_VARIABLES = ("MPV_PATH", "YTDLP_PATH")
_tool_cache_lock = threading.Lock()
_tool_refreshing = threading.Event()


def _tool_variables() -> dict:
    """MPV_PATH and YTDLP_PATH as find_tool sees them now."""
    env = _windows_environment()
    return {name: env.get(name, "").strip().strip('"') for name in _TOOL_VARIABLES}


def refresh_tool_cache() -> None:
    trace: list = []
    variables = None
    try:
        variables = _tool_variables()
        mpv = _find_mpv(trace)
        ytdlp = _find_ytdlp(mpv)
    except Exception:  # noqa: BLE001 - a diagnosis must never break the agent
        logger.exception("Searching mpv / yt-dlp failed")
        mpv = ytdlp = None
    with _tool_cache_lock:
        _tool_cache.update(at=time.monotonic(), mpv=mpv, ytdlp=ytdlp, mpv_search=trace, variables=variables)
    _tool_refreshing.clear()


def cached_tools() -> dict:
    """{"mpv", "ytdlp", "mpv_search"} from the last search (mpv_search: every
    place looked at for mpv, see find_tool); a search older than
    TOOL_CACHE_SECONDS is started again in the background. Empty until the
    first search is done: right after a restart the service asks /info
    before it, and "not found" there would be a false warning."""
    with _tool_cache_lock:
        snapshot = dict(_tool_cache)
    stale = not snapshot["at"] or time.monotonic() - snapshot["at"] > TOOL_CACHE_SECONDS
    if stale and not _tool_refreshing.is_set():
        _tool_refreshing.set()
        threading.Thread(target=refresh_tool_cache, daemon=True).start()
    if not snapshot["at"]:
        return {}
    return {"mpv": snapshot["mpv"], "ytdlp": snapshot["ytdlp"], "mpv_search": snapshot["mpv_search"]}


def _cached_tool(name: str) -> str | None:
    """The path the last search found for ``name`` ("mpv", "ytdlp") while
    that search is younger than TOOL_CACHE_SECONDS, the file is still there
    and MPV_PATH / YTDLP_PATH are unchanged; None means: search again. A play
    then needs no globs over the install folders before mpv starts.
    A last resort (MPV.NET, a Store app) is never taken from the cache: a
    real mpv installed since is used at once."""
    with _tool_cache_lock:
        at, path, variables = _tool_cache["at"], _tool_cache.get(name), _tool_cache.get("variables")
    if not at or not path or variables is None or time.monotonic() - at > TOOL_CACHE_SECONDS:
        return None
    if _last_resort_note("yt-dlp" if name == "ytdlp" else name, path):
        return None
    try:
        if not Path(path).is_file() or _tool_variables() != variables:
            return None
    except Exception:  # noqa: BLE001 - when in doubt, search in full
        return None
    return path


def _play_mpv() -> str | None:
    return _cached_tool("mpv") or _find_mpv()


def _play_ytdlp(mpv: str | None) -> str | None:
    return _cached_tool("ytdlp") or _find_ytdlp(mpv)


# Where installers put a program. {exe}: the file name; {app}: the program's
# folder name (scoop, Program Files); {pkg}: part of a WinGet/Chocolatey
# package name; {app_any}: a folder whose name contains {app} in any case
# ("MPV Player": winget's shinchiro.mpv is an Inno installer for the machine
# that installs to %ProgramFiles%\MPV Player and does not touch PATH).
_TOOL_PLACES = (
    ("WinGet links (user)", "{LOCALAPPDATA}/Microsoft/WinGet/Links/{exe}"),
    ("WinGet links (machine)", "{PROGRAMFILES}/WinGet/Links/{exe}"),
    ("WinGet packages (user)", "{LOCALAPPDATA}/Microsoft/WinGet/Packages/*{pkg}*/**/{exe}"),
    ("WinGet packages (machine)", "{PROGRAMFILES}/WinGet/Packages/*{pkg}*/**/{exe}"),
    ("scoop", "{SCOOP}/shims/{exe}"),
    ("scoop", "{SCOOP}/apps/{app}/current/{exe}"),
    ("scoop (global)", "{PROGRAMDATA}/scoop/shims/{exe}"),
    ("Chocolatey", "{CHOCOLATEYINSTALL}/bin/{exe}"),
    ("Chocolatey", "{CHOCOLATEYINSTALL}/lib/*{pkg}*/tools/**/{exe}"),
    ("Program Files", "{PROGRAMFILES}/{app}/{exe}"),
    ("Program Files", "{PROGRAMW6432}/{app}/{exe}"),
    ("Program Files", "{PROGRAMFILES}/{app_any}/{exe}"),
    ("Program Files", "{PROGRAMW6432}/{app_any}/{exe}"),
    ("Program Files (x86)", "{PROGRAMFILES(X86)}/{app}/{exe}"),
    ("Program Files (x86)", "{PROGRAMFILES(X86)}/{app_any}/{exe}"),
    ("LocalAppData Programs", "{LOCALAPPDATA}/Programs/{app}/{exe}"),
    ("LocalAppData Programs", "{LOCALAPPDATA}/Programs/{app_any}/{exe}"),
)

# Programs that may be named in MPV_PATH (or found for mpv) but are not mpv:
# MPV.NET (mpvnet.exe) is a player with its own window, settings and single
# instance; started with mpv's options it is slow and its control channel did
# not answer. It is used only when no real mpv.exe is found anywhere.
_FALLBACK_NAMES = {"mpv": {"mpvnet.exe", "mpvnet"}}


def _last_resort_note(command: str, found: str) -> str | None:
    """Why ``found`` is used only when nothing else is found, or None.
    Store apps (anything under a WindowsApps folder, e.g. MPV.NET or an app
    execution alias) start through the package and its own setup."""
    path = PureWindowsPath(found)
    if path.name.lower() in _FALLBACK_NAMES.get(command, set()):
        return "not mpv itself: used only if nothing else is found"
    if any(part.lower() == "windowsapps" for part in path.parts[:-1]):
        return "Store app (WindowsApps): used only if nothing else is found"
    return None


def _any_case(text: str) -> str:
    """A glob piece matching ``text`` in any case (Windows globs are
    case-insensitive anyway; this makes the tests on other systems agree)."""
    return "".join(f"[{c.lower()}{c.upper()}]" if c.isalpha() else glob.escape(c) for c in text)


def agent_account() -> dict:
    """The Windows account the agent runs as and its LOCALAPPDATA: a program
    another account installed for itself is not found here."""
    try:
        user = getpass.getuser()
    except Exception:  # noqa: BLE001 - only a diagnosis
        user = None
    return {"agent_user": user, "agent_localappdata": os.environ.get("LOCALAPPDATA")}


_UNINSTALL_KEY = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"


def _installed_locations(app: str) -> list:
    """InstallLocation of every installed program whose name contains
    ``app`` (HKLM and HKCU uninstall entries, 64 and 32 bit), MPV.NET left
    out. Empty outside Windows.

    The views are chosen explicitly (KEY_WOW64_64KEY / KEY_WOW64_32KEY): a
    32-bit Python would otherwise be redirected to the 32-bit view twice and
    never see a 64-bit installer's entry. REG_EXPAND_SZ values
    ("%ProgramFiles%\\MPV Player") are expanded."""
    if os.name != "nt":
        return []
    import winreg

    views = ((winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_64KEY),
             (winreg.HKEY_LOCAL_MACHINE, winreg.KEY_WOW64_32KEY),
             (winreg.HKEY_CURRENT_USER, winreg.KEY_WOW64_64KEY))

    def read(entry, value_name: str) -> str:
        value, kind = winreg.QueryValueEx(entry, value_name)
        value = str(value)
        if kind == winreg.REG_EXPAND_SZ:
            value = winreg.ExpandEnvironmentStrings(value)
        return value

    locations = []
    for root, view in views:
        access = winreg.KEY_READ | view
        try:
            handle = winreg.OpenKey(root, _UNINSTALL_KEY, 0, access)
        except OSError:
            continue
        with handle:
            for index in range(1000):
                try:
                    sub = winreg.EnumKey(handle, index)
                except OSError:
                    break
                try:
                    with winreg.OpenKey(handle, sub, 0, access) as entry:
                        name = read(entry, "DisplayName")
                        location = read(entry, "InstallLocation").strip().strip('"')
                except OSError:
                    continue
                lowered = name.lower()
                if app.lower() in lowered and "mpv.net" not in lowered and location and location not in locations:
                    locations.append(location)
    return locations


def find_tool(command: str, path_variable: str, app: str, pkg: str, *, near=(), env=None,
              exe=None, trace=None) -> tuple:
    """(path or None, [places searched]) for a program like mpv: the variable
    naming it (MPV_PATH), PATH (with the user's PATH from the registry), the
    agent's folder, ``near`` (folders to look in first, e.g. mpv's for yt-dlp)
    and the usual install places. ``trace`` (a list) gets every place looked
    at: {"place", "looked", "found"} (for /info)."""
    env = _windows_environment() if env is None else env
    # The agent runs on Windows: the files looked for are always "<name>.exe"
    # (PATH is searched with shutil.which, which knows PATHEXT there).
    exe = exe or f"{command}.exe"
    searched = []
    fallback = None
    trace = [] if trace is None else trace

    def take(place: str, looked: str, found) -> str | None:
        """The program found at a place, or None to keep searching."""
        nonlocal fallback
        if found:
            found = str(found)
            # mpv.com is mpv's console wrapper: it starts mpv.exe as a child,
            # which then outlives a stop that ends the wrapper.
            if found.lower().endswith(".com") and Path(found).with_suffix(".exe").is_file():
                found = str(Path(found).with_suffix(".exe"))
        entry = {"place": place, "looked": looked[:200], "found": found or None}
        trace.append(entry)
        note = _last_resort_note(command, found) if found else None
        if note:
            entry["note"] = note
            fallback = fallback or found
            return None
        return found or None

    configured = env.get(path_variable, "").strip().strip('"')
    if configured:
        searched.append(path_variable)
        found = configured if Path(configured).is_file() else shutil.which(configured, path=env.get("PATH"))
        if take(path_variable, configured, found):
            return trace[-1]["found"], searched
    searched.append("PATH (incl. the user's PATH from the registry)")
    if take("PATH", command, shutil.which(command, path=env.get("PATH"))):
        return trace[-1]["found"], searched
    searched.append("the agent folder")
    for folder in (*near, AGENT_DIR):
        candidate = Path(folder) / exe
        if take("next to mpv" if folder in near else "the agent folder", str(candidate),
                candidate if candidate.is_file() else None):
            return trace[-1]["found"], searched
    # Installers record their folder (Inno: InstallLocation): right even
    # when a new version installs somewhere else.
    searched.append("installed programs (registry)")
    try:
        locations = _installed_locations(app)
    except Exception:  # noqa: BLE001 - an unreadable registry is no reason to stop searching
        logger.exception("Reading the uninstall entries failed")
        locations = None
    if not locations:
        take("installed programs (registry)",
             f"uninstall entries naming {app!r}: " + ("could not be read" if locations is None else "none"),
             None)
    for location in locations or ():
        candidate = Path(location) / exe
        if take("installed programs (registry)", str(candidate), candidate if candidate.is_file() else None):
            return trace[-1]["found"], searched
    defaults = {
        "LOCALAPPDATA": env.get("LOCALAPPDATA", ""),
        "PROGRAMFILES": env.get("PROGRAMFILES", r"C:\Program Files"),
        "PROGRAMW6432": env.get("PROGRAMW6432", ""),
        "PROGRAMFILES(X86)": env.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
        "PROGRAMDATA": env.get("PROGRAMDATA", r"C:\ProgramData"),
        "SCOOP": env.get("SCOOP") or (str(Path(env["USERPROFILE"]) / "scoop") if env.get("USERPROFILE") else ""),
        "CHOCOLATEYINSTALL": env.get("CHOCOLATEYINSTALL") or (
            str(Path(env.get("PROGRAMDATA", r"C:\ProgramData")) / "chocolatey")),
    }
    for label, template in _TOOL_PLACES:
        if label not in searched:
            searched.append(label)
        variable = template[1:template.index("}")]
        base = defaults.get(variable, "")
        if not base:
            continue
        pattern = template.replace("{" + variable + "}", base.replace("\\", "/")).format(
            exe=exe, app=app, pkg=pkg, app_any=f"*{_any_case(app)}*")
        if "*" in pattern:
            matches = sorted(glob.glob(pattern, recursive=True))
            match = next((m for m in matches if Path(m).is_file()), None)
            found = take(label, pattern, Path(match) if match else None)
        else:
            found = take(label, pattern, Path(pattern) if Path(pattern).is_file() else None)
        if found:
            return found, searched
    return fallback, searched


def _find_mpv(trace=None) -> str | None:
    return find_tool("mpv", "MPV_PATH", "mpv", "mpv", trace=trace)[0]


def missing_tool_message(command: str, path_variable: str, searched: list, install: str) -> str:
    return (f"{command} was not found on this PC (searched: {', '.join(searched)}); "
            f"install it ({install}) or set {path_variable}")


# -- YouTube links (audio profiles) -----------------------------------------
# An audio profile's path may be a YouTube link instead of a file: mpv plays
# its sound, fetched with yt-dlp (which must be installed on this PC). Only
# YouTube addresses are accepted, so the agent never streams arbitrary URLs.
YOUTUBE_URL_RE = re.compile(
    r"^https?://(?:(?:www|m|music)\.)?"
    r"(?:youtube\.com/(?:watch\?(?:[^\s#]*&)?v=[\w-]{6,}|shorts/[\w-]{6,}|live/[\w-]{6,}|embed/[\w-]{6,})"
    r"|youtu\.be/[\w-]{6,})[^\s]*$"
)

def is_youtube_url(value) -> bool:
    return (isinstance(value, str) and len(value) <= 500
            and not any(ord(c) < 32 for c in value) and YOUTUBE_URL_RE.match(value) is not None)


def _find_ytdlp(mpv: str | None = None) -> str | None:
    near = (Path(mpv).parent,) if mpv else ()
    return find_tool("yt-dlp", "YTDLP_PATH", "yt-dlp", "yt-dlp", near=near)[0]


# -- audio (mpv) -------------------------------------------------------------
# With a fade, mpv fades in by itself: its audio filter (lavfi afade) ramps the
# sound from silence once the first audio plays, so a stream (YouTube) that
# loads for a while is faded in too. Nothing has to reach mpv for that, and
# playback starts as fast as without a fade.
# The fade-out on stop drives mpv's volume over its JSON IPC (a named pipe on
# Windows) down to silence, then mpv is ended. The IPC is connected in the
# background; if it does not answer, stop ends the music at once instead of
# fading. (Before, a play with fade waited for the IPC and failed when it did
# not come: on the streaming PC every faded play failed that way.)
# mpv's volume is cubic: gain = (v/100)^3, so dB = 60 * log10(v / 100).
FADE_FLOOR_DB = -60.0
FADE_STEP_SECONDS = 0.05
# How long mpv may take to open its control channel (only the fade-out
# needs it). On Windows a cold start can take several seconds.
IPC_CONNECT_SECONDS = 10.0
# A player that ends within this time after starting did not start at all.
START_CHECK_SECONDS = 0.4


class AudioStartError(OSError):
    """mpv did not start; the message says why (shown to the user)."""


def agent_log_directory() -> Path:
    return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MixerMuteDisplayAgent"


def mpv_log_path() -> Path:
    """mpv's own log of the last start (overwritten each time)."""
    return agent_log_directory() / "mpv-last.log"


def _mpv_error_line(log_path: Path) -> str:
    """The last error mpv wrote to its log, without the time stamp."""
    try:
        with open(log_path, "rb") as log_file:
            log_file.seek(0, os.SEEK_END)
            log_file.seek(max(0, log_file.tell() - 8192))
            lines = log_file.read().decode("utf-8", "replace").splitlines()
    except OSError:
        return ""
    for line in reversed(lines):
        match = re.match(r"^\[\s*[\d.]+\]\[[ef]\]\[([^\]]*)\]\s*(.+)$", line.strip())
        if match:
            return match.group(2).strip()[:160]
    return ""


_MPV_LOG_LINE_RE = re.compile(r"^\[\s*(\d{1,7}\.\d{1,6})\]\[(\w)\]\[([^\]]*)\]\s*(.*)$")
_MPV_MILESTONES = (
    ("opened", "Opening done"),
    ("audio_out", "AO: ["),
    ("playing", "starting audio playback"),
)
MPV_REPORT_BYTES = 256 * 1024


def mpv_start_report(log_path: Path | None = None) -> dict | None:
    """Where the last mpv start spent its time, from mpv's own log (time
    stamps in seconds since mpv started): when the file was open, the audio
    output ready and the sound playing, and the longest pauses between two
    log lines up to the first sound, with the lines around them (later
    pauses are quiet playback). Sent with /info, so a slow start on a PC can
    be read remotely (GET /update/agents)."""
    log_path = mpv_log_path() if log_path is None else log_path
    try:
        written = log_path.stat().st_mtime
        with open(log_path, "rb") as log_file:
            lines = log_file.read(MPV_REPORT_BYTES).decode("utf-8", "replace").splitlines()
    except OSError:
        return None
    report: dict = {"written_at": written}
    gaps = []
    previous = None
    for line in lines:
        match = _MPV_LOG_LINE_RE.match(line.strip())
        if not match:
            continue
        try:
            stamp = float(match.group(1))
        except ValueError:
            continue
        text = f"[{match.group(3)}] {match.group(4)}"[:160]
        if previous is not None and stamp - previous[0] >= 1.0:
            gaps.append({"seconds": round(stamp - previous[0], 2), "at": previous[0],
                         "before": previous[1], "after": text})
        previous = (stamp, text)
        for key, marker in _MPV_MILESTONES:
            if key not in report and marker in match.group(4):
                report[key] = stamp
        if "playing" in report:
            break  # the start is over
    report["gaps"] = sorted(gaps, key=lambda gap: -gap["seconds"])[:3]
    return report


def _start_failure(process: subprocess.Popen, what: str) -> AudioStartError:
    code = process.poll()
    detail = _mpv_error_line(mpv_log_path())
    if code is not None:
        message = f"audio player could not be started: mpv ended at once (exit code {code})"
    else:
        message = f"audio player could not be started: {what}"
    return AudioStartError(message + (f": {detail}" if detail else ""))


def db_to_mpv_volume(db: float) -> float:
    return max(0.0, min(100.0, 100.0 * 10 ** (db / 60.0)))


def _valid_fade(fade) -> bool:
    def number(value, low, high):
        return type(value) in (int, float) and low <= value <= high

    return (
        isinstance(fade, dict) and set(fade) == {"db", "seconds"}
        and number(fade["db"], FADE_FLOOR_DB, 0.0) and number(fade["seconds"], 0.5, 30.0)
    )


class MpvIpc:
    """Line-based JSON IPC to one mpv process; every command waits for its reply."""

    def __init__(self, address: str) -> None:
        self._address = address
        self._stream = None
        self._socket: socket.socket | None = None
        self._request_id = 0

    def connect(self, process: subprocess.Popen, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        last_error: OSError | None = None
        while True:
            try:
                if os.name == "nt":
                    self._stream = open(self._address, "r+b", buffering=0)
                else:  # mpv elsewhere uses a Unix socket (and the tests do)
                    self._socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                    try:
                        self._socket.connect(self._address)
                    except OSError:
                        self._socket.close()
                        self._socket = None
                        raise
                    self._stream = self._socket.makefile("rwb", buffering=0)
                break
            except OSError as exc:
                last_error = exc
                if process.poll() is not None or time.monotonic() > deadline:
                    # The Windows error (2 not found, 5 access denied, 231
                    # busy, ...) says why; it goes into the agent's log.
                    raise OSError(f"mpv IPC {self._address} not reachable: {last_error!r}")
                time.sleep(0.05)
        self.command("disable_event", "all")

    def command(self, *args):
        if self._stream is None:
            raise OSError("mpv IPC closed")
        self._request_id += 1
        request_id = self._request_id
        self._stream.write(json.dumps({"command": list(args), "request_id": request_id}).encode() + b"\n")
        while True:
            line = self._readline()
            if not line:
                raise OSError("mpv IPC closed")
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if reply.get("request_id") == request_id:
                if reply.get("error") != "success":
                    raise OSError(f"mpv: {reply.get('error')}")
                return reply.get("data")

    def _readline(self) -> bytes:
        # Byte-wise: unbuffered pipes and sockets have no reliable readline.
        data = bytearray()
        while True:
            chunk = self._stream.read(1)
            if not chunk:
                return bytes(data)
            data += chunk
            if chunk == b"\n":
                return bytes(data)

    def close(self) -> None:
        for handle in (self._stream, self._socket):
            if handle is not None:
                try:
                    handle.close()
                except OSError:
                    pass
        self._stream = self._socket = None


def _ipc_address() -> str:
    name = f"mixer-agent-mpv-{os.getpid()}-{time.monotonic_ns() % 10**9}"
    if os.name == "nt":
        return rf"\\.\pipe\{name}"
    # Unix socket paths are limited to about 100 characters.
    return str(Path(tempfile.gettempdir()) / f"{name}.sock")


class _Playback:
    def __init__(self, profile_id: str, process: subprocess.Popen, ipc: MpvIpc | None, fade: dict | None,
                 stream: bool = False):
        self.profile_id = profile_id
        self.stream = stream  # a YouTube link: sound only after loading
        self.process = process
        self.ipc = ipc
        self.fade = fade
        # mpv starts at the profile's level; afade does the fade-in.
        self.volume_db = fade["db"] if fade else 0.0
        self.stop_fade = threading.Event()   # ends a running fade early
        self.stamp: str | None = None        # the service's start stamp (see _valid_stamp)

    def set_db(self, db: float) -> None:
        self.ipc.command("set_property", "volume", db_to_mpv_volume(db))
        self.volume_db = db

    def ramp(self, target_db: float, seconds: float) -> bool:
        """Volume from the current level to target_db, linear in dB. False if
        stopped early or mpv is gone."""
        start = self.volume_db
        steps = max(1, round(seconds / FADE_STEP_SECONDS))
        for step in range(1, steps + 1):
            if self.stop_fade.wait(FADE_STEP_SECONDS):
                return False
            try:
                self.set_db(start + (target_db - start) * step / steps)
            except (OSError, ValueError):
                return False
        return True

    def terminate(self) -> bool:
        """End mpv at once. True if it was still playing."""
        self.stop_fade.set()
        playing = self.process.poll() is None
        if playing:
            # mpv first: a fade thread waiting for a reply then gets EOF
            # instead of blocking the pipe's close.
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        if self.ipc is not None:
            self.ipc.close()
        return playing


_audio_lock = threading.Lock()
_playback: _Playback | None = None
_fading_out: set[_Playback] = set()


def _stop_audio_locked() -> bool:
    """End the current playback and any still fading out, at once."""
    global _playback
    playing, _playback = _playback, None
    for old in list(_fading_out):
        old.terminate()
    _fading_out.clear()
    return playing.terminate() if playing is not None else False


def _connect_ipc(playback: _Playback, address: str) -> None:
    """Background: connect mpv's IPC for the fade-out on stop. Without it the
    music still plays; stop then ends it at once."""
    ipc = MpvIpc(address)
    try:
        ipc.connect(playback.process, IPC_CONNECT_SECONDS)
    except OSError as exc:
        if playback.process.poll() is None and not playback.stop_fade.is_set():
            logger.warning("Audio profile=%s plays without fade-out on stop: %s", playback.profile_id, exc)
        return
    with _audio_lock:
        if playback.stop_fade.is_set() or playback.process.poll() is not None:
            ipc.close()
            return
        playback.ipc = ipc
    logger.info("Audio profile=%s: control channel ready for the fade-out", playback.profile_id)


def _fade_out(playback: _Playback, seconds: float) -> None:
    # The full fade time from the profile's level; less if it is quieter.
    span = max(playback.fade["db"] - FADE_FLOOR_DB, 1.0)
    part = min(1.0, max(0.0, (playback.volume_db - FADE_FLOOR_DB) / span))
    playback.ramp(FADE_FLOOR_DB, seconds * part)
    with _audio_lock:
        if playback not in _fading_out:
            return  # a new track already ended it
        _fading_out.discard(playback)
        playback.terminate()
    logger.info("Audio faded out profile=%s", playback.profile_id)


# -- local copies of the audio (start without waiting) -----------------------
# The music lives on I:\GoogleDrive, a Google Drive drive that fetches a file
# only when it is opened: mpv then may wait until Drive has downloaded it
# before the first sound. A YouTube link likewise waits for yt-dlp and the
# stream. So the agent keeps a local copy of each audio source and plays that.
# Copies are made in the background: for the sources the service sends with
# /info (it asks on every open page every 30 s; each source is looked at most
# every PREFETCH_CHECK_SECONDS, a failed one again only after a growing
# pause), at agent start for the list the service sent last, and on a play
# that finds none (that play still uses the source).
# A file's copy is named after its source's size and modification time: a
# changed source never plays an old copy, and a copy mpv has open is never
# overwritten (Windows refuses that) - a new name is written instead.
AUDIO_CACHE_MAX_BYTES = 4 * 1024 ** 3
AUDIO_CACHE_MAX_FILE_BYTES = 1024 ** 3
AUDIO_CACHE_KEEP_FREE_BYTES = 2 * 1024 ** 3   # never fill the agent PC's disk
AUDIO_COPY_SUFFIXES = {".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".opus", ".wma"}
AUDIO_COPY_CHUNK = 1024 * 1024
YTDLP_DOWNLOAD_SECONDS = 900
PREFETCH_CHECK_SECONDS = 300.0      # a source sent with /info is looked at once per 5 min
PREFETCH_RETRY_SECONDS = 300.0      # after a failure; doubles each time ...
PREFETCH_RETRY_MAX_SECONDS = 3600.0  # ... up to one hour
MAX_PREFETCH_SOURCES = 20
_audio_cache_lock = threading.Lock()
_audio_cache_busy: set[str] = set()   # keys being copied right now
# key -> {"signature": (size, mtime_ns) or the link, "failures": n, "retry_at": monotonic}
_prefetch_failures: dict[str, dict] = {}
_sources_checked: dict[str, float] = {}  # source sent with /info -> when last looked at
_prefetching_sources = threading.Event()


def audio_cache_directory() -> Path:
    return agent_log_directory() / "audio-cache"


def _cache_key(source) -> str:
    import hashlib
    # Windows paths are case-insensitive; YouTube video ids are not.
    text = str(source).casefold() if isinstance(source, Path) else str(source)
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:20]


def _file_cache_name(target: Path, stat: os.stat_result) -> str:
    suffix = re.sub(r"[^A-Za-z0-9.]", "", target.suffix)[:10]
    return f"{_cache_key(target)}-{stat.st_size}-{stat.st_mtime_ns}{suffix}"


def _youtube_cache_prefix(url: str) -> str:
    return f"{_cache_key(url)}-yt."


def _in_progress(name: str) -> bool:
    """A copy still being made (or left behind by one that broke off)."""
    return name.endswith(".part") or "-download" in name


def cached_audio(source) -> Path | None:
    """The local copy to play for ``source`` (a Path to a file, or a YouTube
    link), or None while there is no current one."""
    directory = audio_cache_directory()
    if isinstance(source, Path):
        try:
            stat = source.stat()
        except OSError:
            return None
        candidate = directory / _file_cache_name(source, stat)
        try:
            return candidate if candidate.is_file() and candidate.stat().st_size == stat.st_size else None
        except OSError:
            return None
    prefix = _youtube_cache_prefix(source)
    try:
        matches = sorted(p for p in directory.glob(prefix + "*")
                         if p.is_file() and not _in_progress(p.name) and not p.name.endswith(".ytdl"))
    except OSError:
        return None
    return matches[0] if matches else None


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass  # mpv still plays it (Windows refuses), or it is gone already


def _drop_other_copies(key: str, keep: Path) -> None:
    """Older copies of the same source (one mpv still plays stays; the next
    trim gets it)."""
    for old in audio_cache_directory().glob(f"{key}-*"):
        if old != keep and not _in_progress(old.name):
            _unlink(old)


def _trim_audio_cache() -> None:
    """Delete leftovers of copies that broke off, and the least recently
    played copies above AUDIO_CACHE_MAX_BYTES."""
    files = []
    with _audio_cache_lock:
        busy = set(_audio_cache_busy)
    for path in audio_cache_directory().glob("*"):
        if path.name == SOURCES_FILE or path.name.endswith(".tmp"):
            continue
        if _in_progress(path.name):
            if path.name.split("-", 1)[0] not in busy:
                _unlink(path)
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        files.append((stat.st_mtime, stat.st_size, path))
    total = sum(size for _, size, _ in files)
    for _, size, path in sorted(files):
        if total <= AUDIO_CACHE_MAX_BYTES:
            break
        try:
            path.unlink()
        except OSError:
            continue
        total -= size


def _check_free_space(directory: Path, size: int) -> None:
    free = shutil.disk_usage(directory).free
    if free < size + AUDIO_CACHE_KEEP_FREE_BYTES:
        raise OSError(f"not enough free disk space for a local copy ({free} bytes free)")


def _copy_file_to_cache(target: Path) -> Path | None:
    if target.suffix.lower() not in AUDIO_COPY_SUFFIXES:
        return None  # plays from the source; only audio files are copied
    stat = target.stat()
    if stat.st_size > AUDIO_CACHE_MAX_FILE_BYTES:
        logger.info("Audio %s is too large to keep a local copy (%s bytes)", target, stat.st_size)
        return None
    directory = audio_cache_directory()
    directory.mkdir(parents=True, exist_ok=True)
    final = directory / _file_cache_name(target, stat)
    if final.is_file() and final.stat().st_size == stat.st_size:
        return final
    _check_free_space(directory, stat.st_size)
    part = final.with_name(final.name + ".part")
    started = time.monotonic()
    try:
        with open(target, "rb") as source, open(part, "wb") as copy:
            shutil.copyfileobj(source, copy, AUDIO_COPY_CHUNK)
        after = target.stat()
        if (part.stat().st_size, after.st_size, after.st_mtime_ns) != (stat.st_size, stat.st_size, stat.st_mtime_ns):
            raise OSError(f"{target} changed while it was copied")
        os.replace(part, final)
    finally:
        if part.exists():
            _unlink(part)
    _drop_other_copies(_cache_key(target), final)
    logger.info("Audio %s copied locally in %.1fs (%s bytes)", target, time.monotonic() - started, stat.st_size)
    return final


def _download_youtube_to_cache(url: str) -> Path | None:
    existing = cached_audio(url)
    if existing is not None:
        return existing
    mpv = _play_mpv()
    ytdlp = _play_ytdlp(mpv)
    if not ytdlp:
        raise OSError("yt-dlp was not found")
    directory = audio_cache_directory()
    directory.mkdir(parents=True, exist_ok=True)
    _check_free_space(directory, AUDIO_CACHE_MAX_FILE_BYTES)
    key = _cache_key(url)
    temp = directory / f"{key}-download"
    started = time.monotonic()
    try:
        result = subprocess.run(
            [ytdlp, "--ignore-config", "--no-playlist", "--no-progress", "--quiet", "--no-warnings",
             "-f", "bestaudio/best", "--max-filesize", str(AUDIO_CACHE_MAX_FILE_BYTES),
             "-o", f"{temp}.%(ext)s", "--", url],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=YTDLP_DOWNLOAD_SECONDS,
        )
        done = [p for p in directory.glob(f"{key}-download.*")
                if p.is_file() and not p.name.endswith((".part", ".ytdl"))]
        if result.returncode != 0 or not done:
            detail = result.stderr.decode("utf-8", "replace").strip().splitlines()[-1:] if result.stderr else []
            raise OSError(f"yt-dlp could not download {url}: exit {result.returncode} {' '.join(detail)}"[:300])
        final = directory / (_youtube_cache_prefix(url) + done[0].suffix.lstrip("."))
        os.replace(done[0], final)
    finally:
        for leftover in directory.glob(f"{key}-download*"):
            _unlink(leftover)
    logger.info("Audio %s downloaded locally in %.1fs", url, time.monotonic() - started)
    return final


def _source_signature(source):
    """What a failure is remembered for: a file's size and modification time
    (a changed file is tried again at once), or the link."""
    if isinstance(source, Path):
        stat = source.stat()
        return (stat.st_size, stat.st_mtime_ns)
    return source


def _prefetch_now(source) -> None:
    key = _cache_key(source)
    try:
        signature = _source_signature(source)
    except OSError as exc:
        logger.warning("No local copy of %s: %s", source, exc)
        return
    now = time.monotonic()
    with _audio_cache_lock:
        failure = _prefetch_failures.get(key)
        if key in _audio_cache_busy or (
                failure and failure["signature"] == signature and now < failure["retry_at"]):
            return
        _audio_cache_busy.add(key)
    ok = False
    try:
        if isinstance(source, Path):
            _copy_file_to_cache(source)
        else:
            _download_youtube_to_cache(source)
        ok = True
        _trim_audio_cache()
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("No local copy of %s: %s", source, exc)
    except Exception:  # noqa: BLE001 - a background copy must never break the agent
        logger.exception("Local copy of %s failed", source)
    finally:
        with _audio_cache_lock:
            _audio_cache_busy.discard(key)
            if ok:
                _prefetch_failures.pop(key, None)
            else:
                old = _prefetch_failures.get(key)
                failures = old["failures"] + 1 if old and old["signature"] == signature else 1
                pause = min(PREFETCH_RETRY_SECONDS * 2 ** (failures - 1), PREFETCH_RETRY_MAX_SECONDS)
                _prefetch_failures[key] = {"signature": signature, "failures": failures,
                                           "retry_at": time.monotonic() + pause}


def prefetch_audio(source) -> threading.Thread | None:
    """Make the local copy of ``source`` in the background (None: it is
    current already or being made)."""
    if cached_audio(source) is not None:
        return None
    with _audio_cache_lock:
        if _cache_key(source) in _audio_cache_busy:
            return None
    thread = threading.Thread(target=_prefetch_now, args=(source,), name="audio-copy", daemon=True)
    thread.start()
    return thread


# The list of sources the service sent last, kept for the agent's next start.
SOURCES_FILE = "sources.json"


def _remember_sources(sources: list) -> None:
    path = audio_cache_directory() / SOURCES_FILE
    try:
        if path.is_file() and json.loads(path.read_text(encoding="utf-8")) == sources:
            return
    except (OSError, ValueError):
        pass
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(SOURCES_FILE + ".tmp")
        temp.write_text(json.dumps(sources), encoding="utf-8")
        os.replace(temp, path)
    except OSError:
        logger.warning("Could not keep the audio sources for the next start")


def _prefetch_sources_now(sources: list, throttle: bool = True) -> None:
    try:
        for source in sources:
            now = time.monotonic()
            if throttle and now - _sources_checked.get(source, -PREFETCH_CHECK_SECONDS) < PREFETCH_CHECK_SECONDS:
                continue
            _sources_checked[source] = now
            try:
                target = _audio_target(source)
            except ValueError:
                continue  # not there (yet): the play reports it
            if cached_audio(target) is None:
                _prefetch_now(target)
    except Exception:  # noqa: BLE001 - a background copy must never break the agent
        logger.exception("Local copies of the audio sources failed")
    finally:
        _prefetching_sources.clear()


def prefetch_sources(sources: list) -> threading.Thread | None:
    """The sources sent with /info: kept for the next start, and their local
    copies made one after the other in one background thread. Each source
    is looked at once per PREFETCH_CHECK_SECONDS; None if a run is going."""
    sources = [s for s in sources[:MAX_PREFETCH_SOURCES] if isinstance(s, str) and s]
    if _prefetching_sources.is_set():
        return None
    _prefetching_sources.set()
    thread = threading.Thread(target=lambda: (_remember_sources(sources), _prefetch_sources_now(sources)),
                              name="audio-copy", daemon=True)
    thread.start()
    return thread


def refresh_audio_cache() -> None:
    """At agent start: the copies of the sources the service sent last (a
    file changed on Drive meanwhile, or a copy cut off by a restart)."""
    _trim_audio_cache()  # leftovers of copies a restart cut off
    try:
        sources = json.loads((audio_cache_directory() / SOURCES_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if not isinstance(sources, list):
        return
    _prefetching_sources.set()
    _prefetch_sources_now([s for s in sources[:MAX_PREFETCH_SOURCES] if isinstance(s, str) and s])


def _audio_target(path: str):
    """A YouTube link as it is, or the resolved Path of an audio file."""
    if is_youtube_url(path):
        return path
    try:
        target = resolve_path(path)
    except FileNotFoundError:
        raise ValueError("Configured audio file is not available") from None
    if not target.is_absolute() or not target.is_file():
        raise ValueError("Configured audio file is not available")
    return target


def _play_audio(profile_id: str, path: str, fade: dict | None = None, stamp: str | None = None,
                keep_if_playing: bool = False) -> bool:
    """Start the profile's track (any other one ends). With keep_if_playing a
    track of the same profile that still plays is kept as it is: no restart,
    no fade-in; it takes the new stamp. Returns True when it was kept."""
    global _playback
    if keep_if_playing:
        with _audio_lock:
            playback = _playback
            if playback is not None and playback.profile_id == profile_id and playback.process.poll() is None:
                playback.stamp = stamp
                logger.info("Audio kept playing profile=%s", profile_id)
                return True
    target = _audio_target(path)
    local = cached_audio(target)
    if local is not None:
        try:
            os.utime(local)  # most recently played: trimmed last
        except OSError:
            pass
    else:
        # Plays the source this time; the next start plays the copy.
        prefetch_audio(target)
    stream = local is None and isinstance(target, str)
    mpv = _play_mpv()
    if not mpv:
        _, searched = find_tool("mpv", "MPV_PATH", "mpv", "mpv")
        raise FileNotFoundError(missing_tool_message("mpv", "MPV_PATH", searched, "winget install mpv"))
    ytdlp = _play_ytdlp(mpv) if stream else None
    if stream and not ytdlp:
        _, searched = find_tool("yt-dlp", "YTDLP_PATH", "yt-dlp", "yt-dlp", near=(Path(mpv).parent,))
        raise FileNotFoundError(missing_tool_message(
            "yt-dlp", "YTDLP_PATH", searched, "winget install yt-dlp.yt-dlp, or put yt-dlp.exe next to mpv.exe"))

    startup_info = None
    if os.name == "nt":
        startup_info = subprocess.STARTUPINFO()
        startup_info.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup_info.wShowWindow = subprocess.SW_HIDE

    command = [
        mpv,
        "--no-config",
        "--no-video",
        "--vo=null",
        "--audio-display=no",
        "--force-window=no",
        "--no-terminal",
        "--really-quiet",
        # mpv's own reasons (no audio device, unreadable file, ...) for the
        # error message and agent support.
        f"--log-file={mpv_log_path()}",
    ]
    if stream:
        # Sound only, one video (no playlist), fetched by the yt-dlp found here.
        command += ["--ytdl=yes", "--ytdl-format=bestaudio/best", "--ytdl-raw-options=no-playlist=",
                    f"--script-opts=ytdl_hook-ytdl_path={ytdlp}"]
    address = _ipc_address() if fade else None
    if fade:
        command += [
            f"--volume={db_to_mpv_volume(fade['db']):.3f}",
            # Fade-in by mpv itself, from the first sound on (see above).
            f"--af=lavfi=[afade=t=in:d={fade['seconds']:g}]",
            f"--input-ipc-server={address}",
        ]
    with _audio_lock:
        _stop_audio_locked()
        process = subprocess.Popen(
            command + ["--", str(local or target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            startupinfo=startup_info,
        )
        playback = _Playback(profile_id, process, None, fade, stream)
        # Nothing waits for mpv: catch a player that ends straight away (no
        # audio device, unreadable file) here.
        time.sleep(START_CHECK_SECONDS)
        if process.poll() is not None:
            raise _start_failure(process, "mpv ended at once")
        playback.stamp = stamp
        _playback = playback
        if fade:
            threading.Thread(target=_connect_ipc, args=(playback, address), daemon=True).start()
    logger.info("Audio started profile=%s path=%s local=%s pid=%s fade=%s",
                profile_id, target, local, process.pid, fade)
    return False


def _stop_audio(fade: dict | None = None, only_profile: str | None = None, stamp: str | None = None) -> bool:
    """Stop playback; with a fade (and a track started with one) it fades
    out in the background and the call returns at once. With only_profile
    only that profile's track, and with a stamp only the start it names: a
    newer track (or a newer start of the same one) keeps playing."""
    global _playback
    with _audio_lock:
        playback = _playback
        if only_profile is not None and (
                playback is None or playback.profile_id != only_profile
                or (stamp is not None and playback.stamp != stamp)):
            logger.info("Audio stop for profile=%s ignored: %s plays", only_profile,
                        playback.profile_id if playback else "nothing")
            return False
        if fade and playback is not None and playback.ipc is not None and playback.process.poll() is None:
            _playback = None
            _fading_out.add(playback)
            threading.Thread(target=_fade_out, args=(playback, fade["seconds"]), daemon=True).start()
            logger.info("Audio fading out profile=%s over %.1fs", playback.profile_id, fade["seconds"])
            return True
        stopped = _stop_audio_locked()
    logger.info("Audio stopped active=%s", stopped)
    return stopped


def agent_version() -> str:
    try:
        return VERSION_FILE.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _valid_update_request(body) -> bool:
    if not isinstance(body, dict) or set(body) != {"version", "files"}:
        return False
    files = body["files"]
    return (
        isinstance(body["version"], str) and bool(_VERSION_RE.match(body["version"]))
        and isinstance(files, dict) and "show_image_agent.py" in files
        and all(isinstance(name, str) and _UPDATE_FILE_RE.match(name) and isinstance(data, str)
                for name, data in files.items())
    )


def _install_update(body: dict) -> None:
    """Replace the agent files. Everything is decoded and the Python files are
    compiled first, so a broken upload leaves the installed agent untouched."""
    decoded = {name: base64.b64decode(data, validate=True) for name, data in body["files"].items()}
    for name, data in decoded.items():
        if name.endswith(".py"):
            compile(data, name, "exec")
    for name, data in decoded.items():
        partial = AGENT_DIR / f".{name}.new"
        partial.write_bytes(data)
        os.replace(partial, AGENT_DIR / name)
    VERSION_FILE.write_text(body["version"], encoding="utf-8")
    logger.info("Agent files updated to %s (%s files)", body["version"], len(decoded))


RESTART_FADE_SECONDS = 1.5


def stop_audio_for_restart(seconds: float = RESTART_FADE_SECONDS) -> bool:
    """End the music before this process ends: the new agent would not know a
    player still running (Stop would not reach it, Play would start a second
    one). With mpv's control channel a short fade-out, otherwise at once.
    True if something was playing."""
    global _playback
    with _audio_lock:
        playback, _playback = _playback, None
        fading = list(_fading_out)
        _fading_out.clear()
    for old in fading:
        old.terminate()
    if playback is None:
        return bool(fading)
    if playback.ipc is not None and playback.process.poll() is None:
        playback.ramp(FADE_FLOOR_DB, seconds)
    playback.terminate()
    logger.info("Audio ended for the agent restart profile=%s", playback.profile_id)
    return True


def _restart_agent() -> None:
    """Start the agent again (now with the new files) and end this process."""
    try:
        stop_audio_for_restart()
    except Exception:  # noqa: BLE001 - the restart must happen anyway
        logger.exception("Could not end the music before the restart")
    script = str(Path(__file__).resolve())
    started = False
    if os.name == "nt":
        # restart-agent.ps1 waits until this process is gone, then starts the
        # scheduled task again. WMI creates it outside this process (and its
        # Task Scheduler job), so it survives our exit.
        helper = AGENT_DIR / "restart-agent.ps1"
        inner = (f'powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "{helper}" '
                 f'-Python "{sys.executable}" -Script "{script}"')
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                 "$r = Invoke-CimMethod -ClassName Win32_Process -MethodName Create "
                 "-Arguments @{CommandLine = $env:MIXER_AGENT_RESTART}; exit $r.ReturnValue"],
                env={**os.environ, "MIXER_AGENT_RESTART": inner},
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=30, check=False,
            )
            started = result.returncode == 0
        except (OSError, subprocess.SubprocessError):
            logger.exception("Restart helper could not be started")
    if not started:
        flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        subprocess.Popen([sys.executable, script], creationflags=flags, start_new_session=os.name != "nt",
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    logger.info("Agent restarts with version %s", agent_version())
    logging.shutdown()
    os._exit(0)


def _schedule_shutdown() -> None:
    timer = threading.Timer(2, _shutdown_agent_pc)
    timer.daemon = True
    timer.start()


def _shutdown_agent_pc() -> None:
    username = os.environ.get("USERNAME") or getpass.getuser()
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    commands = [
        ["shutdown.exe", "/s", "/t", "10"],
        ["taskkill.exe", "/F", "/FI", f"USERNAME eq {username}", "/FI", "STATUS eq RUNNING"],
    ]
    for command in commands:
        try:
            subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=flags,
            )
        except OSError:
            logger.exception("Could not start agent shutdown command %s", command[0])


def _show_image(root: tk.Tk, image_label: tk.Label, profile: dict) -> None:
    """Show the image on its monitor; with "monitors" the same image on each
    of them (the first in the main window, the others in extra windows)."""
    monitors = profile.get("monitors") or [profile["monitor"]]
    for monitor in monitors:  # all monitors present, or nothing is shown
        monitor_bounds(monitor)
    # The same further monitors: their windows stay (and show the next image
    # without disappearing in between).
    if [getattr(window, "monitor", None) for window in _mirrors] != monitors[1:]:
        _close_mirrors()
    _show_widget(root, image_label)
    _show_image_on(root, image_label, profile, monitors[0])
    for index, monitor in enumerate(monitors[1:]):
        if index < len(_mirrors):
            window = _mirrors[index]
        else:
            window = tk.Toplevel(root, background="black")
            window.withdraw()
            window.protocol("WM_DELETE_WINDOW", lambda: _hide_all(root))
            window.bind("<Escape>", lambda _event: _hide_all(root))
            window.image_label = tk.Label(window, background="black", borderwidth=0)
            window.image_label.pack(fill="both", expand=True)
            window.monitor = monitor
            _mirrors.append(window)
        _show_image_on(window, window.image_label, profile, monitor)


# Extra windows showing the current image on further monitors.
_mirrors: list[tk.Toplevel] = []


def _close_mirrors() -> None:
    while _mirrors:
        window = _mirrors.pop()
        _window_style.pop(str(window), None)
        window.destroy()


# How each agent window was last set up (full screen: no frame, topmost) and
# where the song window is. Changing that needs the window hidden for a
# moment (Tk changes the frame only while it is hidden), so it is done only
# when it changes: the next slide or image never uncovers what is behind.
_window_style: dict[str, bool] = {}
_song_place: tuple | None = None


def _keeps_style(window, fullscreen: bool) -> bool:
    """The window is on screen with that frame (or none) already."""
    try:
        showing = window.state() == "normal"
    except tk.TclError:
        return False
    return showing and _window_style.get(str(window)) == fullscreen


def _set_style(window, fullscreen: bool) -> bool:
    """Frame and topmost for a window about to be shown; hides it for that
    only when they change. True when it did (the frame changed)."""
    if _keeps_style(window, fullscreen):
        return False
    window.withdraw()
    window.overrideredirect(fullscreen)
    window.attributes("-topmost", fullscreen)
    _window_style[str(window)] = fullscreen
    return True


def _hide_all(root: tk.Tk) -> None:
    """Escape or the window's close button: hide, never end the agent."""
    global _shown_image
    _shown_image = None
    _close_mirrors()
    root.withdraw()


def _show_image_on(root: tk.Wm, image_label: tk.Label, profile: dict, monitor: int) -> None:
    fullscreen = profile["fullscreen"]
    # A framed window keeps the taskbar free, like a window snapped into a corner.
    left, top, right, bottom = monitor_bounds(monitor) if fullscreen else monitor_work_area(monitor)
    with Image.open(profile["path"]) as opened:
        source = opened.convert("RGB")

    def fitted(width: int, height: int) -> Image.Image:
        if fullscreen:
            return ImageOps.fit(source, (width, height))
        image = source.copy()
        image.thumbnail((max(1, width), max(1, height)), Image.Resampling.LANCZOS)
        return image

    image = fitted(right - left, bottom - top)
    photo = ImageTk.PhotoImage(image)
    restyled = _set_style(root, fullscreen)
    x, y = _window_position(
        (left, top, right, bottom), image.width, image.height, profile["position"]
    )
    logger.info(
        "Preparing image path=%s size=%sx%s monitor_bounds=%s position=%s fullscreen=%s",
        profile["path"],
        image.width,
        image.height,
        (left, top, right, bottom),
        profile["position"],
        profile["fullscreen"],
    )
    root.geometry(f"{image.width}x{image.height}+{x}+{y}")
    image_label.configure(image=photo)
    image_label.image = photo
    root.deiconify()
    root.state("normal")
    root.update_idletasks()
    root.geometry(f"{image.width}x{image.height}+{x}+{y}")
    root.lift()
    hwnd = root.winfo_id()
    if not hwnd:
        raise RuntimeError("Image window was not found")
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    parent = user32.GetParent(hwnd)
    if parent:
        hwnd = parent
    outer = wintypes.RECT()
    client = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(outer)):
        raise ctypes.WinError()
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        raise ctypes.WinError()
    frame_width = (outer.right - outer.left) - (client.right - client.left)
    frame_height = (outer.bottom - outer.top) - (client.bottom - client.top)
    if fullscreen:
        window_width, window_height = right - left, bottom - top
        x, y = left, top
    else:
        invisible = _invisible_border(hwnd)
        visible_frame_width = frame_width - invisible[0] - invisible[2]
        visible_frame_height = frame_height - invisible[1] - invisible[3]
        if profile["position"] == "center":
            resized = fitted(right - left - visible_frame_width, bottom - top - visible_frame_height)
        else:
            # A corner gets a quarter of the work area, as Windows' corner snap does.
            quarter_width, quarter_height = (right - left) // 2, (bottom - top) // 2
            resized = ImageOps.contain(source, (
                max(1, quarter_width - visible_frame_width), max(1, quarter_height - visible_frame_height)
            ), Image.Resampling.LANCZOS)
        if resized.size != image.size:
            image = resized
            photo = ImageTk.PhotoImage(image)
            image_label.configure(image=photo)
            image_label.image = photo
        if profile["position"] == "center":
            window_width = image.width + frame_width
            window_height = image.height + frame_height
        else:
            window_width = quarter_width + invisible[0] + invisible[2]
            window_height = quarter_height + invisible[1] + invisible[3]
        x, y = _framed_window_position(
            (left, top, right, bottom), window_width, window_height, invisible, profile["position"]
        )
    insert_after = -1 if profile["fullscreen"] else -2
    # SWP_SHOWWINDOW, and SWP_FRAMECHANGED only when the frame was just taken away.
    flags = 0x0040 | (0x0020 if profile["fullscreen"] and restyled else 0)
    if not user32.SetWindowPos(
        hwnd, insert_after, x, y, window_width, window_height, flags
    ):
        raise ctypes.WinError()
    positioned = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(positioned)):
        raise ctypes.WinError()
    logger.info(
        "Positioned image hwnd=%s monitor=%s fullscreen=%s position=%s target=%s,%s,%s,%s rect=%s,%s,%s,%s",
        hwnd,
        monitor,
        profile["fullscreen"],
        profile["position"],
        x,
        y,
        window_width,
        window_height,
        positioned.left,
        positioned.top,
        positioned.right,
        positioned.bottom,
    )


def _read_song_library(path: str) -> tuple[list[dict], bool]:
    """All .foil files of a FoilPresenter library (its "Songs" folder, or the
    folder itself), and PowerPoint songs (.pptx: only the XML of their slides,
    the service takes the text from it). Raises FileNotFoundError when the
    folder is missing."""
    folder = _songs_folder(path)
    files, truncated = [], False
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        suffix = entry.suffix.lower()
        if suffix not in (".foil", ".pptx") or not entry.is_file() or entry.name.startswith("~$"):
            continue
        if len(files) >= MAX_SONG_FILES:
            truncated = True
            break
        if suffix == ".pptx":
            parts = _pptx_parts(entry)
            if parts:
                files.append({"name": entry.name, "pptx_parts": parts})
            continue
        if entry.stat().st_size > MAX_SONG_FILE_BYTES:
            continue
        files.append({"name": entry.name, "xml": entry.read_bytes().decode("utf-8", errors="replace")})
    return files, truncated


MAX_PPTX_SLIDES = 200
MAX_PPTX_PART_BYTES = 1024 * 1024
MAX_PPTX_XML_BYTES = 4 * 1024 * 1024
_PPTX_PART_RE = re.compile(r"^ppt/(?:presentation\.xml|_rels/presentation\.xml\.rels|slides/slide\d{1,4}\.xml)$")


def _pptx_parts(file: Path) -> dict | None:
    """The presentation, its relations and the slides as XML text (pictures
    and media are left in the file); None for a file that is no readable
    .pptx or too big unpacked."""
    import zipfile

    try:
        with zipfile.ZipFile(file) as archive:
            infos = [info for info in archive.infolist() if _PPTX_PART_RE.match(info.filename)]
            slides = sum(1 for info in infos if info.filename.startswith("ppt/slides/"))
            # Sizes as unpacked, checked before anything is unpacked.
            if (slides > MAX_PPTX_SLIDES or any(info.file_size > MAX_PPTX_PART_BYTES for info in infos)
                    or sum(info.file_size for info in infos) > MAX_PPTX_XML_BYTES):
                logger.warning("PowerPoint song skipped (too big): %s", file)
                return None
            return {info.filename: archive.read(info).decode("utf-8", errors="replace") for info in infos}
    except (zipfile.BadZipFile, OSError, KeyError):
        logger.warning("PowerPoint song could not be read: %s", file)
        return None


def _songs_folder(path: str) -> Path:
    folder = resolve_path(path, folder=True)
    if not folder.is_absolute() or not folder.is_dir():
        raise FileNotFoundError(path)
    return folder / "Songs" if (folder / "Songs").is_dir() else folder


def _save_song(path: str, name: str, xml: str) -> Path:
    """Write a new song into the library; an existing file is never replaced
    (FileExistsError)."""
    if not _SONG_NAME_RE.match(name) or name.strip(" .") != name or ".." in name:
        raise ValueError("invalid song file name")
    data = xml.encode("utf-8")
    if not data or len(data) > MAX_SONG_FILE_BYTES:
        raise ValueError("invalid song size")
    target = _songs_folder(path) / name
    with open(target, "xb") as handle:  # "x": fails if the file exists
        handle.write(data)
    return target


SONG_REQUEST_BYTES = 262144
MAX_LAYOUT_LINES = 200


def _valid_lines(lines) -> bool:
    return (isinstance(lines, list) and len(lines) <= MAX_LAYOUT_LINES
            and all(isinstance(line, str) and len(line) <= 2000 for line in lines))


def _valid_size(px) -> bool:
    return isinstance(px, (int, float)) and not isinstance(px, bool) and 1 <= px <= 1000


def _valid_layout(layout) -> bool:
    """"layout": what the service laid out for this screen: the window size
    it measured ("screen"), the text size and lines, the grey preview."""
    if not isinstance(layout, dict) or set(layout) != {"screen", "px", "lines", "preview"}:
        return False
    screen, preview = layout["screen"], layout["preview"]
    return (
        isinstance(screen, list) and len(screen) == 2
        and all(type(v) is int and 100 <= v <= 20000 for v in screen)
        and _valid_size(layout["px"]) and _valid_lines(layout["lines"])
        and (preview is None or (isinstance(preview, dict) and set(preview) == {"px", "lines"}
                                 and _valid_size(preview["px"]) and _valid_lines(preview["lines"])))
    )


SONG_KEYS = {"action", "monitor", "fullscreen", "style", "text", "title", "copyright", "next", "layout"}
SONG_STYLE_KEYS = {"font", "font_size", "text_color", "background_color", "align"}


def _known_song_fields(body) -> bool:
    """Only fields this agent knows: a request it refuses all the same has a
    value it does not take (422), not a newer field (400: the service then
    sends an older form)."""
    return (isinstance(body, dict) and set(body) <= SONG_KEYS
            and (not isinstance(body.get("style"), dict) or set(body["style"]) <= SONG_STYLE_KEYS))


def _valid_song_request(body) -> bool:
    if not isinstance(body, dict) or body.get("action") not in ("show", "blank", "hide"):
        return False
    if body["action"] == "hide":
        return set(body) == {"action"}
    style = body.get("style")
    base = {"action", "monitor", "fullscreen", "style", "text", "title", "copyright"}
    style_base = {"font", "font_size", "text_color", "background_color"}
    # "next" (preview of the next slide), "layout" (lines and size laid out
    # by the service) and style "align" are newer and optional, so an older
    # service still works with this agent.
    return (
        base <= set(body) <= base | {"next", "layout"}
        and ("layout" not in body or _valid_layout(body["layout"]))
        and isinstance(body["monitor"], int) and not isinstance(body["monitor"], bool) and body["monitor"] >= 1
        and isinstance(body["fullscreen"], bool)
        and all(isinstance(body.get(k, ""), str) and len(body.get(k, "")) <= 8000
                for k in ("text", "title", "copyright", "next"))
        and isinstance(style, dict)
        and style_base <= set(style) <= style_base | {"align"}
        and isinstance(style["font"], str) and 0 < len(style["font"]) <= 60
        and isinstance(style["font_size"], int) and 12 <= style["font_size"] <= 240
        and all(isinstance(style[k], str) and _COLOR_RE.match(style[k]) for k in ("text_color", "background_color"))
        and style.get("align", "center") in ("left", "center", "right")
    )


# Slide layout. Keep in sync with renderSlide() in app/static/js/songs.js,
# which draws the 16:9 preview the same way.
SLIDE_PAD_X, SLIDE_PAD_Y = 0.06, 0.07
SLIDE_TITLE_H, SLIDE_COPY_H = 0.12, 0.08
SLIDE_LINE = 1.25
# Smallest lyrics size in px on a 1080 px high screen (never above the set
# size): smaller is unreadable from the back. Text that does not fit even
# then is wrapped, not shrunk further (and cut at the bottom).
SLIDE_MIN_PX = 40
# Preview of the next slide: grey (text colour mixed into the background),
# at most half the slide's text size and never below 20 px on 1080 lines; only
# in the room left under the centred text, which never moves for it.
PREVIEW_SCALE, PREVIEW_MIN, PREVIEW_GAP, PREVIEW_MIX = 0.5, 20, 0.5, 0.45


# The file's own lines are kept only if that costs at most 15 % of the size
# the wrapped text would get; otherwise the slides of one song would jump
# between small and full size.
OWN_LINES_KEEP = 0.85
# Repeat marks stay with their word, never alone on a line.
REPEAT_OPEN, REPEAT_CLOSE = "|:", ":|"


def _next_word(words: list[str], start: int) -> int:
    """Index of the first non-empty word from start on (double spaces give
    empty ones), or len(words)."""
    while start < len(words) and words[start] == "":
        start += 1
    return start


def _word_units(paragraph: str) -> list[str]:
    """The paragraph's words; a repeat mark is joined to its neighbour so a
    line break never separates them: "|:" to the word after it ("|: Alles"),
    ":|" to the one before ("bleibst. :|"), also across double spaces; a
    mark with no word on that side takes the one on the other side
    ("singen dir |:"). Joined with spaces the units give the paragraph back.
    Same as slideWordUnits() in app/static/js/songs.js."""
    words = paragraph.split(" ")
    units: list[str] = []
    i = 0
    while i < len(words):
        word = words[i]
        if word in (REPEAT_OPEN, REPEAT_CLOSE):
            forward = _next_word(words, i + 1)
            back = len(units)
            while back > 0 and units[back - 1] == "":
                back -= 1
            if word == REPEAT_OPEN and forward < len(words) or word == REPEAT_CLOSE and back == 0 and forward < len(words):
                units.append(" ".join(words[i:forward + 1]))
                i = forward + 1
                continue
            if back > 0:
                units[back - 1:] = [" ".join(units[back - 1:] + [word])]
                i += 1
                continue
        units.append(word)
        i += 1
    return units


def _wrap_lines(measure, paragraphs: list[str], px: float, width: float) -> list[str]:
    """Each line of the text, broken at spaces where it is wider than width
    (like FoilPresenter, whose files often keep a whole stanza on one line).
    A word wider than the line on its own is broken between letters.
    measure(text, size) gives the width of text at that pixel size.
    Same as wrapSlideLines() in app/static/js/songs.js."""
    out = []
    for paragraph in paragraphs:
        if measure(paragraph, px) <= width:
            out.append(paragraph)
            continue
        line = ""
        for word in _word_units(paragraph):
            candidate = word if not line else f"{line} {word}"
            if not line or measure(candidate, px) <= width:
                line = candidate
            else:
                out.append(line)
                line = word
            while len(line) > 1 and measure(line, px) > width:
                cut = next((k for k in range(len(line) - 1, 0, -1) if measure(line[:k], px) <= width), 1)
                out.append(line[:cut])
                line = line[cut:]
        out.append(line)
    return out


def _slide_sizes(max_px: float, min_px: float):
    """The sizes tried, largest first (the page tries the same ones)."""
    px = max_px
    while px >= min_px:
        yield px
        px *= 0.94


def _fit_size(measure, text: str, max_px: float, min_px: float, box_w: float, box_h: float):
    """The largest size where the lyrics fit, or None if they do not fit even
    at min_px: wrapped at the largest size where the text fits; the file's own
    lines instead if they fit at OWN_LINES_KEEP of that size or more.
    _wrap_lines() at that size gives the lines (the own ones where they fit).
    Only for a service from before it laid songs out itself (no "layout")."""
    paragraphs = text.split("\n")
    min_px = min(min_px, max_px)
    own = next((px for px in _slide_sizes(max_px, min_px)
                if max(measure(line, px) for line in paragraphs) <= box_w
                and len(paragraphs) * px * SLIDE_LINE <= box_h), None)
    wrapped = next((px for px in _slide_sizes(max_px, min_px)
                    if len(_wrap_lines(measure, paragraphs, px, box_w)) * px * SLIDE_LINE <= box_h), None)
    if own is not None and (wrapped is None or own >= OWN_LINES_KEEP * wrapped):
        return own
    return wrapped


def _fit_text(measure, text: str, max_px: float, min_px: float, box_w: float, box_h: float):
    """(size, lines) of one slide on its own: at _fit_size(), at min_px
    wrapped regardless where nothing fits (an older service)."""
    px = _fit_size(measure, text, max_px, min_px, box_w, box_h)
    if px is None:
        px = min(min_px, max_px)
    return px, _wrap_lines(measure, text.split("\n"), px, box_w)


def _slide_box(width: float, height: float, title: bool, copyright: bool):
    """(box_w, box_h) of the lyrics: the screen less the margins and the
    title and copyright rows of this slide (app/slide_layout.slide_box)."""
    box_w = width - 2 * SLIDE_PAD_X * width
    box_h = (height - 2 * SLIDE_PAD_Y * height - (SLIDE_TITLE_H * height if title else 0)
             - (SLIDE_COPY_H * height if copyright else 0))
    return box_w, box_h


_LAYOUT_MISMATCH_LOGGED: set = set()


def _slide_layout(measure, body: dict, width: int, height: int, box_w: float, box_h: float):
    """(size, lines, preview) to draw: the service's layout where it was
    made for this window size (a line that would run past the margin is
    wrapped all the same: a font the service measured differently); else,
    from an older service, the slide fitted on its own as before."""
    layout = body.get("layout")
    if layout is not None and layout["screen"] == [width, height]:
        px = layout["px"]
        lines = _wrap_lines(measure, layout["lines"], px, box_w + SLIDE_PAD_X * width)
        preview = layout["preview"]
        return px, lines, (preview["px"], preview["lines"]) if preview else None
    if layout is not None and (tuple(layout["screen"]), width, height) not in _LAYOUT_MISMATCH_LOGGED:
        # Once per size pair (the service lays out for the new size from the next slide on).
        _LAYOUT_MISMATCH_LOGGED.add((tuple(layout["screen"]), width, height))
        logger.warning("Song layout made for %s, window is %sx%s: fitting the slide here",
                       layout["screen"], width, height)
    px, lines = _fit_text(measure, body["text"], body["style"]["font_size"] * height / 1080,
                          SLIDE_MIN_PX * height / 1080, box_w, box_h)
    next_text = body.get("next", "")
    preview = _preview_layout(measure, lines, next_text.split("\n") if next_text else [], px, box_w, box_h, height)
    return px, lines, preview


def _visible_lines(count: int, top: float, px: float, bottom: float) -> int:
    """How many of the lines fit above bottom (the text never runs into the
    copyright). Same as in renderSlide()."""
    return max(0, min(count, int((bottom - top - px) // (px * SLIDE_LINE)) + 1)) if bottom - top >= px else 0


def _mix_color(fg: str, bg: str, amount: float) -> str:
    """fg mixed into bg: amount 0 = bg, 1 = fg (both #rrggbb)."""
    a, b = int(fg[1:], 16), int(bg[1:], 16)
    parts = [round(((b >> s) & 255) + (((a >> s) & 255) - ((b >> s) & 255)) * amount) for s in (16, 8, 0)]
    return "#" + "".join(f"{p:02x}" for p in parts)


def _preview_layout(measure, lines: list[str], next_lines: list[str], px: float, box_w: float,
                    box_h: float, height: int):
    """(size, lines) of the next-slide preview, or None if it does not fit:
    half the text size (at least PREVIEW_MIN), its lines wrapped like the
    lyrics, as many as fit under the text. measure(text, size) gives the
    width of text at that pixel size."""
    if not next_lines:
        return None
    pp = max(PREVIEW_SCALE * px, PREVIEW_MIN * height / 1080)
    wrapped = _wrap_lines(measure, next_lines, pp, box_w)
    free = (box_h - len(lines) * px * SLIDE_LINE) / 2 - PREVIEW_GAP * px
    count = min(len(wrapped), int(free // (pp * SLIDE_LINE)) if free > 0 else 0)
    return (pp, wrapped[:count]) if count >= 1 else None


def _draw_slide(canvas: tk.Canvas, width: int, height: int, body: dict) -> None:
    """The slide replaces the one on the canvas: its items are made first and
    the old ones deleted after them, in the same Tk call (never an empty or
    half-drawn canvas on screen)."""
    style = body["style"]
    old = canvas.find_all()
    canvas.configure(background=style["background_color"])
    try:
        _draw_slide_items(canvas, width, height, body)
    finally:
        if old:
            canvas.delete(*old)


def _draw_slide_items(canvas: tk.Canvas, width: int, height: int, body: dict) -> None:
    style = body["style"]
    if body["action"] == "blank":
        return
    align = style.get("align", "center")
    pad_x, pad_y = SLIDE_PAD_X * width, SLIDE_PAD_Y * height
    title_h = SLIDE_TITLE_H * height if body["title"] else 0
    copy_h = SLIDE_COPY_H * height if body["copyright"] else 0
    box_w, box_h = _slide_box(width, height, bool(body["title"]), bool(body["copyright"]))
    # x and anchors for the alignment: line tops ("n") and centred blocks
    x = {"left": pad_x, "center": width / 2, "right": width - pad_x}[align]
    top_anchor = {"left": "nw", "center": "n", "right": "ne"}[align]
    mid_anchor = {"left": "w", "center": "center", "right": "e"}[align]
    fonts = {}

    def measure(text: str, size: float) -> int:
        key = max(1, round(size))
        if key not in fonts:
            fonts[key] = tkfont.Font(family=style["font"], size=-key)
        return fonts[key].measure(text)

    px, lines, preview = _slide_layout(measure, body, width, height, box_w, box_h)
    font = tkfont.Font(family=style["font"], size=-max(1, round(px)))
    # Centred in the box; text taller than the box starts at its top.
    top = pad_y + title_h + max(0.0, (box_h - len(lines) * px * SLIDE_LINE) / 2)
    # Text taller than its box is cut at the box's bottom edge.
    shown = _visible_lines(len(lines), top, px, pad_y + title_h + box_h)
    for i, line in enumerate(lines[:shown]):
        canvas.create_text(x, top + i * px * SLIDE_LINE, text=line, anchor=top_anchor,
                           fill=style["text_color"], font=font, justify=align)
    if preview:
        pp, shown = preview
        preview_font = tkfont.Font(family=style["font"], size=-max(1, round(pp)))
        grey = _mix_color(style["text_color"], style["background_color"], PREVIEW_MIX)
        preview_top = pad_y + title_h + box_h - len(shown) * pp * SLIDE_LINE
        for i, line in enumerate(shown):
            canvas.create_text(x, preview_top + i * pp * SLIDE_LINE, text=line, anchor=top_anchor,
                               fill=grey, font=preview_font, justify=align)
    if body["title"]:
        title_font = tkfont.Font(family=style["font"], size=-round(0.05 * height), weight="bold")
        canvas.create_text(x, pad_y + title_h / 2, text=body["title"], anchor=mid_anchor,
                           fill=style["text_color"], font=title_font, width=box_w, justify=align)
    if body["copyright"]:
        copy_font = tkfont.Font(family=style["font"], size=-round(0.026 * height))
        canvas.create_text(x, height - pad_y - copy_h / 2, text=body["copyright"], anchor=mid_anchor,
                           fill=style["text_color"], font=copy_font, width=box_w, justify=align)


def _valid_monitors(body: dict) -> bool:
    """The same image on several monitors: 2 to 5 different ones, the first
    being "monitor"."""
    monitors = body["monitors"]
    # Images, and documents the agent shows itself (documents page by page).
    document = (body.get("kind") == "file" and documents is not None and isinstance(body.get("path"), str)
                and documents.document_type(body["path"]) is not None)
    return (
        (body.get("kind") == "image" or document)
        and isinstance(monitors, list)
        and 2 <= len(monitors) <= MAX_MONITORS
        and all(type(m) is int and 1 <= m <= MAX_MONITORS for m in monitors)
        and len(set(monitors)) == len(monitors)
        and monitors[0] == body.get("monitor")
    )


def _show_widget(root: tk.Tk, widget: tk.Widget) -> None:
    """The window shows either the image label or the song canvas."""
    for child in root.pack_slaves():
        if child is not widget:
            child.pack_forget()
    if not widget.winfo_ismapped():
        widget.pack(fill="both", expand=True)


def _song_window(monitor: int, fullscreen: bool) -> tuple[int, int, int, int]:
    """(width, height, x, y) of the song window on that monitor: the whole
    monitor, or a 1280 x 720 window in its middle. Raises RuntimeError for a
    monitor that is not there."""
    left, top, right, bottom = monitor_bounds(monitor)
    if fullscreen:
        return right - left, bottom - top, left, top
    width, height = min(1280, right - left), min(720, bottom - top)
    x, y = _window_position((left, top, right, bottom), width, height, "center")
    return width, height, x, y


# Fonts are measured at this size (the service scales: app/slide_layout.REF_PX).
METRICS_REF_PX = 100
MAX_METRICS_CHARS = 4000
METRICS_TIMEOUT_SECONDS = 5


def _valid_metrics_request(body) -> bool:
    return (
        isinstance(body, dict) and set(body) == {"font", "monitor", "fullscreen", "chars"}
        and isinstance(body["font"], str) and 0 < len(body["font"]) <= 60 and not any(ord(c) < 32 for c in body["font"])
        and type(body["monitor"]) is int and 1 <= body["monitor"] <= 16
        and isinstance(body["fullscreen"], bool)
        and isinstance(body["chars"], str) and 0 < len(body["chars"]) <= MAX_METRICS_CHARS
    )


def _measure_chars(font: str, chars: str) -> list[int]:
    """Width of each character in px at METRICS_REF_PX (needs the Tk thread)."""
    measuring = tkfont.Font(family=font, size=-METRICS_REF_PX)
    return [measuring.measure(c) for c in chars]


def _show_song(root: tk.Tk, canvas: tk.Canvas, body: dict) -> None:
    """A slide (or black) in the song window. While the window already
    shows a song there, the next slide is only drawn over the old one: the
    window is not hidden, moved, restyled or raised, so nothing behind it
    shows for a moment. Placing and styling it happen when the monitor,
    full screen or the size change, when it was hidden, or when Windows
    moved it away (a full-screen window is checked against its real place
    on every slide and made topmost again)."""
    global _song_place
    _close_mirrors()
    if body["action"] == "hide":
        root.withdraw()
        logger.info("Song window hidden")
        return
    width, height, x, y = _song_window(body["monitor"], body["fullscreen"])
    place = (body["monitor"], body["fullscreen"], width, height, x, y)
    if (place == _song_place and root.pack_slaves() == [canvas] and _keeps_style(root, body["fullscreen"])
            and (not body["fullscreen"] or _song_window_kept(root, x, y, width, height))):
        root.configure(background=body["style"]["background_color"])
        _draw_slide(canvas, width, height, body)
        if not body["fullscreen"]:
            root.lift()   # a framed window may have been covered (a full-screen one is topmost)
        logger.info("Song %s monitor=%s", body["action"], body["monitor"])
        return
    _show_widget(root, canvas)
    root.configure(background=body["style"]["background_color"])
    restyled = _set_style(root, body["fullscreen"])
    root.geometry(f"{width}x{height}+{x}+{y}")
    _draw_slide(canvas, width, height, body)
    root.deiconify()
    root.state("normal")
    root.update_idletasks()
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.lift()
    if body["fullscreen"]:
        _place_song_window(root, x, y, width, height, restyled)
    _song_place = place
    logger.info("Song %s monitor=%s fullscreen=%s size=%sx%s", body["action"], body["monitor"],
                body["fullscreen"], width, height)


def _song_window_kept(root, x: int, y: int, width: int, height: int) -> bool:
    """Windows: the full-screen song window is still exactly on its monitor
    (Windows moves it away when the beamer drops its signal for a moment);
    then it is made topmost again (another topmost window, a document or
    the taskbar, may be above it now) without moving, sizing, activating or
    redrawing it. False when it was moved: it is placed again."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    hwnd = root.winfo_id()
    hwnd = user32.GetParent(hwnd) or hwnd
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        return False
    if (rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top) != (x, y, width, height):
        logger.info("Song window was moved to %s,%s,%s,%s: placing it again", rect.left, rect.top, rect.right,
                    rect.bottom)
        return False
    # HWND_TOPMOST, SWP_NOSIZE | SWP_NOMOVE | SWP_NOACTIVATE
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0010)
    return True


def _place_song_window(root, x: int, y: int, width: int, height: int, restyled: bool) -> None:
    """Windows: the full-screen song window exactly on its monitor, topmost."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    user32.GetParent.argtypes = [wintypes.HWND]
    user32.GetParent.restype = wintypes.HWND
    user32.SetWindowPos.argtypes = [
        wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT,
    ]
    hwnd = root.winfo_id()
    hwnd = user32.GetParent(hwnd) or hwnd
    # HWND_TOPMOST, SWP_SHOWWINDOW, and SWP_FRAMECHANGED when the frame was just taken away
    if not user32.SetWindowPos(hwnd, -1, x, y, width, height, 0x0040 | (0x0020 if restyled else 0)):
        raise ctypes.WinError()


class DisplayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/display", "/audio", "/close", "/song", "/song/metrics", "/songs/library",
                             "/songs/save", "/power", "/info", "/update", "/restart", "/pick-file",
                             "/document"):
            self.send_error(404)
            return

        expected = os.environ.get("DESKTOP_AGENT_TOKEN", "")
        supplied = self.headers.get("X-Display-Token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            self.send_error(401)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            limit = {"/song": SONG_REQUEST_BYTES, "/song/metrics": 65536, "/songs/save": MAX_SONG_FILE_BYTES + 8192,
                     "/update": MAX_UPDATE_BYTES}.get(self.path, 8192)
            if not 0 < length <= limit:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        if self.path == "/info":
            # The service sends the audio sources of this PC's profiles:
            # their local copies are made in the background (see above).
            sources = body.get("audio") if isinstance(body, dict) else None
            if isinstance(sources, list):
                prefetch_sources(sources)
            info = {"ok": True, "version": agent_version(),
                    # POST /restart is there (the Update key restarts current
                    # agents); this process's start time shows a restart happened.
                    "restart": True, "started_at": STARTED_AT}
            try:
                # Remote diagnosis: which players this agent finds (None: not
                # found), and what of the user's environment it filled in at start.
                if documents is not None:
                    # What documents can be shown with (pypdfium2, LibreOffice).
                    info["documents"] = documents.capabilities()
                info.update(cached_tools(), restored_env=RESTORED_ENV, audio_start=mpv_start_report(),
                            **agent_account())
            except Exception:  # noqa: BLE001 - the version must always come back
                logger.exception("/info diagnosis failed")
            self._send_json(200, info)
            return
        if self.path == "/restart":
            self._handle_restart(body)
            return
        if self.path == "/update":
            self._handle_update(body)
            return
        if self.path == "/audio":
            self._handle_audio(body)
            return
        if self.path == "/close":
            self._handle_close(body)
            return
        if self.path == "/document":
            self._handle_document(body)
            return
        if self.path == "/songs/library":
            self._handle_song_library(body)
            return
        if self.path == "/songs/save":
            self._handle_song_save(body)
            return
        if self.path == "/song/metrics":
            self._handle_song_metrics(body)
            return
        if self.path == "/song":
            if not _valid_song_request(body):
                if _known_song_fields(body):
                    self._send_json(422, {"ok": False, "reason": "invalid song request"})
                else:
                    self.send_error(400)
                return
            requests.put({"kind": "song", "id": "song", **body})
            answer = {"ok": True, "action": body["action"], "started_at": STARTED_AT}
            if body["action"] != "hide":
                try:
                    # The window the slide is drawn in: the service lays out for this size.
                    width, height, _x, _y = _song_window(body["monitor"], body["fullscreen"])
                    answer["window"] = [body["monitor"], width, height]
                except Exception:  # noqa: BLE001 - the slide is shown (or fails) on the Tk thread
                    pass
            self._send_json(202, answer)
            return
        if self.path == "/power":
            self._handle_power(body)
            return
        if self.path == "/pick-file":
            self._handle_pick_file(body)
            return
        required = {
            "id", "title", "kind", "path", "args", "fullscreen", "monitor", "position"
        }
        if not isinstance(body, dict) or set(body) - {"monitors", "keep_if_open", "stamp", "player"} != required:
            self.send_error(400)
            return
        if ("stamp" in body and not _valid_stamp(body["stamp"])) or (
                "keep_if_open" in body and (body["keep_if_open"] is not True or body.get("kind") != "file")) or (
                "player" in body and (body["player"] != "mpv" or body.get("kind") != "file")):
            self.send_error(400)
            return
        if "monitors" in body and not _valid_monitors(body):
            self.send_error(400)
            return
        if (
            body["kind"] not in ("image", "program", "file")
            or not isinstance(body["id"], str)
            or not body["id"].replace("-", "").replace("_", "").isalnum()
            or not isinstance(body["path"], str)
            or not Path(body["path"]).is_absolute()
            or not isinstance(body["args"], list)
            or not all(isinstance(arg, str) and "\0" not in arg for arg in body["args"])
            or not isinstance(body["fullscreen"], bool)
            or not isinstance(body["monitor"], int)
            or body["monitor"] < 1
            or body["position"]
            not in ("top-left", "top-right", "bottom-left", "bottom-right", "center")
        ):
            self.send_error(400)
            return
        kept_document = body.get("keep_if_open") and _keep_document(body["id"], body["path"], body.get("stamp"))
        if kept_document or (body.get("keep_if_open") and _keep_open(body["id"], body.get("stamp"))):
            # The program's next row starts it again: its window stays as it is.
            logger.info("Profile %s kept open", body["id"])
            answer = {"ok": True, "profile": body["id"], "display": body["monitor"], "kept": True}
            if kept_document:
                answer["shown"] = True  # still on screen
            self._send_json(202, answer)
            return
        program = body["kind"] == "program"
        try:
            target = resolve_path(body["path"], suffixes=(".exe",) if program else ())
        except FileNotFoundError:
            target = None
        if target is None or not target.is_file() or (program and target.suffix.lower() != ".exe"):
            self.send_error(503, "Configured target is not available")
            return
        if runs_from_network_share(target):
            logger.warning("Refused to start %s: runnable file on a network share", target)
            self.send_error(403, "Programs on a network share are not started")
            return

        if body["kind"] == "file":
            with _opened_lock:
                if "stamp" in body:
                    _opened_stamps[body["id"]] = body["stamp"]
                else:
                    _opened_stamps.pop(body["id"], None)
                launch_token = uuid.uuid4().hex
                _launching[body["id"]] = (time.monotonic() + LAUNCH_PENDING_SECONDS, launch_token)
                _cancelled.discard(body["id"])
        # Profiles open the file the path resolved to ("*" filled in).
        reply = _document_reply(body, target)
        requests.put({**body, "path": str(target), "requested": body["path"],
                      **({"launch_token": launch_token} if body["kind"] == "file" else {}),
                      **({"reply": reply} if reply else {})})
        answer = {"ok": True, "profile": body["id"], "display": body["monitor"]}
        if reply:
            # A document: the answer comes once its first page is on screen
            # (the service freezes the beamer after it), at most
            # DOCUMENT_ACK_SECONDS; "shown": false when it is not (yet).
            reply.done.wait(DOCUMENT_ACK_SECONDS)
            answer["shown"] = reply.shown
        result = json.dumps(answer).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def _handle_document(self, body: dict) -> None:
        """Page through the document shown (see "documents page by page")."""
        if documents is None:
            self.send_error(404)
            return
        if not documents.valid_document_request(body):
            self.send_error(400)
            return
        try:
            state = _document_command(body)
        except Exception:  # noqa: BLE001 - a broken page must not end the request thread silently
            logger.exception("Document %s failed", body["action"])
            self._send_json(500, {"ok": False, "reason": "page could not be rendered"})
            return
        if state is None:
            self._send_json(409, {"ok": False, "reason": "no document shown"})
            return
        self._send_json(200, {"ok": True, **state})

    def _handle_close(self, body: dict) -> None:
        """{"id": profile}: close the windows it opened, hide its image if it
        is still shown (app/profile_stops.py). Answers at once."""
        if (
            not isinstance(body, dict)
            or set(body) - {"stamp"} != {"id"}
            or not isinstance(body["id"], str)
            or not body["id"].replace("-", "").replace("_", "").isalnum()
            or ("stamp" in body and not _valid_stamp(body["stamp"]))
        ):
            self.send_error(400)
            return
        profile_id = body["id"]
        with _opened_lock:
            opening = _launch_pending(profile_id) and profile_id not in _cancelled
            unknown = profile_id in _unknown_windows and profile_id not in _opened_windows
        closed = _close_profile_windows(profile_id, stamp=body.get("stamp"))
        # The image and the document are hidden on the tk thread; its answer
        # says whether one of them was this profile's (a Desktop key's stop:
        # "War nicht offen" otherwise).
        reply: queue.Queue = queue.Queue(maxsize=1)
        requests.put({"kind": "close", "id": profile_id, "stamp": body.get("stamp"), "reply": reply})
        try:
            hidden = reply.get(timeout=CLOSE_REPLY_SECONDS)
        except queue.Empty:
            hidden = None
        image = hidden["image"] if hidden else None
        document = hidden["document"] if hidden else None
        if closed or opening or image or document:
            was_open = True
        elif hidden is None or unknown:
            was_open = None  # cannot tell (a window of an app the agent did not start)
        else:
            was_open = False
        self._send_json(200, {"ok": True, "profile": profile_id, "windows_closed": closed, "opening": opening,
                              "image_hidden": image, "document_hidden": document, "was_open": was_open})

    def _handle_audio(self, body: dict) -> None:
        if (
            not isinstance(body, dict)
            or set(body) - {"fade", "only_if", "keep_if_playing", "stamp"} != {"id", "action", "path"}
            or ("fade" in body and not _valid_fade(body["fade"]))
            # "only_if": stop only this profile's track; "keep_if_playing":
            # leave it running if it plays; "stamp": which start (app/profile_stops.py).
            or ("only_if" in body and (body["only_if"] is not True or body["action"] != "stop"))
            or ("keep_if_playing" in body and (body["keep_if_playing"] is not True or body["action"] != "play"))
            or ("stamp" in body and not _valid_stamp(body["stamp"]))
            or not isinstance(body["id"], str)
            or not body["id"].replace("-", "").replace("_", "").isalnum()
            or body["action"] not in ("play", "stop")
            or (body["action"] == "play" and not isinstance(body["path"], str))
            or (body["action"] == "stop" and body["path"] is not None)
        ):
            self.send_error(400)
            return
        try:
            if body["action"] == "play":
                if "stamp" in body or "keep_if_playing" in body:
                    kept = _play_audio(body["id"], body["path"], body.get("fade"), stamp=body.get("stamp"),
                                       keep_if_playing=body.get("keep_if_playing", False))
                else:
                    kept = _play_audio(body["id"], body["path"], body.get("fade"))
                result = {"ok": True, "playing": True, "profile": body["id"], "kept": bool(kept)}
            else:
                if body.get("only_if"):
                    stopped = _stop_audio(body.get("fade"), only_profile=body["id"], stamp=body.get("stamp"))
                else:
                    stopped = _stop_audio(body.get("fade"))
                result = {"ok": True, "playing": False, "stopped": stopped}
        except (FileNotFoundError, ValueError) as exc:
            logger.warning("Audio %s rejected: %s", body["action"], exc)
            result = {"ok": False, "reason": str(exc)}
        except AudioStartError as exc:
            logger.warning("Audio %s failed for profile=%s: %s (mpv log: %s)",
                           body["action"], body["id"], exc, mpv_log_path())
            result = {"ok": False, "reason": str(exc)}
        except OSError:
            logger.exception("Audio %s failed for profile=%s", body["action"], body["id"])
            result = {"ok": False, "reason": "audio player could not be started"}
        status = 200 if result["ok"] else 503
        encoded = json.dumps(result).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _handle_song_metrics(self, body: dict) -> None:
        """The song window's size on a monitor and the font's character
        widths, so the service can lay songs out for this screen. Measured on
        the Tk thread (a short job between two slides)."""
        if not _valid_metrics_request(body):
            self.send_error(400)
            return
        try:
            width, height, _x, _y = _song_window(body["monitor"], body["fullscreen"])
        except RuntimeError as exc:
            self._send_json(409, {"ok": False, "reason": str(exc)})
            return
        reply: queue.Queue = queue.Queue(maxsize=1)
        requests.put({"kind": "measure", "id": "measure", "font": body["font"], "chars": body["chars"], "reply": reply})
        try:
            widths = reply.get(timeout=METRICS_TIMEOUT_SECONDS)
        except queue.Empty:
            widths = None
        if widths is None:
            self._send_json(503, {"ok": False, "reason": "the font could not be measured"})
            return
        self._send_json(200, {"ok": True, "width": width, "height": height, "ref": METRICS_REF_PX, "widths": widths,
                              "started_at": STARTED_AT})

    def _handle_song_library(self, body: dict) -> None:
        if not isinstance(body, dict) or set(body) != {"path"} or not isinstance(body["path"], str):
            self.send_error(400)
            return
        try:
            files, truncated = _read_song_library(body["path"])
        except FileNotFoundError:
            logger.warning("Song library folder not found: %s", body["path"])
            self.send_error(404, "Song library folder not found")
            return
        except OSError:
            logger.exception("Song library could not be read: %s", body["path"])
            self.send_error(503, "Song library could not be read")
            return
        logger.info("Song library read: %s files from %s", len(files), body["path"])
        self._send_json(200, {"ok": True, "files": files, "truncated": truncated})

    def _handle_song_save(self, body: dict) -> None:
        if (not isinstance(body, dict) or set(body) != {"path", "name", "xml"}
                or not all(isinstance(body[k], str) for k in body)):
            self.send_error(400)
            return
        try:
            target = _save_song(body["path"], body["name"], body["xml"])
        except FileNotFoundError:
            self.send_error(404, "Song library folder not found")
            return
        except FileExistsError:
            self.send_error(409, "A song with this name exists already")
            return
        except ValueError:
            self.send_error(400)
            return
        except OSError:
            logger.exception("Song could not be saved: %s", body["name"])
            self.send_error(503, "Song could not be saved")
            return
        logger.info("Song saved: %s", target)
        self._send_json(200, {"ok": True, "name": body["name"]})

    def _send_json(self, status: int, result: dict) -> None:
        encoded = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _handle_update(self, body: dict) -> None:
        if not _valid_update_request(body):
            self.send_error(400)
            return
        try:
            _install_update(body)
        except (ValueError, SyntaxError, OSError):
            logger.exception("Agent update could not be installed")
            self._send_json(500, {"ok": False, "reason": "agent update could not be installed"})
            return
        self._send_json(202, {"ok": True, "version": body["version"], "restarting": True})
        timer = threading.Timer(0.5, _restart_agent)
        timer.daemon = True
        timer.start()

    def _handle_restart(self, body: dict) -> None:
        """Start the agent again with the same files: the same way as after a
        self-update (restart-agent.ps1 with the user's environment)."""
        if body != {}:
            self.send_error(400)
            return
        logger.info("Agent restart requested")
        self._send_json(202, {"ok": True, "version": agent_version(), "restarting": True})
        timer = threading.Timer(0.5, _restart_agent)
        timer.daemon = True
        timer.start()

    def _handle_power(self, body: dict) -> None:
        if not isinstance(body, dict) or body != {"action": "shutdown"}:
            self.send_error(400)
            return
        result = json.dumps({"ok": True, "shutdown_scheduled": True, "delay_seconds": 10}).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)
        _schedule_shutdown()

    def _handle_pick_file(self, body: dict) -> None:
        if not _valid_pick_request(body):
            self.send_error(400)
            return
        if os.name != "nt":
            self._send_json(501, {"ok": False, "reason": "the file dialog needs Windows"})
            return
        # One dialog at a time: a second click must not stack dialogs.
        if not _pick_lock.acquire(blocking=False):
            self._send_json(409, {"ok": False, "reason": "a file dialog is already open on this PC"})
            return
        try:
            path = _pick_file(body["start"])
        except TimeoutError:
            self._send_json(408, {"ok": False, "reason": "no file was picked in time"})
            return
        except OSError as exc:
            logger.warning("File dialog failed: %s", exc)
            self._send_json(503, {"ok": False, "reason": "the file dialog could not be opened"})
            return
        finally:
            _pick_lock.release()
        logger.info("File picked: %s", path if path else "(cancelled)")
        self._send_json(200, {"ok": True, "path": path})

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.client_address[0], fmt % args)


# -- Windows "open file" dialog -------------------------------------------------
# The web page cannot learn a file's path from the browser's own picker, and
# the service (a Windows service) cannot show a window. The agent runs in the
# signed-in session, so it opens the real dialog on this PC's screen. It runs
# in its own PowerShell process: the Tk window (songs, images) stays live
# while someone picks a file. The start folder goes in through an environment
# variable, never into the command text.
PICK_TIMEOUT_SECONDS = 600
_PICK_SCRIPT = r"""
Add-Type -AssemblyName System.Windows.Forms
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$owner = New-Object System.Windows.Forms.Form
$owner.TopMost = $true
$owner.ShowInTaskbar = $false
$owner.FormBorderStyle = 'None'
$owner.Opacity = 0
$owner.StartPosition = 'CenterScreen'
$owner.Show()
$owner.Activate()
$dialog = New-Object System.Windows.Forms.OpenFileDialog
$dialog.Title = 'Datei für den Programmpunkt'
$dialog.Filter = 'Alle Dateien (*.*)|*.*|Musik|*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.opus;*.wma|Programme (*.exe)|*.exe'
$dialog.CheckFileExists = $true
$dialog.RestoreDirectory = $true
$start = $env:MIXER_PICK_START
if ($start -and (Test-Path -LiteralPath $start -PathType Container)) { $dialog.InitialDirectory = $start }
$result = $dialog.ShowDialog($owner)
$owner.Close()
if ($result -eq [System.Windows.Forms.DialogResult]::OK) { [Console]::Out.Write($dialog.FileName) }
"""
_pick_lock = threading.Lock()


def _valid_pick_request(body) -> bool:
    return (
        isinstance(body, dict) and set(body) == {"start"} and isinstance(body["start"], str)
        and len(body["start"]) <= 400 and not any(ord(c) < 32 for c in body["start"])
    )


def _pick_file(start: str) -> str | None:
    """Show the dialog; the chosen path, or None when cancelled. Raises
    OSError when the dialog cannot be shown, TimeoutError after
    PICK_TIMEOUT_SECONDS (the dialog is closed then)."""
    import base64

    encoded = base64.b64encode(_PICK_SCRIPT.encode("utf-16-le")).decode("ascii")
    command = ["powershell.exe", "-NoProfile", "-NonInteractive", "-STA",
               "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded]
    environment = {**os.environ, "MIXER_PICK_START": start}
    try:
        finished = subprocess.run(
            command, env=environment, stdin=subprocess.DEVNULL, capture_output=True,
            timeout=PICK_TIMEOUT_SECONDS, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError("nobody picked a file in time") from exc
    if finished.returncode != 0:
        raise OSError(f"file dialog failed: {finished.stderr.decode('utf-8', 'replace').strip()[:200]}")
    path = finished.stdout.decode("utf-8-sig", "replace").strip()
    return path or None


def drain_requests(root: tk.Tk, image_label: tk.Label, canvas: tk.Canvas) -> None:
    try:
        while True:
            profile = requests.get_nowait()
            try:
                handle_request(root, image_label, canvas, profile)
            except Exception as exc:
                logger.exception("Profile %s failed: %s", profile["id"], exc)
    except queue.Empty:
        pass
    root.after(100, drain_requests, root, image_label, canvas)


def handle_request(root, image_label, canvas, profile: dict) -> None:
    """One request on the tk thread: a song slide, an image, a close (POST
    /close) or a program / file started in the background."""
    global _shown_image, _shown_image_stamp
    if profile["kind"] == "song":
        _shown_image = None  # the window shows the song now
        _show_song(root, canvas, profile)
    elif profile["kind"] == "measure":
        try:
            profile["reply"].put_nowait(_measure_chars(profile["font"], profile["chars"]))
        except Exception:  # noqa: BLE001 - the request gets its 503
            profile["reply"].put_nowait(None)
            raise
    elif profile["kind"] == "image":
        _show_image(root, image_label, profile)
        _shown_image, _shown_image_stamp = profile["id"], profile.get("stamp")
    elif profile["kind"] == "close":
        # Only the image of that profile, and only the start the close names:
        # a newer image, the same one shown again, or a song stays.
        stamp = profile.get("stamp")
        image = _shown_image == profile["id"] and (stamp is None or _shown_image_stamp in (None, stamp))
        if image:
            _hide_all(root)
            logger.info("Image hidden profile=%s", profile["id"])
        document = _hide_document(profile["id"], stamp)
        if profile.get("reply") is not None:
            profile["reply"].put_nowait({"image": image, "document": document})
    elif profile["kind"] == "document":
        shown = False
        try:
            shown = _show_document(root, profile)
        finally:
            _finish_reply(profile, shown)
    elif profile["kind"] == "document-page":
        _show_document_page(profile)
    else:
        threading.Thread(target=_launch_profile, args=(profile,), daemon=True).start()


def create_root() -> tuple[tk.Tk, tk.Label, tk.Canvas]:
    root = tk.Tk()
    root.withdraw()
    root.configure(background="black")
    root.bind("<Escape>", lambda _event: _hide_all(root))
    # A framed (not full-screen) window has a close button; destroying the
    # main window would end mainloop and with it the whole agent.
    root.protocol("WM_DELETE_WINDOW", lambda: _hide_all(root))
    label = tk.Label(root, background="black", borderwidth=0)
    label.pack(fill="both", expand=True)
    canvas = tk.Canvas(root, background="black", highlightthickness=0, borderwidth=0)
    return root, label, canvas


def main() -> None:
    log_directory = agent_log_directory()
    log_directory.mkdir(parents=True, exist_ok=True)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            RotatingFileHandler(
                log_directory / "agent.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8"
            )
        ],
    )
    # Started through WMI after a self-update the process lacks the user's
    # environment (PATH with mpv, user variables): take it from the registry.
    global RESTORED_ENV
    try:
        RESTORED_ENV = restore_user_environment()
    except Exception:  # noqa: BLE001 - the agent must start in any case
        logger.exception("Could not read the user's environment")
    cached_tools()  # first search in the background, for /info
    if documents is not None and os.name == "nt":
        # The Update key runs no pip: a new requirement (pypdfium2) comes this way.
        threading.Thread(target=documents.install_missing_requirements, daemon=True, args=(
            agent_version(), agent_log_directory() / "pip-install.json")).start()
    # Local copies of the audio sources known from before (see above).
    threading.Thread(target=refresh_audio_cache, daemon=True).start()
    if RESTORED_ENV:
        logger.info("Filled in from the user's environment: %s", ", ".join(RESTORED_ENV))
    if not os.environ.get("DESKTOP_AGENT_TOKEN"):
        logger.error("Set DESKTOP_AGENT_TOKEN before starting the agent")
        return

    root, label, canvas = create_root()
    root.after(100, drain_requests, root, label, canvas)

    # After a self-update the old agent may still hold the port for a moment.
    for attempt in range(40):
        try:
            server = ThreadingHTTPServer((HOST, PORT), DisplayHandler)
            break
        except OSError:
            if attempt == 39:
                raise
            time.sleep(0.5)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Display agent %s listening on %s:%s", agent_version(), HOST, PORT)
    root.mainloop()


if __name__ == "__main__":
    main()
