"""tools/fake_mixer.py (used by tools/run_local_test.py for browser checks)
must speak the same frames as the real console: its dump is read by the
service's own parser, and the service's commands change its state."""

import asyncio
import unittest

from app.protocol import SUBSCRIBE_HANDSHAKE_HEX, StreamBuffer, build_bus_command, build_mute_command
from tools.fake_mixer import serve

BUSES = [(17, "8001"), (18, "8301"), (19, "8401"), (20, "ff01")]


async def exchange(payload: bytes) -> dict:
    server, _ = await serve(port=0)
    port = server.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(payload)
        await writer.drain()
        data = await asyncio.wait_for(reader.read(65536), 2)
        while True:
            try:
                data += await asyncio.wait_for(reader.read(65536), 0.2)
            except asyncio.TimeoutError:
                break
    finally:
        writer.close()
        server.close()
    events = StreamBuffer(buses=BUSES, max_input=16).feed(data)
    return {e.channel: (e.muted, e.fader_hex, e.solo) for e in events}


class FakeMixerTests(unittest.TestCase):
    def test_dump_is_readable_and_complete(self):
        state = asyncio.run(exchange(bytes.fromhex(SUBSCRIBE_HANDSHAKE_HEX)))
        self.assertEqual(sorted(state), list(range(1, 21)))
        self.assertTrue(all(v[0] is not None and v[1] is not None for v in state.values()))

    def test_commands_change_the_next_dump(self):
        state = asyncio.run(exchange(
            build_mute_command(4, True, "00003944")
            + build_mute_command(1, False, "00004844", solo=True)
            + build_bus_command("8401", True, "00002244")
            + bytes.fromhex(SUBSCRIBE_HANDSHAKE_HEX)))
        self.assertEqual(state[4], (True, "00003944", False))
        self.assertEqual(state[1], (False, "00004844", True))
        self.assertEqual(state[19], (True, "00002244", False))


if __name__ == "__main__":
    unittest.main()
