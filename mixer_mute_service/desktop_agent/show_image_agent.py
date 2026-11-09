"""Authenticated LAN agent that displays configured images or launches profiles,
and shows FoilPresenter songs slide by slide (read from the local library)."""

from __future__ import annotations

import hmac
import json
import logging
import os
import queue
import getpass
import shutil
import subprocess
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
MAX_SONG_FILES = 5000
MAX_SONG_FILE_BYTES = 512 * 1024
_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")
# A new song's file name, as the service makes it (no folders, no odd characters).
_SONG_NAME_RE = re.compile(r"^[A-Za-z0-9 ._()!'+-]{1,120}\.foil$")
logger = logging.getLogger("display-agent")
_audio_lock = threading.Lock()
_audio_process: subprocess.Popen | None = None


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


def _stop_audio_locked() -> bool:
    global _audio_process
    process = _audio_process
    _audio_process = None
    if process is None or process.poll() is not None:
        return False
    process.terminate()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=3)
    return True


def _play_audio(profile_id: str, path: str) -> None:
    global _audio_process
    target = Path(path)
    if not target.is_absolute() or not target.is_file():
        raise ValueError("Configured audio file is not available")
    mpv = _find_mpv()
    if not mpv:
        raise FileNotFoundError("mpv was not found; install mpv or set MPV_PATH")

    with _audio_lock:
        _stop_audio_locked()
        process = subprocess.Popen(
            [
                mpv,
                "--no-config",
                "--no-video",
                "--vo=null",
                "--audio-display=no",
                "--force-window=no",
                "--no-terminal",
                "--really-quiet",
                "--",
                str(target),
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        _audio_process = process
    logger.info("Audio started profile=%s path=%s pid=%s", profile_id, target, process.pid)


def _stop_audio() -> bool:
    with _audio_lock:
        stopped = _stop_audio_locked()
    logger.info("Audio stopped active=%s", stopped)
    return stopped


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


# Slide layout. Keep in sync with songSlideLayout() in app/static/index.html,
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
        if self.path not in ("/display", "/audio", "/song", "/songs/library", "/songs/save", "/power"):
            self.send_error(404)
            return

        expected = os.environ.get("DESKTOP_AGENT_TOKEN", "")
        supplied = self.headers.get("X-Display-Token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            self.send_error(401)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            limit = {"/song": 32768, "/songs/save": MAX_SONG_FILE_BYTES + 8192}.get(self.path, 8192)
            if not 0 < length <= limit:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
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
            or set(body) != {"id", "action", "path"}
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
                _play_audio(body["id"], body["path"])
                result = {"ok": True, "playing": True, "profile": body["id"]}
            else:
                stopped = _stop_audio()
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

    def log_message(self, fmt, *args):
        logger.info("%s - %s", self.client_address[0], fmt % args)


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

    server = ThreadingHTTPServer((HOST, PORT), DisplayHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    logger.info("Display agent listening on %s:%s", HOST, PORT)
    root.mainloop()


if __name__ == "__main__":
    main()
