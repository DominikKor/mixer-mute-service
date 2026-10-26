"""Direct-mode test with a fake mixer (no real hardware).

Verifies: the client sends the subscribe handshake on connect, resends it as a
keepalive, parses streamed state frames, and can inject a command.
"""

import asyncio
import sys

from app.config import Config
from app.mixer_client import MixerClient
from app.protocol import SUBSCRIBE_HANDSHAKE_HEX
from app.state import StateStore

MIXER_PORT = 16402
HANDSHAKE = bytes.fromhex(SUBSCRIBE_HANDSHAKE_HEX)
STATE_CH9_MUTE = "5a5a5a5a5a5a0000006d0000010009080820012d00805844"

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


async def wait_for(predicate, timeout=2.0):
    end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def main():
    handshakes = []          # times we received the handshake
    injected = bytearray()

    async def fake_mixer(reader, writer):
        sent_state = False
        while True:
            data = await reader.read(4096)
            if not data:
                break
            # count handshakes; everything else is treated as an injected command
            buf = data
            while HANDSHAKE in buf:
                handshakes.append(1)
                i = buf.find(HANDSHAKE)
                buf = buf[:i] + buf[i + len(HANDSHAKE):]
            if buf:
                injected.extend(buf)
            if not sent_state:
                sent_state = True
                writer.write(bytes.fromhex(STATE_CH9_MUTE))  # stream a state frame
                await writer.drain()
        writer.close()

    srv = await asyncio.start_server(fake_mixer, "127.0.0.1", MIXER_PORT)

    cfg = Config(mixer={
        "ip": "127.0.0.1", "port": MIXER_PORT, "mode": "direct",
        "max_channels": 16, "keepalive_seconds": 0.4,
    })
    store = StateStore()
    client = MixerClient(cfg, store)
    client.start()

    await wait_for(lambda: client.connected)
    check("client connected", client.connected)
    await wait_for(lambda: len(handshakes) >= 1)
    check("handshake sent on connect", len(handshakes) >= 1)

    # state streamed by the mixer is parsed into the store
    await wait_for(lambda: store.last_update_at is not None)
    cs = await store.get_channel(9)
    check("streamed state parsed (ch9)", cs is not None and cs.fader_hex == "00805844" and cs.muted is True)

    # keepalive resends the handshake
    await wait_for(lambda: len(handshakes) >= 2, timeout=2.0)
    check("keepalive resends handshake", len(handshakes) >= 2)

    # inject a command -> reaches the mixer over the same connection
    cmd = bytes.fromhex("5a5a5a5a5a5a0000006e000000070708082d00805844")
    await client.send(cmd)
    await wait_for(lambda: cmd in bytes(injected))
    check("injected command reached mixer", cmd in bytes(injected))

    await client.stop()
    srv.close()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
