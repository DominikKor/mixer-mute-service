"""Direct network controls for the imported local AV buttons."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import http.client
import json
import logging
import os
import select
import socket
import struct
import threading
import time
import uuid
from pathlib import Path
from typing import Callable, Optional
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .jsonstore import read_json, write_json_atomic

log = logging.getLogger("mixer.native_controls")

SHELLY_HOSTS = {
    "light-left": "192.168.188.25",
    "light-center-left": "192.168.188.28",
    "light-center-right": "192.168.188.26",
    "light-right": "192.168.188.27",
    "light-front": "192.168.188.23",
    "light-tech": "192.168.188.31",
    "light-stage": "192.168.188.24",
}
ARTNET_HOST = "192.168.188.48"
ARTNET_UNIVERSE = 0
ARTNET_ZONES = {
    "left": {"white": 1, "warm": 2, "cold": 3},
    "center-left": {"white": 5, "warm": 6, "cold": 7},
    "center-right": {"white": 9, "warm": 10, "cold": 11},
    "right": {"white": 13, "warm": 14, "cold": 15},
}
# Names in the Licht section.
SHELLY_LABELS = {
    "light-left": "Links", "light-center-left": "Mitte links", "light-center-right": "Mitte rechts",
    "light-right": "Rechts", "light-front": "Vorne", "light-tech": "Technik", "light-stage": "Bühne",
}
ARTNET_ZONE_LABELS = {"left": "Links", "center-left": "Mitte links", "center-right": "Mitte rechts", "right": "Rechts"}
VISCA_CAMERAS = {"cam1": ("192.168.5.21", 5678), "cam2": ("192.168.5.22", 5678)}
OBS_HOST = "192.168.188.20"
OBS_PORT = 4455
BEAMER_CONTROL_URL = "http://192.168.6.40/form/control_cgi"
BEAMER_COMMAND_FORMS = {
    "avmute": ("avmute", "AV Mute"),
    "power-off": ("btn_powoff", "Power Off"),
    "power-on": ("btn_powon", "Power On"),
    "freeze": ("freeze", "Freeze"),
}
# Allowed range of each stored light setting.
_LEVEL_BOUNDS = {
    "warm": (0, 255), "cold": (0, 255), "start": (0, 255),
    "steps": (5, 255), "fade_ms": (0, 60000),
}


class NativeControlError(RuntimeError):
    """A device command that failed. ``maybe_sent``: it may still have
    reached the device (no answer in time), so its state is unknown."""

    def __init__(self, message: str, maybe_sent: bool = False) -> None:
        super().__init__(message)
        self.maybe_sent = maybe_sent


# OBS answers in milliseconds; a request that takes longer is stuck.
OBS_TIMEOUT_SECONDS = 3.0


def _post_beamer_form(field: str, value: str) -> None:
    request = Request(
        BEAMER_CONTROL_URL,
        data=urlencode({field: value}).encode("ascii"),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=3) as response:
            if not 200 <= response.status < 300:
                raise NativeControlError(f"Beamer rejected {field}")
    except NativeControlError:
        raise
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        reason = getattr(exc, "reason", exc)
        timed_out = isinstance(exc, TimeoutError) or isinstance(reason, TimeoutError)
        if timed_out:
            raise NativeControlError("Beamer did not answer in time", maybe_sent=True) from exc
        raise NativeControlError("Beamer HTTP control unavailable") from exc


# A Shelly that is on the network connects within milliseconds (the room
# light answers in 0.02-0.4 s); one that is off or gone never does. So the
# connect gets a short timeout and the answer a longer one: a dead circuit
# is given up after a second, a slow answer still counts.
SHELLY_CONNECT_TIMEOUT = 1.0
SHELLY_TIMEOUT = 2.5


def _shelly_rpc(host: str, method: str, params: dict) -> dict:
    conn = http.client.HTTPConnection(host, timeout=SHELLY_CONNECT_TIMEOUT)
    try:
        conn.connect()
        conn.sock.settimeout(SHELLY_TIMEOUT)
        conn.request("POST", f"/rpc/{method}", body=json.dumps(params).encode("utf-8"),
                     headers={"Content-Type": "application/json"})
        response = conn.getresponse()
        body = response.read()
        if response.status != 200:
            # Gen2+ answers a rejected call with an error object and a 4xx/5xx.
            try:
                rejected = isinstance(json.loads(body.decode("utf-8")), dict)
            except ValueError:
                rejected = False
            if rejected:
                raise NativeControlError(f"Shelly {host} rejected command")
            raise NativeControlError(f"Shelly {host} answered HTTP {response.status}")
        data = json.loads(body.decode("utf-8"))
    except NativeControlError:
        raise
    except (OSError, http.client.HTTPException, ValueError) as exc:
        raise NativeControlError(f"Shelly {host} unreachable") from exc
    finally:
        conn.close()
    if isinstance(data, dict) and data.get("error"):
        raise NativeControlError(f"Shelly {host} rejected command")
    return data if isinstance(data, dict) else {}


ARTNET_PORT = 0x1936  # 6454: Art-Net's port, as destination and (by the spec) as source
# The stage frame is sent again this often while nothing changes, like
# Companion's generic-artnet module (timer_slow 1000 ms). A node (or the
# DMX fixtures behind it) that gets no data for a few seconds drops to
# black, and a merging node forgets a source that went quiet: a frame sent
# only while fading lights the stage for a moment at best.
ARTNET_KEEPALIVE_SECONDS = 1.0
ARTNET_FRAME_SECONDS = 0.04  # one fade step, like Companion's timer_fast 40 ms
_ARTNET_ID = b"Art-Net\x00"
_ARTNET_PROTOCOL = struct.pack(">H", 14)


class _ArtNetSender:
    """One UDP socket for every Art-Net packet.

    A new socket per packet gave every frame a new source port. An Art-Net
    node tells its sources apart by address and port and merges them: a
    stream of "new sources" while a slider moves makes it jump between old
    and new values (the light flickers). So: one socket, bound to port 6454
    as the spec asks (another port if that is taken, e.g. by a lighting
    program on the same PC).

    The ArtDmx packet is the one Companion's generic-artnet module sends to
    this node (and the node follows): 512 channels, sequence 0 ("no
    sequence": a node that checks sequence numbers would drop frames after
    a counter wrap or a restart of the service), physical 0. Frames go out
    in order over one socket on the LAN, so nothing needs reordering.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._socket: Optional[socket.socket] = None

    def _sock(self) -> socket.socket:
        if self._socket is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.bind(("", ARTNET_PORT))
            except OSError as exc:
                log.warning("Art-Net: port %s is in use (%s); sending from another port", ARTNET_PORT, exc)
                sock.bind(("", 0))
            self._socket = sock
        return self._socket

    def send_dmx(self, host: str, universe: int, values: list[int]) -> None:
        if len(values) != 512 or any(not 0 <= value <= 255 for value in values):
            raise NativeControlError("invalid Art-Net DMX frame")
        with self._lock:
            packet = (
                _ARTNET_ID
                + struct.pack("<H", 0x5000)        # OpDmx, low byte first
                + _ARTNET_PROTOCOL                 # protocol version 14, high byte first
                + bytes((0, 0))                    # sequence 0 (none), physical port 0
                + struct.pack("<H", universe)      # SubUni, Net (15-bit Port-Address)
                + struct.pack(">H", 512)           # data length, high byte first
                + bytes(values)
            )
            try:
                self._sock().sendto(packet, (host, ARTNET_PORT))
            except OSError:
                self._close()  # a fresh socket next time
                raise

    def poll(self, host: str, timeout: float = 1.5) -> Optional[dict]:
        """ArtPoll the node; its ArtPollReply says its name and whether an
        output merges two sources (another Art-Net controller sends too)."""
        with self._lock:
            sock = self._sock()
            bound = sock.getsockname()[1]
            while select.select([sock], [], [], 0)[0]:  # drop anything old
                sock.recvfrom(2048)
            sock.sendto(_ARTNET_ID + struct.pack("<H", 0x2000) + _ARTNET_PROTOCOL + bytes((0, 0)),
                        (host, ARTNET_PORT))
            deadline = time.monotonic() + timeout
            while (left := deadline - time.monotonic()) > 0:
                if not select.select([sock], [], [], left)[0]:
                    break
                data, (address, _) = sock.recvfrom(2048)
                if address == host and data[:8] == _ARTNET_ID and data[8:10] == struct.pack("<H", 0x2100):
                    return _parse_poll_reply(data)
        if bound != ARTNET_PORT:
            raise NativeControlError(f"no ArtPollReply (port {ARTNET_PORT} is used by another program here)")
        return None

    def _close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None


def _parse_poll_reply(data: bytes) -> dict:
    def text(start: int, length: int) -> str:
        return data[start:start + length].split(b"\x00", 1)[0].decode("latin-1").strip()

    ports = min(data[173], 4) if len(data) > 173 else 0
    good_output = data[182:182 + ports] if len(data) >= 182 + ports else b""
    return {
        "short_name": text(26, 18),
        "long_name": text(44, 64),
        "report": text(108, 64),
        "outputs": [
            {
                "sending_dmx": bool(flags & 0x80),
                "merging": bool(flags & 0x08),       # two sources at once
                "merge_mode": "LTP" if flags & 0x02 else "HTP",
            }
            for flags in good_output
        ],
    }


_ARTNET = _ArtNetSender()


def _send_artnet_frame(host: str, universe: int, values: list[int]) -> None:
    _ARTNET.send_dmx(host, universe, values)


def artnet_status(host: Optional[str] = None) -> Optional[dict]:
    """The stage node's ArtPollReply (None: no answer)."""
    return _ARTNET.poll(host or ARTNET_HOST)


def _visca_tcp(host: str, port: int, payload: bytes) -> None:
    try:
        with socket.create_connection((host, port), timeout=2.5) as connection:
            connection.settimeout(0.5)
            connection.sendall(payload)
            try:
                connection.recv(64)
            except TimeoutError:
                pass
    except OSError as exc:
        raise NativeControlError(f"VISCA camera {host} unreachable") from exc


def _obs_request(host: str, port: int, request_type: str, request_data: Optional[dict] = None) -> dict:
    """One request on a fresh OBS WebSocket connection. Raises
    NativeControlError when OBS is unreachable or rejects the request."""
    (answer,) = _obs_batch(host, port, [(request_type, request_data)])
    if answer is None:
        raise NativeControlError(f"OBS rejected {request_type}")
    return answer


def _obs_batch(host: str, port: int, requests: list) -> list:
    """Several requests, one after the other, on one fresh OBS WebSocket
    connection: [(request_type, request_data), ...] -> their response data,
    None for a request OBS rejected (e.g. the preview scene outside studio
    mode). Every wait is bounded: a stuck OBS must not block a worker thread
    for good. Raises NativeControlError when OBS is unreachable."""
    try:
        from websockets.sync.client import connect

        password = os.environ.get("OBS_WEBSOCKET_PASSWORD", "")
        deadline = time.monotonic() + OBS_TIMEOUT_SECONDS

        def recv():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("OBS did not answer in time")
            return json.loads(ws.recv(timeout=remaining))

        with connect(f"ws://{host}:{port}", open_timeout=OBS_TIMEOUT_SECONDS, close_timeout=1) as ws:
            hello = recv()
            authentication = hello.get("d", {}).get("authentication")
            identify = {"op": 1, "d": {"rpcVersion": 1}}
            if authentication:
                if not password:
                    raise NativeControlError("OBS WebSocket password is required")
                secret = base64.b64encode(hashlib.sha256((password + authentication["salt"]).encode()).digest()).decode()
                identify["d"]["authentication"] = base64.b64encode(
                    hashlib.sha256((secret + authentication["challenge"]).encode()).digest()
                ).decode()
            ws.send(json.dumps(identify))
            if recv().get("op") != 2:
                raise NativeControlError("OBS WebSocket identification failed")
            answers = []
            for request_type, request_data in requests:
                request_id = uuid.uuid4().hex
                ws.send(json.dumps({
                    "op": 6,
                    "d": {"requestType": request_type, "requestId": request_id, "requestData": request_data or {}},
                }))
                while True:
                    response = recv()
                    if response.get("op") == 7 and response.get("d", {}).get("requestId") == request_id:
                        break
                status = response["d"].get("requestStatus", {})
                answers.append((response["d"].get("responseData") or {}) if status.get("result") else None)
            return answers
    except NativeControlError:
        raise
    except Exception as exc:
        raise NativeControlError("OBS WebSocket unavailable") from exc


# OBS accepts transition durations from 50 ms to 20 s.
OBS_TRANSITION_MS = (50, 20000)


def obs_status_blocking() -> dict:
    """What OBS has on air, in the preview (studio mode only) and its
    transition, over one connection. Raises NativeControlError."""
    program, studio, preview, transition = _obs_batch(OBS_HOST, OBS_PORT, [
        ("GetCurrentProgramScene", None), ("GetStudioModeEnabled", None),
        ("GetCurrentPreviewScene", None), ("GetCurrentSceneTransition", None),
    ])
    if program is None or not isinstance(program.get("currentProgramSceneName"), str):
        raise NativeControlError("OBS did not name its program scene")
    studio_mode = bool(studio and studio.get("studioModeEnabled"))
    duration = (transition or {}).get("transitionDuration")
    return {
        "program": program["currentProgramSceneName"],
        "preview": (preview or {}).get("currentPreviewSceneName") if studio_mode else None,
        "studio_mode": studio_mode,
        "transition": {
            "name": (transition or {}).get("transitionName"),
            "duration_ms": None if (transition or {}).get("transitionFixed") or not isinstance(duration, (int, float))
            else int(duration),
        },
    }


class NativeControlService:
    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path
        # Lights and beamer have their own locks: a long light fade must not
        # hold up a beamer hide (and the other way round).
        self._lock = asyncio.Lock()
        self._beamer_lock = asyncio.Lock()
        self._dmx = [0] * 512
        self._levels = {"warm": 255, "cold": 50, "start": 255, "steps": 50, "fade_ms": 1000}
        self._beamer_hidden: Optional[bool] = None
        # Stage sending: one frame at a time (fade steps and the keepalive),
        # when the last one went out, and whether this service drives the
        # stage at all. Until the first stage command it stays silent, so an
        # install next to a still-used Companion does not fight it; from then
        # on (also after a restart) it keeps sending its frame.
        self._send_lock = asyncio.Lock()
        self._sent_at = 0.0
        self._stage_active = False
        self._keepalive_task: Optional[asyncio.Task] = None
        self._keepalive_error: Optional[str] = None
        self._load_state()

    def _load_state(self) -> None:
        data = read_json(self._state_path, dict, default={})
        hidden = data.get("beamer_hidden")
        if isinstance(hidden, bool):
            self._beamer_hidden = hidden
        values = data.get("dmx")
        valid_dmx = isinstance(values, list) and len(values) == 512 and all(
            isinstance(v, int) and not isinstance(v, bool) and 0 <= v <= 255 for v in values
        )
        if valid_dmx:
            self._dmx = values
        # Without a valid saved frame the keepalive would send all zeros (a
        # blackout) after a restart: it stays silent until the next command.
        self._stage_active = valid_dmx and data.get("stage_active") is True
        if data.get("stage_active") is True and not valid_dmx:
            log.warning("Art-Net: saved stage frame is invalid; stage keepalive waits for the next light command")
        levels = data.get("levels")
        if isinstance(levels, dict):
            for key, (low, high) in _LEVEL_BOUNDS.items():
                if isinstance(levels.get(key), int):
                    self._levels[key] = max(low, min(high, levels[key]))

    def _persist_state(self) -> None:
        # A hardware command may already have run; a full disk must not turn
        # a successful press into an error.
        try:
            write_json_atomic(self._state_path, {
                "dmx": self._dmx,
                "stage_active": self._stage_active,
                "levels": self._levels,
                "beamer_hidden": self._beamer_hidden,
            })
        except OSError as exc:
            log.warning("Could not save light state to %s: %s", self._state_path, exc)

    async def get_beamer_state(self) -> Optional[bool]:
        async with self._beamer_lock:
            return self._beamer_hidden

    async def set_beamer_state(self, hidden: bool) -> dict:
        if not isinstance(hidden, bool):
            raise NativeControlError("invalid Beamer state")
        async with self._beamer_lock:
            self._beamer_hidden = hidden
            self._persist_state()
            return {"ok": True, "hidden": hidden}

    async def _toggle_beamer(self) -> None:
        await asyncio.to_thread(_post_beamer_form, *BEAMER_COMMAND_FORMS["avmute"])

    async def _beamer_command(self, command: str) -> dict:
        form = BEAMER_COMMAND_FORMS.get(command)
        if form is None:
            raise NativeControlError("unknown Beamer command")
        if command == "avmute":
            # A raw toggle: keep the known hide state in step with it.
            async with self._beamer_lock:
                if self._beamer_hidden is None:
                    await asyncio.to_thread(_post_beamer_form, *form)
                else:
                    await self._set_beamer_mode_locked(not self._beamer_hidden)
            return {"ok": True, "command": command}
        await asyncio.to_thread(_post_beamer_form, *form)
        return {"ok": True, "command": command}

    async def _set_beamer_mode(self, hidden: bool) -> dict:
        async with self._beamer_lock:
            return await self._set_beamer_mode_locked(hidden)

    async def _set_beamer_mode_locked(self, hidden: bool) -> dict:
        """The caller holds ``self._beamer_lock``."""
        if self._beamer_hidden is None:
            raise NativeControlError("Beamer state is unknown; set its current state first")
        if self._beamer_hidden == hidden:
            return {"ok": True, "hidden": hidden, "changed": False}
        try:
            # AV mute is a toggle: the state is only known while every
            # toggle is known to have arrived (or known not to have).
            await self._toggle_beamer()
        except NativeControlError as exc:
            if exc.maybe_sent:
                self._beamer_hidden = None
                self._persist_state()
                log.warning("Beamer AV mute may or may not have switched; state is now unknown")
            raise
        self._beamer_hidden = hidden
        self._persist_state()
        return {"ok": True, "hidden": hidden, "changed": True}

    async def _update_dmx(self, compute: Callable[[list[int]], list[int]], fade: bool = True) -> list[int]:
        """Fade from the current DMX frame to ``compute(current)``.

        The target is computed under the lock, so two quick presses build on
        each other instead of both starting from the same old frame.
        """
        async with self._lock:
            target = compute(self._dmx.copy())
            await self._fade_to(target, fade)
            self._dmx = target
            self._persist_state()
            return target

    async def _fade_to(self, target: list[int], fade: bool = True) -> None:
        """Send the fade frames (one frame without fade). The caller holds
        ``self._lock``."""
        self._stage_active = True  # from now on the keepalive holds the stage
        start = self._dmx.copy()
        frames = max(1, min(1500, self._levels["fade_ms"] // 40)) if fade else 1
        for index in range(1, frames + 1):
            frame = [round(a + (b - a) * index / frames) for a, b in zip(start, target)]
            await self._send_frame(frame)
            if index < frames:
                await asyncio.sleep(ARTNET_FRAME_SECONDS)

    async def _send_frame(self, frame: list[int]) -> None:
        """One ArtDmx frame to the stage; fade steps and the keepalive take
        turns, so frames leave in the order they were made."""
        async with self._send_lock:
            try:
                await asyncio.to_thread(_send_artnet_frame, ARTNET_HOST, ARTNET_UNIVERSE, frame)
            finally:
                # Also after a failure: the keepalive retries a second later,
                # not in a tight loop.
                self._sent_at = time.monotonic()

    # -- stage keepalive -----------------------------------------------------

    def start_keepalive(self) -> None:
        """Start resending the stage frame (called once the event loop runs)."""
        if self._keepalive_task is None or self._keepalive_task.done():
            self._keepalive_task = asyncio.get_running_loop().create_task(self._keepalive())

    async def stop_keepalive(self) -> None:
        task, self._keepalive_task = self._keepalive_task, None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:  # noqa: BLE001 - shutdown must go on (mixer link, cameras)
                log.exception("Art-Net: stage keepalive ended with an error")

    def keepalive_status(self) -> dict:
        return {
            "active": self._stage_active,
            "running": self._keepalive_task is not None and not self._keepalive_task.done(),
            "error": self._keepalive_error,
        }

    async def _keepalive(self) -> None:
        failing = False
        while True:
            if self._stage_active:
                wait = ARTNET_KEEPALIVE_SECONDS - (time.monotonic() - self._sent_at)
                await asyncio.sleep(max(ARTNET_FRAME_SECONDS, wait))
            else:
                # Nothing to hold yet: a stage command sends its own frames.
                await asyncio.sleep(ARTNET_KEEPALIVE_SECONDS)
            try:
                await self.keepalive_tick()
                failing = False
            except Exception as exc:  # noqa: BLE001 - the stage must not go dark for good
                if not failing:
                    log.exception("Art-Net: stage keepalive failed; it keeps trying every second")
                failing = True
                self._keepalive_error = f"{type(exc).__name__}: {exc}"
                await asyncio.sleep(ARTNET_KEEPALIVE_SECONDS)

    async def keepalive_tick(self) -> bool:
        """Send the stage frame again when nothing went out for a second.
        Not while a fade or another change runs (it sends its own frames,
        and the frame it ends on becomes the one resent here)."""
        if not self._stage_active or self._lock.locked():
            return False
        if time.monotonic() - self._sent_at < ARTNET_KEEPALIVE_SECONDS:
            return False
        try:
            await self._send_frame(self._dmx.copy())
        except (OSError, NativeControlError) as exc:
            if self._keepalive_error is None:
                log.warning("Art-Net: stage frame not sent (%s); retrying every second", exc)
            self._keepalive_error = str(exc)
            return False
        if self._keepalive_error is not None:
            log.info("Art-Net: stage frames go out again")
            self._keepalive_error = None
        return True

    def _set_zone(self, frame: list[int], zone_name: str, white: int) -> None:
        zone = ARTNET_ZONES.get(zone_name)
        if zone is None:
            raise NativeControlError("unknown Art-Net zone")
        frame[zone["white"] - 1] = max(0, min(255, white))
        frame[zone["warm"] - 1] = self._levels["warm"]
        frame[zone["cold"] - 1] = self._levels["cold"]

    async def _adjust_setting(self, key: str, delta: int) -> int:
        low, high = _LEVEL_BOUNDS[key]
        async with self._lock:
            self._levels[key] = max(low, min(high, self._levels[key] + delta))
            self._persist_state()
            return self._levels[key]

    # -- Licht section: absolute values and the room light's state ----------

    def stage_state(self) -> dict:
        """Stage zones (white 0..255) and the light settings as last sent.
        Read without the lock: a fade in progress must not hold up a read."""
        return {
            "zones": {name: self._dmx[zone["white"] - 1] for name, zone in ARTNET_ZONES.items()},
            **{key: self._levels[key] for key in _LEVEL_BOUNDS},
        }

    async def set_stage(self, zones: Optional[dict] = None, warm: Optional[int] = None,
                        cold: Optional[int] = None, fade: bool = True) -> dict:
        """Fade the stage to absolute values; zones not named keep theirs.
        fade=False jumps (a slider being dragged sends many small steps)."""
        zones = zones or {}
        if any(name not in ARTNET_ZONES for name in zones):
            raise NativeControlError("unknown Art-Net zone")
        for value in [*zones.values(), warm, cold]:
            if value is not None and (not isinstance(value, int) or isinstance(value, bool) or not 0 <= value <= 255):
                raise NativeControlError("light values must be 0 to 255")

        def compute(frame: list[int]) -> list[int]:
            if warm is not None:
                self._levels["warm"] = warm
            if cold is not None:
                self._levels["cold"] = cold
            for name, zone in ARTNET_ZONES.items():
                self._set_zone(frame, name, zones.get(name, frame[zone["white"] - 1]))
            return frame

        await self._update_dmx(compute, fade)
        return self.stage_state()

    async def set_light_settings(self, values: dict) -> dict:
        """Start brightness, dimming step and fade time (Steuerung keys and the
        Licht section share them)."""
        for key, value in values.items():
            if key not in ("start", "steps", "fade_ms"):
                raise NativeControlError(f"unknown light setting {key}")
            low, high = _LEVEL_BOUNDS[key]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise NativeControlError(f"{key} must be {low} to {high}")
        async with self._lock:
            self._levels.update(values)
            self._persist_state()
        return self.stage_state()

    async def circuit_state(self, target: str) -> Optional[bool]:
        """On/off of one room light circuit as its Shelly reports it; None
        when it does not answer."""
        try:
            data = await asyncio.to_thread(_shelly_rpc, SHELLY_HOSTS[target], "Switch.GetStatus", {"id": 0})
        except NativeControlError:
            return None
        return data.get("output") if isinstance(data.get("output"), bool) else None

    async def circuit_states(self) -> dict:
        """On/off of every room light circuit (asked in parallel); None where
        it does not answer."""
        states = await asyncio.gather(*(self.circuit_state(target) for target in SHELLY_HOSTS))
        return dict(zip(SHELLY_HOSTS, states))

    async def set_circuits(self, states: dict) -> dict:
        """Switch room light circuits; every circuit is tried, failures are
        listed (target -> reason)."""
        if not states or any(target not in SHELLY_HOSTS for target in states):
            raise NativeControlError("unknown Shelly target")
        if any(not isinstance(on, bool) for on in states.values()):
            raise NativeControlError("circuit state must be true or false")

        async def one(target: str, on: bool):
            try:
                await asyncio.to_thread(_shelly_rpc, SHELLY_HOSTS[target], "Switch.Set", {"id": 0, "on": on})
                return None
            except NativeControlError as exc:
                return str(exc)

        errors = await asyncio.gather(*(one(target, on) for target, on in states.items()))
        return {target: error for target, error in zip(states, errors) if error}

    async def press(self, action: dict) -> dict:
        try:
            return await self._press(action)
        except NativeControlError:
            raise
        except (OSError, ValueError, TypeError) as exc:
            # Network/socket/process errors and malformed actions all become a
            # clear 503 instead of a 500 with a stack trace.
            raise NativeControlError(f"{action.get('type', 'native action')} failed: {exc}") from exc

    async def _press(self, action: dict) -> dict:
        action_type = action.get("type")
        if action_type in {"shelly-toggle", "shelly-set"}:
            host = SHELLY_HOSTS.get(action.get("target"))
            if host is None:
                raise NativeControlError("unknown Shelly target")
            if action_type == "shelly-toggle":
                await asyncio.to_thread(_shelly_rpc, host, "Switch.Toggle", {"id": 0})
            else:
                await asyncio.to_thread(_shelly_rpc, host, "Switch.Set", {"id": 0, "on": bool(action.get("on"))})
            return {"ok": True}

        if action_type in {"shelly-set-all", "shelly-set-group"}:
            targets = list(SHELLY_HOSTS) if action_type == "shelly-set-all" else action.get("targets", [])
            if not targets or any(target not in SHELLY_HOSTS for target in targets):
                raise NativeControlError("invalid Shelly group")
            # All at once, and every circuit is tried: one dead Shelly must
            # neither delay nor stop the others.
            on = bool(action.get("on"))
            errors = await self.set_circuits({target: on for target in targets})
            if errors:
                raise NativeControlError(f"room light not switched: {', '.join(sorted(errors))}")
            return {"ok": True, "count": len(targets)}

        if action_type == "artnet-zone-toggle":
            zone_name = action.get("zone")
            zone = ARTNET_ZONES.get(zone_name)
            if zone is None:
                raise NativeControlError("unknown Art-Net zone")

            def toggle(frame: list[int]) -> list[int]:
                is_off = frame[zone["white"] - 1] == 0
                self._set_zone(frame, zone_name, self._levels["start"] if is_off else 0)
                return frame

            target = await self._update_dmx(toggle)
            return {"ok": True, "level": target[zone["white"] - 1]}

        if action_type == "artnet-zones-set":
            # Companion's Overall keys: these zones on (a zone that is on keeps
            # its level, one that is off comes up to the start brightness),
            # those off; zones not named stay as they are.
            on, off = action.get("on") or [], action.get("off") or []
            if not isinstance(on, list) or not isinstance(off, list) or not (on or off) \
                    or any(z not in ARTNET_ZONES for z in [*on, *off]) or set(on) & set(off):
                raise NativeControlError("invalid Art-Net zone set")

            def switch(frame: list[int]) -> list[int]:
                for zone_name in on:
                    white = frame[ARTNET_ZONES[zone_name]["white"] - 1]
                    self._set_zone(frame, zone_name, white or self._levels["start"])
                for zone_name in off:
                    self._set_zone(frame, zone_name, 0)
                return frame

            await self._update_dmx(switch)
            return {"ok": True}

        if action_type in {"artnet-zone-adjust", "artnet-all-adjust"}:
            zones = list(ARTNET_ZONES) if action_type == "artnet-all-adjust" else [action.get("zone")]
            if any(zone_name not in ARTNET_ZONES for zone_name in zones):
                raise NativeControlError("unknown Art-Net zone")
            # The button only gives the direction; "Dimm-Schrittweite" sets
            # how far one press dims.
            direction = 1 if int(action.get("delta", 0)) > 0 else -1

            def adjust(frame: list[int]) -> list[int]:
                step = direction * self._levels["steps"]
                for zone_name in zones:
                    white = frame[ARTNET_ZONES[zone_name]["white"] - 1]
                    self._set_zone(frame, zone_name, white + step)
                return frame

            await self._update_dmx(adjust)
            return {"ok": True}

        if action_type == "artnet-global-adjust":
            color = action.get("color")
            if color not in {"warm", "cold"}:
                raise NativeControlError("unknown Art-Net color control")
            delta = int(action.get("delta", 0))

            def recolor(frame: list[int]) -> list[int]:
                self._levels[color] = max(0, min(255, self._levels[color] + delta))
                for zone in ARTNET_ZONES.values():
                    frame[zone[color] - 1] = self._levels[color]
                return frame

            await self._update_dmx(recolor)
            return {"ok": True, "level": self._levels[color]}

        if action_type == "artnet-start-adjust":
            level = await self._adjust_setting("start", int(action.get("delta", 0)))
            return {"ok": True, "level": level}

        if action_type == "artnet-setting-adjust":
            setting = action.get("setting")
            if setting not in {"steps", "fade_ms"}:
                raise NativeControlError("unknown Art-Net setting")
            value = await self._adjust_setting(setting, int(action.get("delta", 0)))
            return {"ok": True, "value": value}

        if action_type == "visca-preset":
            target = VISCA_CAMERAS.get(action.get("camera"))
            preset = action.get("preset")
            if target is None or not isinstance(preset, int) or not 1 <= preset <= 16:
                raise NativeControlError("invalid VISCA preset")
            await asyncio.to_thread(_visca_tcp, *target, bytes((0x81, 0x01, 0x04, 0x3F, 0x02, preset, 0xFF)))
            return {"ok": True}

        if action_type == "visca-zoom":
            target = VISCA_CAMERAS.get(action.get("camera"))
            zoom = {"in": 0x20, "out": 0x30, "stop": 0x00}.get(action.get("direction"))
            if target is None or zoom is None:
                raise NativeControlError("invalid VISCA zoom action")
            await asyncio.to_thread(_visca_tcp, *target, bytes((0x81, 0x01, 0x04, 0x07, zoom, 0xFF)))
            return {"ok": True}

        if action_type in {"obs-scene", "obs-live", "obs-source-toggle"}:
            scene = action.get("scene")
            if not isinstance(scene, str) or not scene:
                raise NativeControlError("invalid OBS scene")
            if scene == "Current Scene":
                scene = (await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "GetCurrentProgramScene"))
                scene = scene.get("currentProgramSceneName")
            source_scene = action.get("source_scene", scene) if action_type == "obs-live" else scene
            if source_scene == "Current Scene":
                current = await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "GetCurrentProgramScene")
                source_scene = current.get("currentProgramSceneName")
            if not isinstance(source_scene, str) or not source_scene:
                raise NativeControlError("invalid OBS source scene")
            source = action.get("source")
            if action_type == "obs-scene":
                await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "SetCurrentProgramScene", {"sceneName": scene})
                return {"ok": True}
            if not isinstance(source, str):
                raise NativeControlError("invalid OBS source action")
            data = await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "GetSceneItemList", {"sceneName": source_scene})
            items = data.get("sceneItems", [])
            item = next((entry for entry in items if entry.get("sourceName") == source), None)
            if item is None and action_type == "obs-live":
                # Making the source visible is a courtesy; going live is the
                # point. Companion carried on as well when a scene lacked the
                # source (the "Cam N Live" keys used to fail in every scene
                # without a "CamN" source, e.g. PC2 or a title).
                log.info("OBS: %s not in scene %s; switching to %s anyway", source, source_scene, scene)
                await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "SetCurrentProgramScene", {"sceneName": scene})
                return {"ok": True, "source_missing": True}
            if item is None:
                raise NativeControlError(f"OBS source not found: {source}")
            visible = True if action_type == "obs-live" else not bool(item.get("sceneItemEnabled"))
            for hidden_source in action.get("hide_sources", []):
                other = next((entry for entry in items if entry.get("sourceName") == hidden_source), None)
                if other:
                    await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "SetSceneItemEnabled", {
                        "sceneName": source_scene, "sceneItemId": other["sceneItemId"], "sceneItemEnabled": False,
                    })
            await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "SetSceneItemEnabled", {
                "sceneName": source_scene, "sceneItemId": item["sceneItemId"], "sceneItemEnabled": visible,
            })
            if action_type == "obs-live":
                await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "SetCurrentProgramScene", {"sceneName": scene})
            return {"ok": True}

        if action_type == "obs-overlay-toggle":
            return await self._overlay_toggle(action)

        if action_type == "obs-transition-adjust":
            # Companion's "Übergang Langsam/Schneller": the transition OBS uses
            # for every switch, a step longer or shorter.
            delta = int(action.get("delta", 0))
            current = await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "GetCurrentSceneTransition")
            duration = current.get("transitionDuration")
            if current.get("transitionFixed") or not isinstance(duration, (int, float)):
                raise NativeControlError(f"OBS transition has no duration ({current.get('transitionName')})")
            low, high = OBS_TRANSITION_MS
            duration = max(low, min(high, int(duration) + delta))
            await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "SetCurrentSceneTransitionDuration",
                                    {"transitionDuration": duration})
            return {"ok": True, "duration_ms": duration}

        if action_type == "beamer-set":
            hidden = action.get("hidden")
            if not isinstance(hidden, bool):
                raise NativeControlError("invalid Beamer target mode")
            return await self._set_beamer_mode(hidden)

        if action_type == "beamer-command":
            return await self._beamer_command(action.get("command"))

        raise NativeControlError("unsupported native action")

    async def _overlay_toggle(self, action: dict) -> dict:
        """Companion's "Bibel Cam N Beamer": show or hide an overlay source
        (the Bible text) in a camera scene and put that scene on air. While the
        overlay shows, the projector is hidden (the stream shows the text);
        hiding the overlay shows the projector again. The scene is switched
        even if the projector fails; the failure is reported."""
        scene, source = action.get("scene"), action.get("source")
        if not isinstance(scene, str) or not scene or not isinstance(source, str) or not source:
            raise NativeControlError("invalid OBS overlay action")
        data = await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "GetSceneItemList", {"sceneName": scene})
        item = next((entry for entry in data.get("sceneItems", []) if entry.get("sourceName") == source), None)
        if item is None:
            raise NativeControlError(f"OBS source not found: {source}")
        shown = not bool(item.get("sceneItemEnabled"))
        await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "SetSceneItemEnabled", {
            "sceneName": scene, "sceneItemId": item["sceneItemId"], "sceneItemEnabled": shown,
        })
        beamer_error = None
        if action.get("hide_beamer_while_shown"):
            try:
                await self._set_beamer_mode(shown)
            except NativeControlError as exc:
                beamer_error = str(exc)
        await asyncio.to_thread(_obs_request, OBS_HOST, OBS_PORT, "SetCurrentProgramScene", {"sceneName": scene})
        if beamer_error:
            raise NativeControlError(f"{source} {'shown' if shown else 'hidden'}, but beamer: {beamer_error}")
        return {"ok": True, "shown": shown}
