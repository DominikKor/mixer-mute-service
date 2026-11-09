"""Shared test helpers.

Use these instead of copying boilerplate into each module:

- ``Checker`` / ``check``: the PASS/FAIL counters of the script-style tests.
- ``temp_dir``: a temporary folder removed when the process exits.
- ``safe_config_yaml`` / ``use_app_config`` / ``app_client``: a service
  config that can never reach the real mixer (127.0.0.1, closed port 9) and
  never starts a real MediaMTX.
- ``wait_for``, ``FakeLink``, ``observe``: controller tests without a mixer.
- ``varint`` ... ``frame``: protobuf/frame encoders for parser tests.
- ``require_tk``: desktop-agent tests need tkinter; locally they are skipped
  without it, in CI (``REQUIRE_TK=1``) a missing tkinter is an error.

Nothing in here may open a network connection; the package's network guard
(tests/__init__.py) would block it anyway.
"""

from __future__ import annotations

import asyncio
import atexit
import contextlib
import os
import shutil
import struct
import tempfile
import time
from pathlib import Path
from typing import Callable, Iterator, Optional
from unittest.mock import patch

# Port 9 ("discard") is closed on every machine we test on: the mixer client
# keeps failing to connect, which is exactly what a test without a mixer wants.
SAFE_MIXER_IP = "127.0.0.1"
SAFE_MIXER_PORT = 9


# -- results of script-style tests ---------------------------------------------

class Checker:
    """PASS/FAIL counter with the "  ok   name" / "  FAIL name" output."""

    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def __call__(self, name: str, cond) -> bool:
        if cond:
            self.passed += 1
        else:
            self.failed += 1
        print(f"  {'ok  ' if cond else 'FAIL'} {name}")
        return bool(cond)

    def summary(self) -> int:
        """Print the totals; returns the process exit code (1 on any failure)."""
        print(f"\n{self.passed} passed, {self.failed} failed")
        return 1 if self.failed else 0


# -- temporary files and a safe service config ---------------------------------

def temp_dir(prefix: str = "mixer_test_") -> Path:
    """A new temporary folder that is removed again when the process exits."""
    path = Path(tempfile.mkdtemp(prefix=prefix))
    atexit.register(shutil.rmtree, path, True)
    return path


def safe_config_yaml(extra: str = "", max_channels: int = 8) -> str:
    """config.yaml text whose mixer section points at a closed local port.

    ``extra`` is appended as further top-level sections (desktop_display:,
    groups:, ...); it must not contain another ``mixer:`` section.
    """
    if "\nmixer:" in "\n" + extra:
        raise ValueError("extra must not contain a mixer section")
    return (
        f"mixer:\n  ip: {SAFE_MIXER_IP}\n  port: {SAFE_MIXER_PORT}\n"
        f"  max_channels: {max_channels}\n" + extra
    )


def use_app_config(prefix: str, extra: str = "", max_channels: int = 8) -> Path:
    """Write a safe config.yaml into a new temp folder, point MIXER_CONFIG at
    it and return the folder (the service keeps its data files next to it).

    For script-style modules that import app.main after setting MIXER_CONFIG.
    Each module runs in its own process, so the environment change stays there.
    """
    folder = temp_dir(prefix)
    (folder / "config.yaml").write_text(safe_config_yaml(extra, max_channels), encoding="utf-8")
    os.environ["MIXER_CONFIG"] = str(folder / "config.yaml")
    return folder


@contextlib.contextmanager
def no_mediamtx() -> Iterator[None]:
    """Never start a real MediaMTX (it may be installed on the streaming PC)."""
    with patch("app.camera_streams.MediaMTXRelay._binary", return_value=None):
        yield


@contextlib.contextmanager
def app_client(extra: str = "", env: Optional[dict] = None, max_channels: int = 8):
    """A started FastAPI TestClient on a safe temp config; yields (client, folder).

    Restores MIXER_CONFIG and ``env`` afterwards; MediaMTX is never started.
    """
    from fastapi.testclient import TestClient

    from app.main import app

    with tempfile.TemporaryDirectory(prefix="mixer_app_") as name:
        folder = Path(name)
        (folder / "config.yaml").write_text(safe_config_yaml(extra, max_channels), encoding="utf-8")
        variables = {"MIXER_CONFIG": str(folder / "config.yaml"), **(env or {})}
        with patch.dict(os.environ, variables), no_mediamtx(), TestClient(app) as client:
            yield client, folder


# -- controller tests without a mixer ------------------------------------------

async def wait_for(predicate: Callable[[], bool], timeout: float = 3.0, interval: float = 0.02) -> bool:
    """Poll ``predicate`` until it is true or ``timeout`` passes."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(interval)
    return bool(predicate())


class FakeLink:
    """Stands in for MixerClient/ProxyServer: connected, current session,
    records every payload the controller sends (as bytes)."""

    def __init__(self, session_age: float = 1.0) -> None:
        self.connected = True
        self.session_epoch = time.monotonic() - session_age
        self.sent: list = []

    def link_stale(self) -> bool:
        return False

    async def send(self, payload: bytes) -> None:
        self.sent.append(payload)


async def observe(store, channel: int, muted: Optional[bool], fader_hex: Optional[str],
                  solo: Optional[bool] = False, from_dump: bool = False) -> None:
    """Feed one observed state into a StateStore, as a mixer frame would."""
    from app.protocol import FrameEvent

    await store.apply_event(FrameEvent(channel, muted, fader_hex, from_dump=from_dump, solo=solo))


# -- protobuf / frame encoders ---------------------------------------------------

def varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def vfield(field: int, value: int) -> bytes:
    """Varint field."""
    return varint(field << 3) + varint(value)


def f32field(field: int, value: float) -> bytes:
    """Float (fixed32, little endian) field."""
    return varint(field << 3 | 5) + struct.pack("<f", value)


def bfield(field: int, data: bytes) -> bytes:
    """Length-delimited field."""
    return varint(field << 3 | 2) + varint(len(data)) + data


def frame(ftype: int, msg: bytes) -> bytes:
    """A full frame: magic, type, length, varint message length, message."""
    body = varint(len(msg)) + msg
    return bytes.fromhex("5a" * 6) + struct.pack(">II", ftype, len(body)) + body


# -- desktop agent tests -----------------------------------------------------------

def require_tk():
    """tkinter, or None when this Python has none (the agent tests then skip).

    With REQUIRE_TK=1 (CI) a missing tkinter raises instead, so a skipped
    agent test cannot look like a passed one.
    """
    try:
        import tkinter
    except ImportError:
        if os.environ.get("REQUIRE_TK") == "1":
            raise
        return None
    return tkinter
