"""Authenticated LAN agent that displays configured images or launches profiles."""

from __future__ import annotations

import hmac
import json
import os
import queue
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import tkinter as tk
from PIL import Image, ImageOps, ImageTk


HOST = "0.0.0.0"
PORT = 8766
requests: queue.Queue[dict] = queue.Queue()


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


def _place_process_window(process_id: int, monitor: int, fullscreen: bool) -> None:
    if not process_id:
        print("No process handle was returned; window placement skipped", flush=True)
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
    rect = wintypes.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
        raise ctypes.WinError()
    width = right - left if fullscreen else rect.right - rect.left
    height = bottom - top if fullscreen else rect.bottom - rect.top
    if fullscreen:
        style = user32.GetWindowLongW(hwnd, -16)
        user32.SetWindowLongW(hwnd, -16, (style & ~0x00CF0000) | 0x80000000)
        insert_after = -1
        flags = 0x0040 | 0x0020
    else:
        insert_after = -2
        flags = 0x0040
    user32.ShowWindow(hwnd, 9)
    if not user32.SetWindowPos(hwnd, insert_after, left, top, width, height, flags):
        raise ctypes.WinError()


def _launch_profile(profile: dict) -> None:
    path = Path(profile["path"])
    if profile["kind"] == "program":
        process_id = subprocess.Popen(
            [str(path), *profile["args"]], cwd=str(path.parent), shell=False
        ).pid
    else:
        process_id = _shell_open_file(path)
    _place_process_window(process_id, profile["monitor"], profile["fullscreen"])


def _show_image(root: tk.Tk, image_label: tk.Label, profile: dict) -> None:
    left, top, right, bottom = monitor_bounds(profile["monitor"])
    bounds = (right - left, bottom - top)
    with Image.open(profile["path"]) as source:
        if profile["fullscreen"]:
            image = ImageOps.fit(source.convert("RGB"), bounds)
        else:
            image = ImageOps.contain(source.convert("RGB"), bounds)
    photo = ImageTk.PhotoImage(image)
    root.withdraw()
    root.overrideredirect(profile["fullscreen"])
    root.attributes("-topmost", profile["fullscreen"])
    root.geometry(f"{image.width}x{image.height}+{left}+{top}")
    image_label.configure(image=photo)
    image_label.image = photo
    root.deiconify()
    root.lift()


class DisplayHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/display":
            self.send_error(404)
            return

        expected = os.environ.get("DESKTOP_AGENT_TOKEN", "")
        supplied = self.headers.get("X-Display-Token", "")
        if not expected or not hmac.compare_digest(supplied, expected):
            self.send_error(401)
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise ValueError("invalid request size")
            body = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self.send_error(400)
            return
        required = {"id", "title", "kind", "path", "args", "fullscreen", "monitor"}
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

    def log_message(self, fmt, *args):
        print("%s - %s" % (self.client_address[0], fmt % args), flush=True)


def drain_requests(root: tk.Tk, image_label: tk.Label) -> None:
    try:
        while True:
            profile = requests.get_nowait()
            try:
                if profile["kind"] == "image":
                    _show_image(root, image_label, profile)
                else:
                    threading.Thread(target=_launch_profile, args=(profile,), daemon=True).start()
            except Exception as exc:
                print(f"Profile {profile['id']} failed: {exc}", flush=True)
    except queue.Empty:
        pass
    root.after(100, drain_requests, root, image_label)


def main() -> None:
    if not os.environ.get("DESKTOP_AGENT_TOKEN"):
        raise SystemExit("Set DESKTOP_AGENT_TOKEN before starting the agent")

    root = tk.Tk()
    root.withdraw()
    root.configure(background="black")
    root.bind("<Escape>", lambda _event: root.withdraw())
    label = tk.Label(root, background="black", borderwidth=0)
    label.pack(fill="both", expand=True)
    root.after(100, drain_requests, root, label)

    server = ThreadingHTTPServer((HOST, PORT), DisplayHandler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"Display agent listening on {HOST}:{PORT}", flush=True)
    root.mainloop()


if __name__ == "__main__":
    main()
