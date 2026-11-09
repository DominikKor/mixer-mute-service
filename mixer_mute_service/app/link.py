"""What the controller needs from a mixer link, and what both links share.

Two transports implement the link: MixerClient (direct mode, our own
connection) and ProxyServer (proxy mode, relaying the iPad app). The
controller only ever talks to a ``MixerLink``; it never imports a transport.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import sys
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Protocol, Tuple

from .config import Config
from .protocol import BusMap, BusObjects, DumpListener, StreamBuffer

log = logging.getLogger("mixer.link")

# A write the mixer does not take within this time means the link is broken.
SEND_TIMEOUT_SECONDS = 2.0
# Opening the TCP connection to the mixer: without a limit an unreachable mixer
# stalls each attempt for the OS SYN timeout (20-75 s), so the reconnect
# backoff would mean nothing.
CONNECT_TIMEOUT_SECONDS = 5.0
# TCP keepalive on the mixer connection: a pulled cable or a dead switch port is
# noticed even while nobody sends (the link then ends and is reconnected).
KEEPALIVE_IDLE_SECONDS = 10
KEEPALIVE_INTERVAL_SECONDS = 3
KEEPALIVE_PROBES = 3


class MixerLink(Protocol):
    """The mixer connection as the controller sees it."""

    connected: bool
    # Monotonic time since which the link has been up without interruption
    # (None while down): values observed before it are not trusted.
    session_epoch: Optional[float]

    def start(self) -> None: ...

    async def stop(self) -> None: ...

    def link_stale(self) -> bool:
        """True while up but without fresh state from the mixer."""
        ...

    async def send(self, payload: bytes) -> None:
        """Write a command. Raises when it cannot be handed to the link."""
        ...


@dataclass
class ParserHooks:
    """Shared by every stream parser of a link: the bus map (grows at runtime),
    bus discovery and the listeners for decoded state dumps."""

    bus_map: BusMap
    on_bus_objects: Optional[Callable[[BusObjects], None]] = None
    dump_listeners: List[DumpListener] = field(default_factory=list)


def default_bus_map(cfg: Config) -> BusMap:
    return BusMap([(b.channel, b.id) for b in cfg.mixer.buses])


def new_stream_buffer(
    cfg: Config, hooks: ParserHooks, extra_listeners: Tuple[DumpListener, ...] = ()
) -> StreamBuffer:
    return StreamBuffer(
        buses=hooks.bus_map,
        max_input=cfg.mixer.max_channels,
        on_bus_objects=hooks.on_bus_objects,
        dump_listeners=[*hooks.dump_listeners, *extra_listeners],
    )


def enable_tcp_keepalive(writer: asyncio.StreamWriter) -> bool:
    """Turn on TCP keepalive with short timers (best effort, per platform).

    Never raises: the keepalive is an extra, a failure here must not cost the
    connection. asyncio hands out a TransportSocket wrapper, which has no
    ioctl() (Windows' way to set the timers), hence the real socket below.
    """
    sock = writer.get_extra_info("socket")
    if sock is None:
        return False
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        if sys.platform == "win32" and hasattr(socket, "SIO_KEEPALIVE_VALS"):
            raw = getattr(sock, "_sock", sock)
            raw.ioctl(socket.SIO_KEEPALIVE_VALS,
                      (1, KEEPALIVE_IDLE_SECONDS * 1000, KEEPALIVE_INTERVAL_SECONDS * 1000))
        else:
            idle = getattr(socket, "TCP_KEEPIDLE", None) or getattr(socket, "TCP_KEEPALIVE", None)
            if idle is not None:
                sock.setsockopt(socket.IPPROTO_TCP, idle, KEEPALIVE_IDLE_SECONDS)
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, KEEPALIVE_INTERVAL_SECONDS)
            if hasattr(socket, "TCP_KEEPCNT"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPCNT, KEEPALIVE_PROBES)
        return True
    except Exception as exc:  # noqa: BLE001 - best effort, see above
        log.warning("could not enable TCP keepalive: %s", exc)
        return False


async def open_mixer_connection(cfg: Config) -> Tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect to the mixer, giving up after CONNECT_TIMEOUT_SECONDS."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(cfg.mixer.ip, cfg.mixer.port), timeout=CONNECT_TIMEOUT_SECONDS
    )
    try:
        enable_tcp_keepalive(writer)
    except BaseException:
        # Never leave a half-set-up connection open on the mixer.
        writer.close()
        raise
    return reader, writer
