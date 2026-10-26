"""Transparent TCP proxy between the iPad app and the mixer.

The iPad connects to this service (point the app's mixer address at this host);
the service opens an upstream connection to the real mixer and relays bytes in
both directions, parsing every frame to keep channel state fresh. Because all
iPad<->mixer traffic flows through here, the service always sees current fader
and mute values -- the problem the direct connection could not solve.

This class is a drop-in for MixerClient: it exposes .start(), .stop(),
.connected and .send(payload) so the controller/routes are unchanged. Injected
commands (our safe mutes) are written to the same upstream link as the iPad's
traffic, serialized so they never interleave mid-frame.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .config import Config
from .protocol import BusMap, StreamBuffer
from .state import StateStore

log = logging.getLogger("mixer.proxy")
# Set MIXER_PROXY_DEBUG=1 to log every relayed chunk's hex (protocol discovery).


class ProxyServer:
    def __init__(self, cfg: Config, store: StateStore, bus_map: Optional[BusMap] = None) -> None:
        self._cfg = cfg
        self._store = store
        # Shared by both directions of every session, so a bus discovered from
        # a dump (mixer -> app) is also understood in app -> mixer commands.
        self._bus_map = bus_map or BusMap([(b.channel, b.id) for b in cfg.mixer.buses])
        self._server: Optional[asyncio.AbstractServer] = None
        self._mixer_writer: Optional[asyncio.StreamWriter] = None
        self._send_lock = asyncio.Lock()
        self._sessions = 0

        self.connected: bool = False  # True while an iPad<->mixer session is live
        self.last_rx_at: Optional[float] = None
        # Monotonic time the current session began; None when no session is live.
        # Values observed at/after this are trusted (we relay every change).
        self.session_epoch: Optional[float] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._serve(), name="mixer-proxy")

    async def _serve(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._cfg.mixer.listen_host, self._cfg.mixer.listen_port
        )
        addr = ", ".join(str(s.getsockname()) for s in self._server.sockets)
        log.info(
            "proxy listening on %s -> mixer %s:%s (point the iPad app here)",
            addr, self._cfg.mixer.ip, self._cfg.mixer.port,
        )
        async with self._server:
            await self._server.serve_forever()

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    # -- per-session relay -------------------------------------------------

    async def _handle_client(
        self, client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter
    ) -> None:
        peer = client_writer.get_extra_info("peername")
        log.info("iPad connected from %s; opening upstream to mixer", peer)
        try:
            mixer_reader, mixer_writer = await asyncio.open_connection(
                self._cfg.mixer.ip, self._cfg.mixer.port
            )
        except Exception as exc:  # noqa: BLE001
            log.error("cannot reach mixer %s:%s: %s", self._cfg.mixer.ip, self._cfg.mixer.port, exc)
            client_writer.close()
            return

        self._mixer_writer = mixer_writer
        self._sessions += 1
        self.connected = True
        self.session_epoch = time.monotonic()
        log.info("session established (iPad %s <-> mixer)", peer)

        up = asyncio.create_task(
            self._pump(client_reader, mixer_writer, "ipad->mixer")
        )
        down = asyncio.create_task(
            self._pump(mixer_reader, client_writer, "mixer->ipad")
        )
        try:
            await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (up, down):
                t.cancel()
            self._sessions -= 1
            if self._sessions <= 0:
                self.connected = False
                self._mixer_writer = None
                self.session_epoch = None
            for w in (mixer_writer, client_writer):
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass
            log.info("session closed (iPad %s)", peer)

    async def _pump(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, label: str
    ) -> None:
        """Relay one direction, parsing frames into state as they pass through."""
        debug = log.isEnabledFor(logging.DEBUG)
        buf = StreamBuffer(buses=self._bus_map)
        while True:
            data = await reader.read(8192)
            if data == b"":
                return  # peer closed -> ends the session
            self.last_rx_at = time.monotonic()
            if debug:
                log.debug("%s %d bytes: %s", label, len(data), data.hex())

            # Forward first (low latency for the live console), then parse.
            async with self._send_lock:
                writer.write(data)
                await writer.drain()

            for event in buf.feed(data):
                if not await self._store.apply_event(event):
                    log.info("ignored dump value for channel=%s (we just commanded it)", event.channel)
                    continue
                log.debug(
                    "[%s] channel=%s muted=%s fader=%s",
                    label, event.channel, event.muted, event.fader_hex,
                )

    # -- sending (injection) ----------------------------------------------

    async def send(self, payload: bytes) -> None:
        """Inject a command into the upstream (mixer) link of the active session.

        Callers MUST have passed safety checks. Raises if no session is live.
        """
        if not self.connected or self._mixer_writer is None:
            raise ConnectionError("no active iPad/mixer session")
        async with self._send_lock:
            self._mixer_writer.write(payload)
            await self._mixer_writer.drain()

    # -- health ------------------------------------------------------------

    def rx_age(self) -> Optional[float]:
        if self.last_rx_at is None:
            return None
        return time.monotonic() - self.last_rx_at
