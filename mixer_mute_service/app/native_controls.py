"""Direct network controls for the imported local AV buttons."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import socket
import struct
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
    pass


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
        raise NativeControlError("Beamer HTTP control unavailable") from exc


def _shelly_rpc(host: str, method: str, params: dict) -> dict:
    request = Request(
        f"http://{host}/rpc/{method}",
        data=json.dumps(params).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=2.5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except (URLError, TimeoutError, OSError, ValueError) as exc:
        raise NativeControlError(f"Shelly {host} unreachable") from exc
    if isinstance(data, dict) and data.get("error"):
        raise NativeControlError(f"Shelly {host} rejected command")
    return data if isinstance(data, dict) else {}


def _send_artnet_frame(host: str, universe: int, values: list[int]) -> None:
    if len(values) != 512 or any(not 0 <= value <= 255 for value in values):
        raise NativeControlError("invalid Art-Net DMX frame")
    packet = (
        b"Art-Net\x00"
        + struct.pack("<H", 0x5000)
        + struct.pack(">H", 14)
        + bytes((0, 0))
        + struct.pack("<H", universe)
        + struct.pack(">H", 512)
        + bytes(values)
    )
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sender:
        sender.sendto(packet, (host, 6454))


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
    try:
        from websockets.sync.client import connect

        password = os.environ.get("OBS_WEBSOCKET_PASSWORD", "")
        with connect(f"ws://{host}:{port}", open_timeout=3, close_timeout=1) as ws:
            hello = json.loads(ws.recv())
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
            if json.loads(ws.recv()).get("op") != 2:
                raise NativeControlError("OBS WebSocket identification failed")
            request_id = uuid.uuid4().hex
            ws.send(json.dumps({
                "op": 6,
                "d": {"requestType": request_type, "requestId": request_id, "requestData": request_data or {}},
            }))
            while True:
                response = json.loads(ws.recv())
                if response.get("op") != 7 or response.get("d", {}).get("requestId") != request_id:
                    continue
                status = response["d"].get("requestStatus", {})
                if not status.get("result"):
                    raise NativeControlError(f"OBS rejected {request_type}")
                return response["d"].get("responseData", {})
    except NativeControlError:
        raise
    except Exception as exc:
        raise NativeControlError("OBS WebSocket unavailable") from exc


class NativeControlService:
    def __init__(self, state_path: Path) -> None:
        self._state_path = state_path
        self._lock = asyncio.Lock()
        self._dmx = [0] * 512
        self._levels = {"warm": 255, "cold": 50, "start": 255, "steps": 50, "fade_ms": 1000}
        self._beamer_hidden: Optional[bool] = None
        self._load_state()

    def _load_state(self) -> None:
        data = read_json(self._state_path, dict, default={})
        hidden = data.get("beamer_hidden")
        if isinstance(hidden, bool):
            self._beamer_hidden = hidden
        values = data.get("dmx")
        if isinstance(values, list) and len(values) == 512 and all(
            isinstance(v, int) and 0 <= v <= 255 for v in values
        ):
            self._dmx = values
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
                "levels": self._levels,
                "beamer_hidden": self._beamer_hidden,
            })
        except OSError as exc:
            log.warning("Could not save light state to %s: %s", self._state_path, exc)

    async def get_beamer_state(self) -> Optional[bool]:
        async with self._lock:
            return self._beamer_hidden

    async def set_beamer_state(self, hidden: bool) -> dict:
        if not isinstance(hidden, bool):
            raise NativeControlError("invalid Beamer state")
        async with self._lock:
            self._beamer_hidden = hidden
            self._persist_state()
            return {"ok": True, "hidden": hidden}

    async def _toggle_beamer(self) -> None:
        await asyncio.to_thread(_post_beamer_form, *BEAMER_COMMAND_FORMS["avmute"])

    async def _beamer_command(self, command: str) -> dict:
        form = BEAMER_COMMAND_FORMS.get(command)
        if form is None:
            raise NativeControlError("unknown Beamer command")
        await asyncio.to_thread(_post_beamer_form, *form)
        return {"ok": True, "command": command}

    async def _set_beamer_mode(self, hidden: bool) -> dict:
        async with self._lock:
            if self._beamer_hidden is None:
                raise NativeControlError("Beamer state is unknown; set its current state first")
            if self._beamer_hidden == hidden:
                return {"ok": True, "hidden": hidden, "changed": False}
            await self._toggle_beamer()
            self._beamer_hidden = hidden
            self._persist_state()
            return {"ok": True, "hidden": hidden, "changed": True}

    async def _update_dmx(self, compute: Callable[[list[int]], list[int]]) -> list[int]:
        """Fade from the current DMX frame to ``compute(current)``.

        The target is computed under the lock, so two quick presses build on
        each other instead of both starting from the same old frame.
        """
        async with self._lock:
            target = compute(self._dmx.copy())
            await self._fade_to(target)
            self._dmx = target
            self._persist_state()
            return target

    async def _fade_to(self, target: list[int]) -> None:
        """Send the fade frames. The caller holds ``self._lock``."""
        start = self._dmx.copy()
        frames = max(1, min(1500, self._levels["fade_ms"] // 40))
        for index in range(1, frames + 1):
            frame = [round(a + (b - a) * index / frames) for a, b in zip(start, target)]
            await asyncio.to_thread(_send_artnet_frame, ARTNET_HOST, ARTNET_UNIVERSE, frame)
            if index < frames:
                await asyncio.sleep(0.04)

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
            for target in targets:
                await asyncio.to_thread(
                    _shelly_rpc, SHELLY_HOSTS[target], "Switch.Set", {"id": 0, "on": bool(action.get("on"))}
                )
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

        if action_type == "artnet-zones-on":
            zones = action.get("zones")
            if not isinstance(zones, list) or not zones or any(z not in ARTNET_ZONES for z in zones):
                raise NativeControlError("unknown Art-Net zone")

            def switch_on(frame: list[int]) -> list[int]:
                for zone_name in zones:
                    white = frame[ARTNET_ZONES[zone_name]["white"] - 1]
                    self._set_zone(frame, zone_name, white or self._levels["start"])
                return frame

            await self._update_dmx(switch_on)
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

        if action_type == "beamer-set":
            hidden = action.get("hidden")
            if not isinstance(hidden, bool):
                raise NativeControlError("invalid Beamer target mode")
            return await self._set_beamer_mode(hidden)

        if action_type == "beamer-command":
            return await self._beamer_command(action.get("command"))

        raise NativeControlError("unsupported native action")
