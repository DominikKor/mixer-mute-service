"""Persist RTSP camera sources and expose them through a local MediaMTX relay."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Optional
from urllib.parse import quote, urlsplit, urlunsplit

import yaml

from .jsonstore import backup_corrupt, read_json, write_json_atomic

log = logging.getLogger("mixer.camera_streams")
HLS_PORT = 8888  # MediaMTX HLS, bound to 127.0.0.1 only
WEBRTC_PORT = 8889  # MediaMTX WebRTC signalling (WHEP), bound to 127.0.0.1 only
WEBRTC_MEDIA_PORT = 8189  # WebRTC media, UDP and TCP, reachable from the LAN
_PRIVATE_NETWORKS = tuple(map(ipaddress.ip_network, ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16")))


class CameraStreamError(ValueError):
    pass


def _validate_source(rtsp_url: str, username: str = "", password: str = "") -> str:
    if not isinstance(rtsp_url, str) or not rtsp_url or len(rtsp_url) > 1024:
        raise CameraStreamError("RTSP-Adresse ist erforderlich (maximal 1024 Zeichen)")
    if any(ord(char) < 32 or ord(char) == 127 for char in rtsp_url):
        raise CameraStreamError("RTSP-Adresse darf keine Steuerzeichen enthalten")
    try:
        parsed = urlsplit(rtsp_url.strip())
        address = ipaddress.ip_address(parsed.hostname or "")
        parsed_port = parsed.port
        port = (322 if parsed.scheme.lower() == "rtsps" else 554) if parsed_port is None else parsed_port
    except ValueError as exc:
        raise CameraStreamError("RTSP-Adresse muss eine private IPv4-Adresse und einen gueltigen Port enthalten") from exc
    if parsed.scheme.lower() not in ("rtsp", "rtsps") or parsed.username or parsed.password:
        raise CameraStreamError("Nur RTSP/RTSPS ohne Zugangsdaten in der URL ist erlaubt")
    if not isinstance(address, ipaddress.IPv4Address) or not any(address in network for network in _PRIVATE_NETWORKS):
        raise CameraStreamError("Kameras muessen eine private IPv4-Adresse verwenden")
    if not 1 <= port <= 65535 or not parsed.path or parsed.fragment:
        raise CameraStreamError("RTSP-Adresse benoetigt einen Pfad und einen gueltigen Port")
    if not isinstance(username, str) or not isinstance(password, str):
        raise CameraStreamError("Zugangsdaten muessen Text sein")
    if len(username) > 128 or len(password) > 256:
        raise CameraStreamError("Zugangsdaten sind zu lang")
    if any(ord(char) < 32 or ord(char) == 127 for char in username + password):
        raise CameraStreamError("Zugangsdaten duerfen keine Steuerzeichen enthalten")
    if password and not username:
        raise CameraStreamError("Ein Passwort benoetigt auch einen Benutzernamen")
    host = parsed.hostname
    netloc = f"{host}:{port}"
    if username:
        auth = quote(username, safe="")
        if password:
            auth += ":" + quote(password, safe="")
        netloc = f"{auth}@{netloc}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path, parsed.query, ""))


def _relay_config(cameras: list[dict], additional_host: str = "") -> dict:
    paths = {}
    for camera in cameras:
        paths[camera["id"]] = {
            "source": _validate_source(
                camera["rtsp_url"], camera.get("username", ""), camera.get("password", "")
            ),
            "sourceOnDemand": True,
            "rtspTransport": "tcp",
        }
    config = {
        "logLevel": "warn",
        # Only what the service passes on itself; MediaMTX would otherwise
        # also offer every camera (and publishing) to the whole LAN via
        # RTSP, RTMP and SRT.
        "rtsp": False,
        "rtmp": False,
        "srt": False,
        "api": False,
        "metrics": False,
        "pprof": False,
        # WebRTC for every device (lowest delay). The signalling (WHEP) stays
        # on localhost; the service passes it on through its own port
        # (app/camera_webrtc.py). Only the media port must be reachable from
        # the LAN: UDP, or TCP where UDP is dropped (install-service.ps1 opens
        # both in the Windows firewall).
        "webrtc": True,
        "webrtcAddress": f"127.0.0.1:{WEBRTC_PORT}",
        "webrtcLocalUDPAddress": f":{WEBRTC_MEDIA_PORT}",
        "webrtcLocalTCPAddress": f":{WEBRTC_MEDIA_PORT}",
        "webrtcIPsFromInterfaces": True,
        "webrtcAdditionalHosts": [additional_host] if additional_host else [],
        # The same cameras as low-latency HLS, also only on localhost and
        # passed on by the service (app/camera_hls.py): the player falls back
        # to it when the WebRTC media port cannot be reached. Parts of 200 ms;
        # how far HLS lags behind depends mostly on the cameras' keyframe
        # interval (a segment can only start at a keyframe), see README.
        "hls": True,
        "hlsAddress": f"127.0.0.1:{HLS_PORT}",
        "hlsVariant": "lowLatency",
        "hlsSegmentDuration": "1s",
        "hlsPartDuration": "200ms",
        "paths": paths,
    }
    return config


class CameraStreamStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        data = read_json(path, dict, default={})
        cameras = data.get("cameras", [])
        self._cameras = self._valid_entries(cameras if isinstance(cameras, list) else [])

    def _valid_entries(self, cameras: list) -> list[dict]:
        """Drop entries the relay could not use (one broken entry would
        otherwise keep every camera from starting). The file is kept as
        <name>.corrupt before the next save overwrites it."""
        valid = []
        for camera in cameras:
            try:
                if not isinstance(camera, dict) or not isinstance(camera.get("id"), str) or not camera["id"]:
                    raise CameraStreamError("missing id")
                if any(not isinstance(camera.get(key, ""), str) for key in ("username", "password")):
                    raise CameraStreamError("credentials must be text")
                self._validate_name(camera.get("name"))
                _validate_source(camera.get("rtsp_url"), camera.get("username", ""), camera.get("password", ""))
            except CameraStreamError as exc:
                log.error("Ignoring camera entry %r in %s: %s",
                          camera.get("id") if isinstance(camera, dict) else camera, self._path, exc)
                continue
            valid.append(camera)
        if len(valid) != len(cameras):
            try:
                backup_corrupt(self._path)
            except OSError as exc:
                log.error("Could not keep a copy of %s: %s", self._path, exc)
        return valid

    async def public_snapshot(self) -> list[dict]:
        async with self._lock:
            return [self._public(camera) for camera in self._cameras]

    async def relay_snapshot(self) -> list[dict]:
        async with self._lock:
            return [dict(camera) for camera in self._cameras]

    @staticmethod
    def _public(camera: dict) -> dict:
        return {
            "id": camera["id"],
            "name": camera["name"],
            "has_credentials": bool(camera.get("username") or camera.get("password")),
        }

    def _persist(self) -> None:
        write_json_atomic(self._path, {"cameras": self._cameras})

    async def add(self, name: str, rtsp_url: str, username: str = "", password: str = "") -> dict:
        name = self._validate_name(name)
        _validate_source(rtsp_url, username, password)
        async with self._lock:
            camera = {
                "id": "cam-" + uuid.uuid4().hex[:12],
                "name": name,
                "rtsp_url": rtsp_url.strip(),
                "username": username,
                "password": password,
            }
            self._cameras.append(camera)
            self._persist()
            return self._public(camera)

    async def update(
        self, camera_id: str, name: str, rtsp_url: str, username: str = "", password: str = ""
    ) -> dict:
        name = self._validate_name(name)
        async with self._lock:
            camera = next((item for item in self._cameras if item["id"] == camera_id), None)
            if camera is None:
                raise KeyError(camera_id)
            new_username = username or camera.get("username", "")
            new_password = password or camera.get("password", "")
            new_rtsp_url = rtsp_url.strip() or camera["rtsp_url"]
            _validate_source(new_rtsp_url, new_username, new_password)
            camera.update({"name": name, "rtsp_url": new_rtsp_url, "username": new_username, "password": new_password})
            self._persist()
            return self._public(camera)

    async def delete(self, camera_id: str) -> None:
        async with self._lock:
            updated = [camera for camera in self._cameras if camera["id"] != camera_id]
            if len(updated) == len(self._cameras):
                raise KeyError(camera_id)
            self._cameras = updated
            self._persist()

    @staticmethod
    def _validate_name(name: str) -> str:
        if not isinstance(name, str) or not name.strip() or len(name.strip()) > 64:
            raise CameraStreamError("Kameraname ist erforderlich (maximal 64 Zeichen)")
        if any(ord(char) < 32 or ord(char) == 127 for char in name):
            raise CameraStreamError("Kameraname darf keine Steuerzeichen enthalten")
        return name.strip()


class MediaMTXRelay:
    def __init__(self, data_dir: Path) -> None:
        self._data_dir = data_dir
        self._config_path = data_dir / "mediamtx-cameras.yml"
        self._process: Optional[subprocess.Popen] = None
        self._lock = asyncio.Lock()
        self._error: Optional[str] = None
        self._has_cameras = False
        self._cameras: list[dict] = []
        self._supervisor: Optional[asyncio.Task] = None

    @staticmethod
    def _binary() -> Optional[str]:
        configured = os.environ.get("MEDIAMTX_PATH", "").strip()
        if configured:
            if Path(configured).is_file():
                return configured
            found = shutil.which(configured)
            if found:
                return found
        return shutil.which("mediamtx") or shutil.which("mediamtx.exe")

    @property
    def ready(self) -> bool:
        return not self._has_cameras or (self._process is not None and self._process.poll() is None)

    @property
    def reason(self) -> Optional[str]:
        if self.ready:
            return None
        return self._error or "MediaMTX-Relay ist nicht gestartet"

    async def configure(self, cameras: list[dict]) -> dict:
        async with self._lock:
            return await asyncio.to_thread(self._configure, cameras)

    def _configure(self, cameras: list[dict]) -> dict:
        self._has_cameras = bool(cameras)
        self._cameras = [dict(camera) for camera in cameras]
        if not cameras:
            self._stop_process()
            self._config_path.unlink(missing_ok=True)
            (self._data_dir / "mediamtx.log").unlink(missing_ok=True)
            self._error = None
            return {"ready": True, "reason": None}
        binary = self._binary()
        if not binary:
            self._stop_process()
            self._error = "MediaMTX fehlt; MEDIAMTX_PATH setzen oder mediamtx in PATH installieren"
            return {"ready": False, "reason": self._error}
        config = _relay_config(cameras, os.environ.get("CAMERA_WEBRTC_HOST", "").strip())
        self._data_dir.mkdir(parents=True, exist_ok=True)
        temporary = self._config_path.with_suffix(".yml.tmp")
        temporary.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
        os.replace(temporary, self._config_path)
        self._stop_process()
        try:
            with (self._data_dir / "mediamtx.log").open("wb") as relay_log:
                self._process = subprocess.Popen(
                    [binary, str(self._config_path)],
                    cwd=str(self._data_dir),
                    stdin=subprocess.DEVNULL,
                    stdout=relay_log,
                    stderr=subprocess.STDOUT,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            time.sleep(0.2)
            if self._process.poll() is not None:
                self._process = None
                raise OSError("MediaMTX exited during startup")
        except OSError:
            log.exception("MediaMTX failed to start; camera source URLs are omitted from logs")
            self._process = None
            self._error = "MediaMTX konnte nicht gestartet werden; mediamtx-cameras.yml pruefen"
            return {"ready": False, "reason": self._error}
        self._error = None
        return {"ready": True, "reason": None}

    def start_supervision(self, interval: float = 10.0) -> None:
        """Restart MediaMTX when it exits on its own (crash, killed)."""
        if self._supervisor is None:
            self._supervisor = asyncio.create_task(self._supervise(interval))

    async def _supervise(self, interval: float) -> None:
        while True:
            await asyncio.sleep(interval)
            try:
                await self.restart_if_dead()
            except Exception:  # noqa: BLE001 - keep supervising
                log.exception("MediaMTX supervision failed")

    async def restart_if_dead(self) -> bool:
        async with self._lock:
            process = self._process
            if not self._has_cameras or (process is not None and process.poll() is None):
                return False
            if self._binary() is None:
                return False  # not installed: nothing to restart
            log.warning("MediaMTX is not running; starting it again")
            status = await asyncio.to_thread(self._configure, self._cameras)
            if not status["ready"]:
                log.warning("MediaMTX restart failed: %s", status["reason"])
            return True

    async def stop(self) -> None:
        if self._supervisor is not None:
            self._supervisor.cancel()
            self._supervisor = None
        async with self._lock:
            await asyncio.to_thread(self._stop_process)

    def _stop_process(self) -> None:
        process = self._process
        self._process = None
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)