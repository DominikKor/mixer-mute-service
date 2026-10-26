"""End-to-end proxy test with a fake mixer and fake iPad (no real hardware).

Verifies: bytes relay both ways, frames in BOTH directions update state, and a
command injected via ProxyServer.send() reaches the upstream mixer.
"""

import asyncio
import sys

from app.config import Config
from app.proxy import ProxyServer
from app.safety import check_channel_send
from app.state import StateStore

PROXY_PORT = 16400
MIXER_PORT = 16401

IPAD_MUTE_CH2 = "5a5a5a5a5a5a0000006e0000000909080120012d1f14b643"
MIXER_STATUS_UNMUTE_CH2 = "5a5a5a5a5a5a0000006d000001000708012d00005143000000000000"
INJECTED = "5a5a5a5a5a5a0000006e000000070708012d1f14b643"

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


async def wait_for(predicate, timeout=2.0):
    loop_end = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < loop_end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


async def main():
    mixer_rx = bytearray()
    mixer_ready = asyncio.Event()

    async def fake_mixer(reader, writer):
        # On first bytes from the (proxied) iPad, send a status frame back.
        first = True
        while True:
            data = await reader.read(4096)
            if not data:
                break
            mixer_rx.extend(data)
            mixer_ready.set()
            if first:
                first = False
                writer.write(bytes.fromhex(MIXER_STATUS_UNMUTE_CH2))
                await writer.drain()
        writer.close()

    mixer_srv = await asyncio.start_server(fake_mixer, "127.0.0.1", MIXER_PORT)

    cfg = Config(
        mixer={
            "ip": "127.0.0.1", "port": MIXER_PORT,
            "listen_host": "127.0.0.1", "listen_port": PROXY_PORT,
            "mode": "proxy", "max_channels": 8,
        }
    )
    store = StateStore()
    proxy = ProxyServer(cfg, store)
    proxy.start()
    await asyncio.sleep(0.2)  # let the proxy bind

    # Fake iPad connects to the proxy and sends a mute command.
    ipad_reader, ipad_writer = await asyncio.open_connection("127.0.0.1", PROXY_PORT)
    await wait_for(lambda: proxy.connected)
    check("proxy reports session connected", proxy.connected)

    ipad_writer.write(bytes.fromhex(IPAD_MUTE_CH2))
    await ipad_writer.drain()

    # The mixer should receive the forwarded iPad frame.
    await wait_for(lambda: bytes.fromhex(IPAD_MUTE_CH2) in bytes(mixer_rx))
    check("iPad frame relayed to mixer", bytes.fromhex(IPAD_MUTE_CH2) in bytes(mixer_rx))

    # The iPad should receive the mixer's status reply.
    reply = await asyncio.wait_for(ipad_reader.read(4096), timeout=2)
    check("mixer status relayed to iPad", reply.hex().startswith("5a5a5a5a5a5a0000006d"))

    # State should be populated from BOTH directions; ch2 fader known and fresh.
    await wait_for(lambda: True)  # allow parse tasks to run
    cs = await store.get_channel(2)
    check("ch2 state captured from traffic", cs is not None and cs.fader_hex is not None)
    check("ch2 fader is an observed value",
          cs is not None and cs.fader_hex in ("1f14b643", "00005143"))
    check("ch2 mute state known", cs is not None and cs.muted is not None)
    safe = check_channel_send(2, cs, cfg, session_epoch=proxy.session_epoch)
    check("ch2 is safe to send (seen this session)", safe.ok)

    # Injection: a command sent via the proxy must reach the mixer.
    before = len(mixer_rx)
    await proxy.send(bytes.fromhex(INJECTED))
    await wait_for(lambda: bytes.fromhex(INJECTED) in bytes(mixer_rx[before:]) or
                           bytes.fromhex(INJECTED) in bytes(mixer_rx))
    check("injected command reached mixer", bytes.fromhex(INJECTED) in bytes(mixer_rx))

    # Toggle relies on optimistic state updates after a send (the mixer does not
    # echo our injected commands). Two toggles must alternate mute -> unmute.
    from app.config import Config as _C
    from app.controller import Controller
    cfg2 = _C(mixer={"ip": "127.0.0.1", "port": MIXER_PORT, "max_channels": 8})
    from app.groups import GroupStore
    gs = GroupStore(__import__("pathlib").Path("/tmp/_wf_groups_test.json"), 8, {})
    ctrl = Controller(cfg2, store, proxy, gs)
    r1 = await ctrl.channel_toggle(2)
    r2 = await ctrl.channel_toggle(2)
    check("toggle #1 succeeds", r1.get("ok") is True)
    check("toggle alternates target", r1.get("target_muted") != r2.get("target_muted"))

    ipad_writer.close()
    await proxy.stop()
    mixer_srv.close()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
