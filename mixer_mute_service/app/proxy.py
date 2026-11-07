"""Transparent TCP proxy between the iPad app and the mixer.

The iPad connects to this service (point the app's mixer address at this host);
the service opens an upstream connection to the real mixer and relays bytes in
both directions, parsing every frame to keep channel state fresh.

This class is a drop-in for MixerClient: it exposes .start(), .stop(),
.connected, .session_epoch, .link_stale() and .send(payload) so the controller
and routes are unchanged. Injected commands (our safe mutes) are written to the
upstream link of the newest live session. The app's own bytes are relayed as
they arrive, but an injection only goes out while that link is at a frame
boundary: an app frame split across two TCP reads holds the link until its
second half is relayed, so an injected command can never land inside it (which
would corrupt the app's fader value).

Caveat (why "direct" is the default): the mixer does not echo commands back to
the connection they came from, so the app never learns about injected
commands; its next touch of that channel re-sends its stale mute flag.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .config import Config
from .protocol import BusMap, StreamBuffer
from .state import StateStore

log = logging.getLogger("mixer.proxy")

# How long an injection waits for the app's frame in progress to complete.
INJECT_WAIT_SECONDS = 1.0
# A write the peer does not take within this time means the link is broken.
SEND_TIMEOUT_SECONDS = 2.0


@dataclass(eq=False)
class _Session:
    peer: object
    mixer_writer: asyncio.StreamWriter
    client_writer: asyncio.StreamWriter
    # Held while upstream bytes are written and while the app's stream is
    # mid-frame; injections take it too.
    upstream_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def live(self) -> bool:
        return not self.mixer_writer.is_closing()


class ProxyServer:
    def __init__(self, cfg: Config, store: StateStore, bus_map: Optional[BusMap] = None) -> None:
        self._cfg = cfg
        self._store = store
        # Shared by both directions of every session, so a bus discovered from
        # a dump (mixer -> app) is also understood in app -> mixer commands.
        self._bus_map = bus_map or BusMap([(b.channel, b.id) for b in cfg.mixer.buses])
        self._server: Optional[asyncio.AbstractServer] = None
        self._task: Optional[asyncio.Task] = None
        self._sessions: List[_Session] = []

        self.connected: bool = False  # True while an iPad<->mixer session is live
        # Monotonic time since which at least one session has been live without
        # interruption; None when none is. Every change made in that period
        # passed through us, so values observed since then are current.
        self.session_epoch: Optional[float] = None

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._serve(), name="mixer-proxy")

    async def _serve(self) -> None:
        try:
            self._server = await asyncio.start_server(
                self._handle_client, self._cfg.mixer.listen_host, self._cfg.mixer.listen_port
            )
        except OSError as exc:
            log.error(
                "proxy cannot listen on %s:%s (%s); no app can connect, every send stays blocked",
                self._cfg.mixer.listen_host, self._cfg.mixer.listen_port, exc,
            )
            return
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
        for session in list(self._sessions):
            for w in (session.mixer_writer, session.client_writer):
                w.close()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass

    # -- per-session relay -------------------------------------------------

    def _update_link_state(self) -> None:
        self._sessions = [s for s in self._sessions if s.live()]
        if self._sessions:
            if self.session_epoch is None:
                self.session_epoch = time.monotonic()
            self.connected = True
        else:
            self.connected = False
            self.session_epoch = None

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

        session = _Session(peer, mixer_writer, client_writer)
        self._sessions.append(session)
        self._update_link_state()
        log.info("session established (iPad %s <-> mixer)", peer)

        up = asyncio.create_task(self._pump_up(client_reader, session))
        down = asyncio.create_task(self._pump_down(mixer_reader, session))
        try:
            await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for t in (up, down):
                t.cancel()
            for w in (mixer_writer, client_writer):
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass
            if session in self._sessions:
                self._sessions.remove(session)
            self._update_link_state()
            log.info("session closed (iPad %s)", peer)

    def _new_buffer(self) -> StreamBuffer:
        return StreamBuffer(buses=self._bus_map, max_input=self._cfg.mixer.max_channels)

    async def _pump_up(self, reader: asyncio.StreamReader, session: _Session) -> None:
        """Relay app -> mixer, holding the upstream lock while mid-frame."""
        debug = log.isEnabledFor(logging.DEBUG)
        buf = self._new_buffer()
        held = False
        try:
            while True:
                data = await reader.read(8192)
                if data == b"":
                    return  # peer closed -> ends the session
                if debug:
                    log.debug("ipad->mixer %d bytes: %s", len(data), data.hex())
                if not held:
                    await session.upstream_lock.acquire()
                    held = True
                session.mixer_writer.write(data)
                await asyncio.wait_for(session.mixer_writer.drain(), SEND_TIMEOUT_SECONDS)
                events = buf.feed(data)
                if buf.at_frame_boundary:
                    session.upstream_lock.release()
                    held = False
                await self._store.apply_events(events, "ipad->mixer")
        finally:
            if held:
                session.upstream_lock.release()

    async def _pump_down(self, reader: asyncio.StreamReader, session: _Session) -> None:
        """Relay mixer -> app, parsing status frames and dumps into state."""
        debug = log.isEnabledFor(logging.DEBUG)
        buf = self._new_buffer()
        while True:
            data = await reader.read(8192)
            if data == b"":
                return
            if debug:
                log.debug("mixer->ipad %d bytes: %s", len(data), data.hex())
            # Forward first (low latency for the live console), then parse.
            session.client_writer.write(data)
            await asyncio.wait_for(session.client_writer.drain(), SEND_TIMEOUT_SECONDS)
            await self._store.apply_events(buf.feed(data), "mixer->ipad")

    # -- sending (injection) ----------------------------------------------

    def link_stale(self) -> bool:
        """A proxied app may be idle for long stretches; a dead upstream ends
        its session (EOF / failed write) instead."""
        return False

    async def send(self, payload: bytes) -> None:
        """Inject a command into the upstream (mixer) link of the newest live
        session, at a frame boundary of the app's stream.

        Callers MUST have passed safety checks. Raises if no session is live
        or the app's frame in progress does not complete in time.
        """
        live = [s for s in self._sessions if s.live()]
        if not live:
            raise ConnectionError("no active iPad/mixer session")
        session = live[-1]
        try:
            await asyncio.wait_for(session.upstream_lock.acquire(), INJECT_WAIT_SECONDS)
        except asyncio.TimeoutError:
            raise ConnectionError("mixer link busy (app frame in progress)") from None
        try:
            if not session.live():
                raise ConnectionError("iPad/mixer session closed")
            session.mixer_writer.write(payload)
            try:
                await asyncio.wait_for(session.mixer_writer.drain(), SEND_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                session.mixer_writer.close()
                raise ConnectionError("mixer did not accept data; session dropped") from None
        finally:
            session.upstream_lock.release()
