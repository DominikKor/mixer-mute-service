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
logger = logging.getLogger("display-agent")


def monitor_bounds(display_number: int) -> tuple[int, int, int, int]:
    if os.name != "nt":
        raise RuntimeError("This display agent is intended for Windows")

    import ctypes
    from ctypes import wintypes

    monitors: list[tuple[int, int, int, int]] = []

    @ctypes.WINFUNCTYPE(
        ctypes.c_int,
        wintypes.HMONITOR,
        wintypes.HDC,
        ctypes.POINTER(wintypes.RECT),
        wintypes.LPARAM,
    )
    def callback(_monitor, _dc, rect, _data):
        bounds = rect.contents
        monitors.append((bounds.left, bounds.top, bounds.right, bounds.bottom))
        return 1

    if not ctypes.windll.user32.EnumDisplayMonitors(None, None, callback, 0):
        raise OSError("Could not enumerate displays")
    monitors.sort(key=lambda rect: (rect[0], rect[1]))
    if display_number < 1 or display_number > len(monitors):
        raise RuntimeError(f"Display {display_number} not found; detected {len(monitors)}")
    return monitors[display_number - 1]


def _find_window(process_id: int):
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def callback(hwnd, _data):
        owner = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value == process_id and user32.IsWindowVisible(hwnd):
            found.append(hwnd)
            return False
        return True

    user32.EnumWindows(callback, 0)
    foreground = user32.GetForegroundWindow()
    if foreground in found:
        return foreground
    for hwnd in found:
        if not user32.IsIconic(hwnd):
            return hwnd
    return found[0] if found else None


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
        hwnd = ctypes.windll.user32.GetForegroundWindow()
        owner = wintypes.DWORD()
        ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        return owner.value
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


def _place_process_window(
    process_id: int, monitor: int, fullscreen: bool, position: str
) -> None:
    if not process_id:
        logger.warning("No process handle was returned; window placement skipped")
        return
    deadline = time.monotonic() + 20
    hwnd = None
    while time.monotonic() < deadline:
        hwnd = _find_window(process_id)
        if hwnd:
            break
        time.sleep(0.2)
    if not hwnd:
        raise RuntimeError("Application window was not found within 20 seconds")

    import ctypes
    from ctypes import wintypes

    left, top, right, bottom = monitor_bounds(monitor)
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9)
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    width = right - left if fullscreen else rect.right - rect.left
    height = bottom - top if fullscreen else rect.bottom - rect.top
    x, y = _window_position((left, top, right, bottom), width, height, position)
    if fullscreen:
        style = user32.GetWindowLongW(hwnd, -16)
        user32.SetWindowLongW(hwnd, -16, (style & ~0x00CF0000) | 0x80000000)
        insert_after = -1
        flags = 0x0040 | 0x0020
    else:
        insert_after = -2
        flags = 0x0040
    if not user32.SetWindowPos(hwnd, insert_after, x, y, width, height, flags):
        raise ctypes.WinError()
    logger.info(
        "Positioned window pid=%s hwnd=%s monitor=%s fullscreen=%s position=%s rect=%sx%s+%s+%s",
        process_id,
        hwnd,
        monitor,
        fullscreen,
        position,
        width,
        height,
        x,
        y,
    )


def _launch_profile(profile: dict) -> None:
    try:
        path = Path(profile["path"])
        if profile["kind"] == "program":
            process_id = subprocess.Popen(
                [str(path), *profile["args"]], cwd=str(path.parent), shell=False
            ).pid
        else:
            process_id = _shell_open_file(path)
        _place_process_window(
            process_id, profile["monitor"], profile["fullscreen"], profile["position"]
        )
    except Exception:
        logger.exception("Profile %s failed", profile["id"])


def _find_mpv() -> str | None:
    configured = os.environ.get("MPV_PATH", "").strip()
    if configured:
        if Path(configured).is_file():
            return configured
        resolved = shutil.which(configured)
        if resolved:
            return resolved
    found = shutil.which("mpv")
    if found:
        return found
    candidates = [
        Path(__file__).with_name("mpv.exe"),
        Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "mpv" / "mpv.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "mpv" / "mpv.exe",
    ]
    return next((str(path) for path in candidates if path.is_file()), None)


# -- audio (mpv) -------------------------------------------------------------
# With a fade, mpv starts silent and its volume is driven over mpv's JSON IPC
# (a named pipe on Windows): up to the profile's dB level on play, down to
# silence on stop, then mpv is ended. mpv's volume is cubic: gain = (v/100)^3,
# so dB = 60 * log10(v / 100).
FADE_FLOOR_DB = -60.0
FADE_STEP_SECONDS = 0.05
IPC_CONNECT_SECONDS = 2.0


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
            except OSError:
                if process.poll() is not None or time.monotonic() > deadline:
                    raise OSError("mpv IPC not reachable")
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
    def __init__(self, profile_id: str, process: subprocess.Popen, ipc: MpvIpc | None, fade: dict | None):
        self.profile_id = profile_id
        self.process = process
        self.ipc = ipc
        self.fade = fade
        self.volume_db = FADE_FLOOR_DB if fade else 0.0
        self.stop_fade = threading.Event()   # ends a running fade early
        self.fade_thread: threading.Thread | None = None

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


def _fade_in(playback: _Playback) -> None:
    if playback.ramp(playback.fade["db"], playback.fade["seconds"]):
        logger.info("Audio faded in profile=%s to %.1f dB", playback.profile_id, playback.fade["db"])


def _fade_out(playback: _Playback, seconds: float) -> None:
    if playback.fade_thread is not None:
        playback.stop_fade.set()
        playback.fade_thread.join()
        playback.stop_fade.clear()
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


def _play_audio(profile_id: str, path: str, fade: dict | None = None) -> None:
    global _playback
    target = Path(path)
    if not target.is_absolute() or not target.is_file():
        raise ValueError("Configured audio file is not available")
    mpv = _find_mpv()
    if not mpv:
        raise FileNotFoundError("mpv was not found; install mpv or set MPV_PATH")

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
    ]
    address = _ipc_address() if fade else None
    if fade:
        command += ["--volume=0", f"--input-ipc-server={address}"]
    with _audio_lock:
        _stop_audio_locked()
        process = subprocess.Popen(
            command + ["--", str(target)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            startupinfo=startup_info,
        )
        playback = _Playback(profile_id, process, MpvIpc(address) if fade else None, fade)
        if fade:
            try:
                playback.ipc.connect(process, IPC_CONNECT_SECONDS)
            except OSError:
                # Silent and not controllable: better not to play at all.
                playback.terminate()
                raise
            playback.fade_thread = threading.Thread(target=_fade_in, args=(playback,), daemon=True)
            playback.fade_thread.start()
        _playback = playback
    logger.info("Audio started profile=%s path=%s pid=%s fade=%s", profile_id, target, process.pid, fade)


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


def _restart_agent() -> None:
    """Start the agent again (now with the new files) and end this process."""
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
    left, top, right, bottom = monitor_bounds(profile["monitor"])
    bounds = (right - left, bottom - top)
    with Image.open(profile["path"]) as source:
        if profile["fullscreen"]:
            image = ImageOps.fit(source.convert("RGB"), bounds)
        else:
            image = source.convert("RGB")
            image.thumbnail(bounds, Image.Resampling.LANCZOS)
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
    window_width = right - left if profile["fullscreen"] else image.width + frame_width
    window_height = bottom - top if profile["fullscreen"] else image.height + frame_height
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
        profile["monitor"],
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
    folder itself). Raises FileNotFoundError when the folder is missing."""
    folder = _songs_folder(path)
    files, truncated = [], False
    for entry in sorted(folder.iterdir(), key=lambda p: p.name.casefold()):
        if entry.suffix.lower() != ".foil" or not entry.is_file():
            continue
        if len(files) >= MAX_SONG_FILES:
            truncated = True
            break
        if entry.stat().st_size > MAX_SONG_FILE_BYTES:
            continue
        files.append({"name": entry.name, "xml": entry.read_bytes().decode("utf-8", errors="replace")})
    return files, truncated


def _songs_folder(path: str) -> Path:
    folder = Path(path)
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
# Preview of the next slide: grey (text colour mixed into the background),
# at most half the slide's text size and never below 20 px on 1080 lines; only
# in the room left under the centred text, which never moves for it.
PREVIEW_SCALE, PREVIEW_MIN, PREVIEW_GAP, PREVIEW_MIX = 0.5, 20, 0.5, 0.45


def _mix_color(fg: str, bg: str, amount: float) -> str:
    """fg mixed into bg: amount 0 = bg, 1 = fg (both #rrggbb)."""
    a, b = int(fg[1:], 16), int(bg[1:], 16)
    parts = [round(((b >> s) & 255) + (((a >> s) & 255) - ((b >> s) & 255)) * amount) for s in (16, 8, 0)]
    return "#" + "".join(f"{p:02x}" for p in parts)


def _preview_layout(measure, lines: list[str], next_lines: list[str], px: float, box_w: float,
                    box_h: float, height: int):
    """(size, lines) of the next-slide preview, or None if it does not fit.
    measure(text, size) gives the width of text at that pixel size."""
    if not next_lines:
        return None
    min_px = PREVIEW_MIN * height / 1080
    pp = PREVIEW_SCALE * px
    while pp >= min_px and max(measure(line, pp) for line in next_lines) > box_w:
        pp *= 0.94
    if pp < min_px:
        return None
    free = (box_h - len(lines) * px * SLIDE_LINE) / 2 - PREVIEW_GAP * px
    count = min(len(next_lines), int(free // (pp * SLIDE_LINE)) if free > 0 else 0)
    return (pp, next_lines[:count]) if count >= 1 else None


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
    lines = body["text"].split("\n")
    px = style["font_size"] * height / 1080
    while px > 8:
        font = tkfont.Font(family=style["font"], size=-round(px))
        if max(font.measure(line) for line in lines) <= box_w and len(lines) * px * SLIDE_LINE <= box_h:
            break
        px *= 0.94
    font = tkfont.Font(family=style["font"], size=-max(8, round(px)))
    top = pad_y + title_h + (box_h - len(lines) * px * SLIDE_LINE) / 2
    for i, line in enumerate(lines):
        canvas.create_text(x, top + i * px * SLIDE_LINE, text=line, anchor=top_anchor,
                           fill=style["text_color"], font=font, justify=align)
    next_text = body.get("next", "")
    preview = _preview_layout(
        lambda text, size: tkfont.Font(family=style["font"], size=-max(1, round(size))).measure(text),
        lines, next_text.split("\n") if next_text else [], px, box_w, box_h, height)
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


def _show_widget(root: tk.Tk, widget: tk.Widget) -> None:
    """The window shows either the image label or the song canvas."""
    for child in root.pack_slaves():
        if child is not widget:
            child.pack_forget()
    if not widget.winfo_ismapped():
        widget.pack(fill="both", expand=True)


def _show_song(root: tk.Tk, canvas: tk.Canvas, body: dict) -> None:
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
                             "/info", "/update", "/pick-file"):
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
            self._send_json(200, {"ok": True, "version": agent_version()})
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
        if not isinstance(body, dict) or set(body) != required:
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
        target = Path(body["path"])
        if not target.is_file() or (body["kind"] == "program" and target.suffix.lower() != ".exe"):
            self.send_error(503, "Configured target is not available")
            return

        requests.put(body)
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
                    _show_widget(root, image_label)
                    _show_image(root, image_label, profile)
                else:
                    threading.Thread(target=_launch_profile, args=(profile,), daemon=True).start()
            except Exception as exc:
                logger.exception("Profile %s failed: %s", profile["id"], exc)
    except queue.Empty:
        pass
    root.after(100, drain_requests, root, image_label, canvas)


def main() -> None:
    log_directory = Path(os.environ.get("LOCALAPPDATA", Path.home())) / "MixerMuteDisplayAgent"
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
    if not os.environ.get("DESKTOP_AGENT_TOKEN"):
        logger.error("Set DESKTOP_AGENT_TOKEN before starting the agent")
        return

    root = tk.Tk()
    root.withdraw()
    root.configure(background="black")
    root.bind("<Escape>", lambda _event: root.withdraw())
    label = tk.Label(root, background="black", borderwidth=0)
    label.pack(fill="both", expand=True)
    canvas = tk.Canvas(root, background="black", highlightthickness=0, borderwidth=0)
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
