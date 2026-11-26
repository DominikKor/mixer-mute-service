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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from logging.handlers import RotatingFileHandler
from pathlib import Path

import glob
import re
import tkinter as tk
import tkinter.font as tkfont
from PIL import Image, ImageOps, ImageTk


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


def _monitor_areas(display_number: int) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
    """The monitor's full bounds and its work area (without the taskbar)."""
    if os.name != "nt":
        raise RuntimeError("This display agent is intended for Windows")

    import ctypes
    from ctypes import wintypes

    class MONITORINFO(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                    ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

    user32 = ctypes.windll.user32
    user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFO)]
    monitors: list[tuple[tuple[int, int, int, int], tuple[int, int, int, int]]] = []

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
        info = MONITORINFO(cbSize=ctypes.sizeof(MONITORINFO))
        if user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            work = info.rcWork
            monitors.append((full, (work.left, work.top, work.right, work.bottom)))
        else:
            monitors.append((full, full))
        return 1

    if not user32.EnumDisplayMonitors(None, None, callback, 0):
        raise OSError("Could not enumerate displays")
    monitors.sort(key=lambda areas: (areas[0][0], areas[0][1]))
    if display_number < 1 or display_number > len(monitors):
        raise RuntimeError(f"Display {display_number} not found; detected {len(monitors)}")
    return monitors[display_number - 1]


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
    process_id: int, before: set, monitor: int, fullscreen: bool, position: str
) -> None:
    """Move the started app's window to its place, and keep it there while
    the app settles: many apps centre or restore their window right after
    showing it, or replace a splash screen by the main window."""
    bounds = monitor_bounds(monitor)
    deadline = time.monotonic() + 20
    settle_until = None
    placed: dict[int, tuple[int, int, int, int]] = {}
    corrections = 0
    while True:
        hwnd = _find_window(process_id, before)
        if hwnd and hwnd not in placed:
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
    try:
        before = set(_visible_windows())
        path = Path(profile["path"])
        if profile["kind"] == "program":
            process_id = subprocess.Popen(
                [str(path), *profile["args"]], cwd=str(path.parent), shell=False
            ).pid
        else:
            process_id = _shell_open_file(path)
        _place_process_window(
            process_id, before, profile["monitor"], profile["fullscreen"], profile["position"]
        )
    except Exception:
        logger.exception("Profile %s failed", profile["id"])


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
_tool_cache = {"at": 0.0, "mpv": None, "ytdlp": None, "mpv_search": []}
_tool_cache_lock = threading.Lock()
_tool_refreshing = threading.Event()


def refresh_tool_cache() -> None:
    trace: list = []
    try:
        mpv = _find_mpv(trace)
        ytdlp = _find_ytdlp(mpv)
    except Exception:  # noqa: BLE001 - a diagnosis must never break the agent
        logger.exception("Searching mpv / yt-dlp failed")
        mpv = ytdlp = None
    with _tool_cache_lock:
        _tool_cache.update(at=time.monotonic(), mpv=mpv, ytdlp=ytdlp, mpv_search=trace)
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
    fallback_names = _FALLBACK_NAMES.get(command, set())
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
        if found and Path(found).name.lower() in fallback_names:
            entry["note"] = "not mpv itself: used only if nothing else is found"
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
    mpv = _find_mpv()
    ytdlp = _find_ytdlp(mpv)
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


def _play_audio(profile_id: str, path: str, fade: dict | None = None) -> None:
    global _playback
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
    mpv = _find_mpv()
    if not mpv:
        _, searched = find_tool("mpv", "MPV_PATH", "mpv", "mpv")
        raise FileNotFoundError(missing_tool_message("mpv", "MPV_PATH", searched, "winget install mpv"))
    ytdlp = _find_ytdlp(mpv) if stream else None
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
        _playback = playback
        if fade:
            threading.Thread(target=_connect_ipc, args=(playback, address), daemon=True).start()
    logger.info("Audio started profile=%s path=%s local=%s pid=%s fade=%s",
                profile_id, target, local, process.pid, fade)


def _stop_audio(fade: dict | None = None) -> bool:
    """Stop playback; with a fade (and a track started with one) it fades
    out in the background and the call returns at once."""
    global _playback
    with _audio_lock:
        playback = _playback
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
    _close_mirrors()
    _show_widget(root, image_label)
    _show_image_on(root, image_label, profile, monitors[0])
    for monitor in monitors[1:]:
        window = tk.Toplevel(root, background="black")
        window.withdraw()
        window.protocol("WM_DELETE_WINDOW", lambda: _hide_all(root))
        window.bind("<Escape>", lambda _event: _hide_all(root))
        label = tk.Label(window, background="black", borderwidth=0)
        label.pack(fill="both", expand=True)
        _mirrors.append(window)
        _show_image_on(window, label, profile, monitor)


# Extra windows showing the current image on further monitors.
_mirrors: list[tk.Toplevel] = []


def _close_mirrors() -> None:
    while _mirrors:
        _mirrors.pop().destroy()


def _hide_all(root: tk.Tk) -> None:
    """Escape or the window's close button: hide, never end the agent."""
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
    root.withdraw()
    root.overrideredirect(profile["fullscreen"])
    root.attributes("-topmost", profile["fullscreen"])
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
    flags = 0x0040 | (0x0020 if profile["fullscreen"] else 0)
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


def _valid_song_request(body) -> bool:
    if not isinstance(body, dict) or body.get("action") not in ("show", "blank", "hide"):
        return False
    if body["action"] == "hide":
        return set(body) == {"action"}
    style = body.get("style")
    base = {"action", "monitor", "fullscreen", "style", "text", "title", "copyright"}
    style_base = {"font", "font_size", "text_color", "background_color"}
    # "next" (preview of the next slide) and style "align" are newer and
    # optional, so an older service still works with this agent.
    return (
        base <= set(body) <= base | {"next"}
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
# largest size): smaller is unreadable from the back. Text that does not fit
# even then is wrapped, not shrunk further.
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


def _fit_text(measure, text: str, max_px: float, min_px: float, box_w: float, box_h: float):
    """(size, lines) of the lyrics: wrapped at the largest size where the text
    fits; the file's own lines instead if they fit at OWN_LINES_KEEP of that
    size or more (and not below min_px); at min_px wrapped regardless.
    Same as fitSlideText() in app/static/js/songs.js."""
    paragraphs = text.split("\n")
    min_px = min(min_px, max_px)
    own = next((px for px in _slide_sizes(max_px, min_px)
                if max(measure(line, px) for line in paragraphs) <= box_w
                and len(paragraphs) * px * SLIDE_LINE <= box_h), None)
    wrapped = None
    for px in _slide_sizes(max_px, min_px):
        lines = _wrap_lines(measure, paragraphs, px, box_w)
        if len(lines) * px * SLIDE_LINE <= box_h:
            wrapped = (px, lines)
            break
    if own is not None and (wrapped is None or own >= OWN_LINES_KEEP * wrapped[0]):
        return own, paragraphs
    if wrapped is not None:
        return wrapped
    return min_px, _wrap_lines(measure, paragraphs, min_px, box_w)


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
    style = body["style"]
    canvas.delete("all")
    canvas.configure(background=style["background_color"])
    if body["action"] == "blank":
        return
    align = style.get("align", "center")
    pad_x, pad_y = SLIDE_PAD_X * width, SLIDE_PAD_Y * height
    title_h = SLIDE_TITLE_H * height if body["title"] else 0
    copy_h = SLIDE_COPY_H * height if body["copyright"] else 0
    box_w = width - 2 * pad_x
    box_h = height - 2 * pad_y - title_h - copy_h
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

    px, lines = _fit_text(measure, body["text"], style["font_size"] * height / 1080,
                          SLIDE_MIN_PX * height / 1080, box_w, box_h)
    font = tkfont.Font(family=style["font"], size=-max(1, round(px)))
    # Centred in the box; text taller than the box starts at its top.
    top = pad_y + title_h + max(0.0, (box_h - len(lines) * px * SLIDE_LINE) / 2)
    # Text taller than its box is cut at the box's bottom edge.
    shown = _visible_lines(len(lines), top, px, pad_y + title_h + box_h)
    for i, line in enumerate(lines[:shown]):
        canvas.create_text(x, top + i * px * SLIDE_LINE, text=line, anchor=top_anchor,
                           fill=style["text_color"], font=font, justify=align)
    next_text = body.get("next", "")
    preview = _preview_layout(measure, lines, next_text.split("\n") if next_text else [], px, box_w, box_h, height)
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
    return (
        body.get("kind") == "image"
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


def _show_song(root: tk.Tk, canvas: tk.Canvas, body: dict) -> None:
    _close_mirrors()
    if body["action"] == "hide":
        root.withdraw()
        logger.info("Song window hidden")
        return
    left, top, right, bottom = monitor_bounds(body["monitor"])
    if body["fullscreen"]:
        width, height, x, y = right - left, bottom - top, left, top
    else:
        width, height = min(1280, right - left), min(720, bottom - top)
        x, y = _window_position((left, top, right, bottom), width, height, "center")
    _show_widget(root, canvas)
    root.configure(background=body["style"]["background_color"])
    root.withdraw()
    root.overrideredirect(body["fullscreen"])
    root.attributes("-topmost", body["fullscreen"])
    root.geometry(f"{width}x{height}+{x}+{y}")
    _draw_slide(canvas, width, height, body)
    root.deiconify()
    root.state("normal")
    root.update_idletasks()
    root.geometry(f"{width}x{height}+{x}+{y}")
    root.lift()
    if body["fullscreen"]:
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
        # HWND_TOPMOST, SWP_SHOWWINDOW | SWP_FRAMECHANGED
        if not user32.SetWindowPos(hwnd, -1, x, y, width, height, 0x0040 | 0x0020):
            raise ctypes.WinError()
    logger.info("Song %s monitor=%s fullscreen=%s size=%sx%s", body["action"], body["monitor"],
                body["fullscreen"], width, height)


class DisplayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path not in ("/display", "/audio", "/song", "/songs/library", "/songs/save", "/power",
                             "/info", "/update", "/restart", "/pick-file"):
            self.send_error(404)
            return

        expected = os.environ.get("DESKTOP_AGENT_TOKEN", "")
        supplied = self.headers.get("X-Display-Token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            self.send_error(401)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            limit = {"/song": 32768, "/songs/save": MAX_SONG_FILE_BYTES + 8192,
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
        if self.path == "/songs/library":
            self._handle_song_library(body)
            return
        if self.path == "/songs/save":
            self._handle_song_save(body)
            return
        if self.path == "/song":
            if not _valid_song_request(body):
                self.send_error(400)
                return
            requests.put({"kind": "song", "id": "song", **body})
            self._send_json(202, {"ok": True, "action": body["action"]})
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
        if not isinstance(body, dict) or set(body) - {"monitors"} != required:
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

        # Profiles open the file the path resolved to ("*" filled in).
        requests.put({**body, "path": str(target)})
        result = json.dumps({"ok": True, "profile": body["id"], "display": body["monitor"]}).encode()
        self.send_response(202)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(result)))
        self.end_headers()
        self.wfile.write(result)

    def _handle_audio(self, body: dict) -> None:
        if (
            not isinstance(body, dict)
            or set(body) - {"fade"} != {"id", "action", "path"}
            or ("fade" in body and not _valid_fade(body["fade"]))
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
                _play_audio(body["id"], body["path"], body.get("fade"))
                result = {"ok": True, "playing": True, "profile": body["id"]}
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
                if profile["kind"] == "song":
                    _show_song(root, canvas, profile)
                elif profile["kind"] == "image":
                    _show_image(root, image_label, profile)
                else:
                    threading.Thread(target=_launch_profile, args=(profile,), daemon=True).start()
            except Exception as exc:
                logger.exception("Profile %s failed: %s", profile["id"], exc)
    except queue.Empty:
        pass
    root.after(100, drain_requests, root, image_label, canvas)


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
