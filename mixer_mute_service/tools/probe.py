"""Read-only mixer protocol probe.

Connects to the mixer and dumps every byte it sends, with timestamps, so we can
learn whether the mixer pushes state to an extra TCP client and in what format.

Usage:
    python -m tools.probe                 # passive capture for 12 s
    python -m tools.probe 30              # passive capture for 30 s
    python -m tools.probe 12 --unsafe-raw-send <hex>
                                          # capture, send <hex> once, keep capturing

SAFETY: by default this sends NOTHING. --unsafe-raw-send bypasses every check
of the service (fader freshness, panic mode, ...), so it refuses the frame
types that change the console: 6e (channel commands, which always write the
fader) and 72 (bus commands). It is only meant for experiments such as an
initial-sync request on a console without signal.
"""

import argparse
import asyncio
import struct
import time

from app.config import load_config

_MAGIC = bytes.fromhex("5a5a5a5a5a5a")
# Frame types that write state: 6e = channel mute/fader, 72 = bus mute/fader.
_REFUSED_TYPES = {0x6E: "channel command (writes the fader)", 0x72: "bus command"}


def checked_raw_payload(hex_text: str) -> bytes:
    """The bytes of an --unsafe-raw-send argument; ValueError if refused."""
    data = bytes.fromhex(hex_text)
    if not data:
        raise ValueError("empty payload")
    pos = data.find(_MAGIC)
    while pos != -1:
        if len(data) < pos + len(_MAGIC) + 4:
            raise ValueError("payload ends inside a frame header")
        (ftype,) = struct.unpack_from(">I", data, pos + len(_MAGIC))
        if ftype in _REFUSED_TYPES:
            raise ValueError(f"refusing frame type {ftype:#x}: {_REFUSED_TYPES[ftype]}")
        pos = data.find(_MAGIC, pos + 1)
    return data


async def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("duration", nargs="?", type=float, default=12.0, help="seconds to capture")
    ap.add_argument("--unsafe-raw-send", metavar="HEX",
                    help="send these bytes once after 2 s (frame types 6e and 72 are refused)")
    args = ap.parse_args()

    send = None
    if args.unsafe_raw_send:
        try:
            send = checked_raw_payload(args.unsafe_raw_send)
        except ValueError as exc:
            ap.error(f"--unsafe-raw-send: {exc}")

    cfg = load_config()
    duration = args.duration
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

    if send is not None:
        await asyncio.sleep(min(2.0, duration / 3))
        print(f"[{_t(start)}] UNSAFE TX {len(send)} bytes: {send.hex()}")
        writer.write(send)
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
