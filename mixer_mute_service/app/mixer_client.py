"""Direct connection to the mixer: persistent own-connection listener + sender.

Opens its own TCP connection, sends the subscribe handshake (so the mixer
streams its state dump + live updates -- verified to work without the iPad),
keeps it alive with periodic handshakes, parses the byte stream into state, and
reuses the same connection to inject commands. Reconnects with backoff. This is
"direct" mode; the iPad connects to the mixer normally and the service tracks
state independently. If no frames arrive, state stays unknown and every send is
blocked by the safety layer -- the desired fail-safe behaviour.

Link liveness: the mixer answers every keepalive handshake with a full dump, so
with a keepalive configured a silent link is a dead one (pulled cable, switch
outage, hung mixer). After LINK_TIMEOUT_FACTOR keepalive intervals without any
data the link counts as stale (sends are refused) and the read times out,
which forces a reconnect.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from .config import Config
from .protocol import SUBSCRIBE_HANDSHAKE_HEX, BusMap, StreamBuffer
from .state import StateStore

log = logging.getLogger("mixer.client")

# Reconnect backoff schedule (seconds), capped at the last value.
_BACKOFF = [1, 2, 5, 10]
# No data for this many keepalive intervals = the link is dead.
LINK_TIMEOUT_FACTOR = 2.5
# A write the mixer does not take within this time means the link is broken.
SEND_TIMEOUT_SECONDS = 2.0


class MixerClient:
    def __init__(self, cfg: Config, store: StateStore, bus_map: Optional[BusMap] = None) -> None:
        self._cfg = cfg
        self._store = store
        self._reader: Optional[asyncio.StreamReader] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        if bus_map is None:
            bus_map = BusMap([(b.channel, b.id) for b in cfg.mixer.buses])
        self._bus_map = bus_map
        self._buffer = self._new_buffer()
        self._send_lock = asyncio.Lock()
        self._stop = asyncio.Event()
        self._task: Optional[asyncio.Task] = None

        self.connected: bool = False
        # Monotonic time of the last bytes read (set to the connect time on
        # connect, so a link that never answers also goes stale).
        self.last_rx_at: Optional[float] = None
        self.session_epoch: Optional[float] = None  # set on connect, cleared on drop

    def _new_buffer(self) -> StreamBuffer:
        return StreamBuffer(buses=self._bus_map, max_input=self._cfg.mixer.max_channels)

    def _link_timeout(self) -> Optional[float]:
        interval = self._cfg.mixer.keepalive_seconds
        return interval * LINK_TIMEOUT_FACTOR if interval and interval > 0 else None

    def link_stale(self) -> bool:
        """True while connected but silent for longer than the link timeout."""
        timeout = self._link_timeout()
        if timeout is None or not self.connected or self.last_rx_at is None:
            return False
        return time.monotonic() - self.last_rx_at > timeout

    # -- lifecycle ---------------------------------------------------------

    def start(self) -> None:
        self._task = asyncio.create_task(self._run(), name="mixer-listener")

    async def stop(self) -> None:
        self._stop.set()
        if self._writer is not None:
            self._writer.close()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                self._task.cancel()

    # -- connection loop ---------------------------------------------------

    async def _run(self) -> None:
        attempt = 0
        while not self._stop.is_set():
            try:
                log.info("connecting to mixer %s:%s", self._cfg.mixer.ip, self._cfg.mixer.port)
                self._reader, self._writer = await asyncio.open_connection(
                    self._cfg.mixer.ip, self._cfg.mixer.port
                )
                # Leftover bytes of the previous connection would otherwise
                # swallow the start of this one -- the state dump.
                self._buffer = self._new_buffer()
                self.connected = True
                self.session_epoch = self.last_rx_at = time.monotonic()
                attempt = 0
                log.info("connected to mixer")
                # Subscribe so the mixer streams its state dump + live updates.
                await self.send(bytes.fromhex(SUBSCRIBE_HANDSHAKE_HEX))
                keepalive = asyncio.create_task(self._keepalive(), name="mixer-keepalive")
                try:
                    await self._read_loop()
                finally:
                    keepalive.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - reconnect on any error
                log.warning("mixer connection error: %s", exc)
            finally:
                self.connected = False
                self.session_epoch = None
                await self._close_writer()

            if self._stop.is_set():
                break

            delay = _BACKOFF[min(attempt, len(_BACKOFF) - 1)]
            attempt += 1
            log.info("reconnecting in %ss", delay)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    async def _read_loop(self) -> None:
        assert self._reader is not None
        timeout = self._link_timeout()
        while not self._stop.is_set():
            try:
                data = await asyncio.wait_for(self._reader.read(4096), timeout=timeout)
            except asyncio.TimeoutError:
                log.warning("no data from mixer for %.1fs; reconnecting", timeout)
                return
            if data == b"":
                log.info("mixer closed connection (EOF)")
                return
            self.last_rx_at = time.monotonic()
            await self._store.apply_events(self._buffer.feed(data))

    async def _keepalive(self) -> None:
        """Periodically resend the subscribe handshake so the mixer keeps us
        subscribed and the TCP link stays alive."""
        interval = self._cfg.mixer.keepalive_seconds
        if not interval or interval <= 0:
            return
        handshake = bytes.fromhex(SUBSCRIBE_HANDSHAKE_HEX)
        try:
            while self.connected and not self._stop.is_set():
                await asyncio.sleep(interval)
                if self.connected:
                    await self.send(handshake)
        except asyncio.CancelledError:
            pass
        except Exception as exc:  # noqa: BLE001
            # A keepalive that cannot be written means the link is broken:
            # tear it down so the read loop ends and we reconnect.
            log.warning("keepalive failed (%s); dropping the connection", exc)
            writer = self._writer
            if writer is not None:
                writer.close()

    async def _close_writer(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
                await asyncio.wait_for(self._writer.wait_closed(), timeout=2)
            except Exception:  # noqa: BLE001
                pass
            finally:
                self._writer = None
                self._reader = None

    # -- sending -----------------------------------------------------------

    async def send(self, payload: bytes) -> None:
        """Write raw bytes to the mixer. Callers MUST have passed safety checks.

        Raises if not connected or the write fails; never silently succeeds.
        """
        async with self._send_lock:
            # Checked inside the lock: the link may have dropped while waiting.
            writer = self._writer
            if not self.connected or writer is None or writer.is_closing():
                raise ConnectionError("mixer not connected")
            writer.write(payload)
            try:
                await asyncio.wait_for(writer.drain(), timeout=SEND_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                writer.close()  # ends the read loop -> reconnect
                raise ConnectionError("mixer did not accept data; connection dropped") from None
