"""Read-only mixer protocol probe.

Connects to the mixer and dumps every byte it sends, with timestamps, so we can
learn whether the mixer pushes state to an extra TCP client and in what format.

Usage:
    python -m tools.probe                 # passive capture for 12s
    python -m tools.probe 30              # passive capture for 30s
    python -m tools.probe 12 <hexpayload> # capture, then send hexpayload, keep capturing

SAFETY: by default this sends NOTHING. A payload is only sent if you pass one
explicitly on the command line. Never pass a mute/unmute command here.
"""

import asyncio
import sys
import time

from app.config import load_config


async def main():
    cfg = load_config()
    duration = float(sys.argv[1]) if len(sys.argv) > 1 else 12.0
    send_hex = sys.argv[2] if len(sys.argv) > 2 else None

    print(f"connecting to {cfg.mixer.ip}:{cfg.mixer.port} ...")
    reader, writer = await asyncio.open_connection(cfg.mixer.ip, cfg.mixer.port)
    print("connected. capturing (read-only)...")

    start = time.monotonic()
    total = 0

    async def reads():
        nonlocal total
        while True:
            data = await reader.read(8192)
            if data == b"":
                print(f"[{_t(start)}] EOF (mixer closed connection)")
                return
            total += len(data)
            print(f"[{_t(start)}] RX {len(data)} bytes: {data.hex()}")

    task = asyncio.create_task(reads())

    if send_hex:
        await asyncio.sleep(min(2.0, duration / 3))
        print(f"[{_t(start)}] TX {len(send_hex)//2} bytes: {send_hex}")
        writer.write(bytes.fromhex(send_hex))
        await writer.drain()

    try:
        await asyncio.wait_for(task, timeout=duration)
    except asyncio.TimeoutError:
        pass

    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    print(f"done. total received: {total} bytes over {duration:.0f}s")


def _t(start):
    return f"{time.monotonic() - start:6.2f}s"


if __name__ == "__main__":
    asyncio.run(main())
