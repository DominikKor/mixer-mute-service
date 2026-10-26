"""Capture proxy test with a fake mixer and fake app (no real hardware).

Verifies: bytes relay byte-exact both ways (even when frames are split across
writes), the proxy sends nothing of its own, and frames are decoded into the log.
"""

import asyncio
import sys
import tempfile
from pathlib import Path

from tools.capture_proxy import Capture, FrameDecoder

PROXY_PORT = 16410
MIXER_PORT = 16411

HANDSHAKE = "5a5a5a5a5a5a0000000100000000"
APP_MUTE_CH2 = "5a5a5a5a5a5a0000006e0000000909080120012d1f14b643"
APP_UNMUTE_CH1 = "5a5a5a5a5a5a0000006e0000000a0a1500abc93d2d00007a44"
STATUS_CH2 = "5a5a5a5a5a5a0000006d000001000708012d00005143" + "00" * 240
STATUS_ECHO = "5a5a5a5a5a5a00000071000001000a08800118013d00009e43" + "00" * 230

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
    print("== decoder ==")
    d = FrameDecoder()
    stream = bytes.fromhex(HANDSHAKE + APP_MUTE_CH2 + STATUS_CH2 + STATUS_ECHO)
    lines = []
    for i in range(0, len(stream), 7):  # feed in awkward 7-byte pieces
        lines += d.feed(stream[i:i + 7])
    check("4 frames decoded from split stream", len(lines) == 4)
    check("handshake decoded", lines[0] == "type=01 len=0")
    check("ch2 mute fields", lines[1].endswith("f1=1 f4=1 f5=1f14b643(364.157)"))
    check("6d status padding skipped", lines[2].endswith("f1=1 f5=00005143(209)"))
    check("bus status fields", lines[3].endswith("f1=128 f3=1 f7=00009e43(316)"))

    print("== relay ==")
    mixer_rx = bytearray()
    app_rx = bytearray()

    async def fake_mixer(reader, writer):
        first = True
        while True:
            data = await reader.read(4096)
            if not data:
                break
            mixer_rx.extend(data)
            if first:
                first = False
                status = bytes.fromhex(STATUS_CH2 + STATUS_ECHO)
                writer.write(status[:100])  # split mid-frame
                await writer.drain()
                await asyncio.sleep(0.05)
                writer.write(status[100:])
                await writer.drain()
        writer.close()

    mixer_srv = await asyncio.start_server(fake_mixer, "127.0.0.1", MIXER_PORT)
    log_path = Path(tempfile.mkdtemp(prefix="capture_test_")) / "capture.log"
    cap = Capture("127.0.0.1", MIXER_PORT, log_path)
    proxy_srv = await asyncio.start_server(cap.handle, "127.0.0.1", PROXY_PORT)

    reader, writer = await asyncio.open_connection("127.0.0.1", PROXY_PORT)
    sent = bytes.fromhex(HANDSHAKE + APP_MUTE_CH2 + APP_UNMUTE_CH1)
    writer.write(sent[:20])
    await writer.drain()
    await asyncio.sleep(0.05)
    writer.write(sent[20:])
    await writer.drain()

    expected_down = bytes.fromhex(STATUS_CH2 + STATUS_ECHO)

    async def read_app():
        while len(app_rx) < len(expected_down):
            data = await reader.read(4096)
            if not data:
                return
            app_rx.extend(data)

    await asyncio.wait_for(read_app(), timeout=2)
    await wait_for(lambda: len(mixer_rx) >= len(sent))
    check("app->mixer relayed byte-exact (nothing added)", bytes(mixer_rx) == sent)
    check("mixer->app relayed byte-exact (nothing added)", bytes(app_rx) == expected_down)

    writer.close()
    await wait_for(lambda: "session closed" in log_path.read_text())
    log = log_path.read_text()
    check("log has raw chunks", "app->mixer RAW" in log and "mixer->app RAW" in log)
    check("log has decoded ch1 unmute", "f2=00abc93d" in log and "f5=00007a44(1000)" in log)
    check("log has decoded echo status", "f1=128 f3=1 f7=00009e43(316)" in log)

    proxy_srv.close()
    mixer_srv.close()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
