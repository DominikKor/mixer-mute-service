"""TCP keepalive on Windows must never cost the mixer connection.

Release v1.2.1-112 never connected on the streaming PC: on Windows the
keepalive timers are set with socket.ioctl(), but asyncio hands out a
TransportSocket wrapper without ioctl(). The AttributeError escaped
open_mixer_connection(), so every attempt ended right after connecting and
the mixer stayed "offline". The Mac never takes that path, so these tests
pretend to be Windows. Localhost only.
"""

import asyncio
import socket
import types
import unittest
from unittest import mock

import app.link as link_mod
from app.config import Config

SIO_KEEPALIVE_VALS = getattr(socket, "SIO_KEEPALIVE_VALS", 0x98000004)


class RawSocket:
    """The real socket: has ioctl() (on Windows)."""

    def __init__(self):
        self.ioctl_calls = []

    def ioctl(self, control, value):
        self.ioctl_calls.append((control, value))


class TransportSocketLike:
    """asyncio's wrapper: setsockopt() yes, ioctl() no, the socket in _sock."""

    def __init__(self, raw):
        self._sock = raw
        self.options = []

    def setsockopt(self, *args):
        self.options.append(args)


def writer_with(sock):
    return types.SimpleNamespace(get_extra_info=lambda name: sock if name == "socket" else None, close=mock.Mock())


def as_windows():
    return mock.patch.multiple(
        link_mod,
        sys=types.SimpleNamespace(platform="win32"),
        socket=types.SimpleNamespace(**{n: getattr(socket, n) for n in ("SOL_SOCKET", "SO_KEEPALIVE", "IPPROTO_TCP")},
                                     SIO_KEEPALIVE_VALS=SIO_KEEPALIVE_VALS),
    )


class WindowsKeepaliveTests(unittest.TestCase):
    def test_timers_set_on_the_real_socket(self):
        raw = RawSocket()
        wrapper = TransportSocketLike(raw)
        with as_windows():
            self.assertTrue(link_mod.enable_tcp_keepalive(writer_with(wrapper)))
        self.assertEqual(wrapper.options, [(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)])
        self.assertEqual(raw.ioctl_calls, [(SIO_KEEPALIVE_VALS, (1, 10000, 3000))])

    def test_no_ioctl_anywhere_is_only_a_warning(self):
        wrapper = TransportSocketLike(object())  # neither has ioctl()
        with as_windows(), self.assertLogs("mixer.link", "WARNING"):
            self.assertFalse(link_mod.enable_tcp_keepalive(writer_with(wrapper)))

    def test_connection_survives_on_windows(self):
        """The real path: a real asyncio connection, the platform says win32."""

        async def run():
            accepted = []
            server = await asyncio.start_server(lambda r, w: accepted.append(w), "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            try:
                with as_windows():
                    reader, writer = await link_mod.open_mixer_connection(Config(mixer={"ip": "127.0.0.1", "port": port}))
                self.assertFalse(writer.is_closing())
                writer.close()
                await writer.wait_closed()
            finally:
                for w in accepted:
                    w.close()
                server.close()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
