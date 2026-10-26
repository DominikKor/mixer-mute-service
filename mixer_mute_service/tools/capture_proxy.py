"""Passive capture proxy: relay app <-> mixer traffic unchanged and log it.

Point the mixer app's IP address at this host; every byte is forwarded to the
real mixer as-is and logged in both directions, together with a decoded view of
each frame. Used to reverse-engineer what the app sends when a fader moves.

SAFETY: this tool never originates any bytes. It has no HTTP API, no injection
path and does not modify relayed data -- it only forwards what the app and the
mixer send to each other.

Usage:
    python -m tools.capture_proxy                      # listen 127.0.0.1:6400
    python -m tools.capture_proxy --listen-host 0.0.0.0  # e.g. for an iPad
    python -m tools.capture_proxy --mixer-ip 192.168.188.30 --out captures

Writes captures/capture-<timestamp>.log (raw chunks + decoded frames, one line
each) and stdout gets the decoded frames. Add markers to the log while it runs:
    echo "$(date '+%H:%M:%S.000') MARK ch3 fader up" >> captures/capture-....log

Frame layout as inferred from captures (not officially documented):
    5a5a5a5a5a5a | type u32 BE | length u32 BE | varint msg_len | protobuf msg
Status frames (6d/71) are padded with zeros; the decoder skips to the next
magic after each message, so padding never desynchronises it.
"""

from __future__ import annotations

import argparse
import asyncio
import datetime
import struct
import sys
from pathlib import Path
from typing import List, Optional, Tuple

from app.config import load_config

MAGIC = bytes.fromhex("5a5a5a5a5a5a")
HEADER_LEN = 14  # magic(6) + type(4) + length(4)


def _ts() -> str:
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def _varint(data: bytes, pos: int) -> Tuple[Optional[int], int]:
    """Decode a varint at pos. Returns (value, new_pos) or (None, pos) if incomplete."""
    value = shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")
    return None, pos


def decode_protobuf(msg: bytes) -> List[str]:
    """Best-effort protobuf field decode into compact strings.

    fixed32 fields are shown as hex plus little-endian float (the fader format).
    """
    out: List[str] = []
    pos = 0
    while pos < len(msg):
        key, pos = _varint(msg, pos)
        if key is None:
            raise ValueError("truncated key")
        field, wire = key >> 3, key & 7
        if wire == 0:
            val, pos = _varint(msg, pos)
            if val is None:
                raise ValueError("truncated varint")
            out.append(f"f{field}={val}")
        elif wire == 5:
            raw = msg[pos:pos + 4]
            if len(raw) < 4:
                raise ValueError("truncated fixed32")
            pos += 4
            f = struct.unpack("<f", raw)[0]
            out.append(f"f{field}={raw.hex()}({f:.6g})")
        elif wire == 1:
            raw = msg[pos:pos + 8]
            if len(raw) < 8:
                raise ValueError("truncated fixed64")
            pos += 8
            out.append(f"f{field}=x64:{raw.hex()}")
        elif wire == 2:
            n, pos = _varint(msg, pos)
            if n is None or pos + n > len(msg):
                raise ValueError("truncated bytes")
            sub = msg[pos:pos + n]
            pos += n
            try:
                inner = decode_protobuf(sub) if sub else []
                out.append(f"f{field}={{{' '.join(inner)}}}")
            except ValueError:
                out.append(f"f{field}=bytes:{sub.hex()}")
        else:
            raise ValueError(f"unsupported wire type {wire}")
    return out


class FrameDecoder:
    """Splits one direction's byte stream into frames for display.

    Purely observational: it works on a copy of the relayed bytes.
    """

    def __init__(self, max_bytes: int = 1 << 20) -> None:
        self._buf = bytearray()
        self._max = max_bytes

    def feed(self, data: bytes) -> List[str]:
        self._buf.extend(data)
        lines: List[str] = []
        while True:
            start = self._buf.find(MAGIC)
            if start == -1:
                # keep a tail in case a magic is split across reads
                del self._buf[: max(0, len(self._buf) - (len(MAGIC) - 1))]
                break
            if start:
                junk = bytes(self._buf[:start])
                if junk.strip(b"\x00"):
                    lines.append(f"?? {len(junk)} unframed bytes: {junk.hex()}")
                del self._buf[:start]
            if len(self._buf) < HEADER_LEN:
                break
            ftype, flen = struct.unpack(">II", self._buf[6:HEADER_LEN])
            if flen == 0:
                lines.append(f"type={ftype:02x} len=0")
                del self._buf[:HEADER_LEN]
                continue
            try:
                mlen, mpos = _varint(self._buf, HEADER_LEN)
            except ValueError:
                mlen, mpos = None, HEADER_LEN
            if mlen is None:
                if len(self._buf) > HEADER_LEN + 10:  # not a varint: show raw
                    lines.append(f"type={ftype:02x} len={flen} undecodable")
                    del self._buf[:len(MAGIC)]
                    continue
                break
            if len(self._buf) < mpos + mlen:
                if len(self._buf) > self._max:
                    lines.append(f"type={ftype:02x} len={flen} msg_len={mlen} too large, dropped")
                    del self._buf[:len(MAGIC)]
                    continue
                break
            msg = bytes(self._buf[mpos:mpos + mlen])
            try:
                fields = " ".join(decode_protobuf(msg))
            except ValueError as exc:
                fields = f"<undecodable: {exc}>"
            lines.append(f"type={ftype:02x} len={flen} msg={msg.hex()} | {fields}")
            # Drop the message; trailing padding is skipped by the magic search.
            del self._buf[: mpos + mlen]
        return lines


class Capture:
    def __init__(self, mixer_ip: str, mixer_port: int, log_path: Path) -> None:
        self._mixer_ip = mixer_ip
        self._mixer_port = mixer_port
        self._log = log_path.open("a", buffering=1)  # line buffered
        self._sessions = 0

    def write(self, line: str, echo: bool = True) -> None:
        self._log.write(line + "\n")
        if echo:
            print(line, flush=True)

    async def handle(self, app_reader: asyncio.StreamReader, app_writer: asyncio.StreamWriter) -> None:
        self._sessions += 1
        sid = self._sessions
        peer = app_writer.get_extra_info("peername")
        self.write(f"{_ts()} S{sid} app connected from {peer}; connecting to mixer "
                   f"{self._mixer_ip}:{self._mixer_port}")
        try:
            mixer_reader, mixer_writer = await asyncio.wait_for(
                asyncio.open_connection(self._mixer_ip, self._mixer_port), timeout=5
            )
        except Exception as exc:  # noqa: BLE001
            self.write(f"{_ts()} S{sid} cannot reach mixer: {exc!r}")
            app_writer.close()
            return
        self.write(f"{_ts()} S{sid} session established")

        up = asyncio.create_task(self._pump(app_reader, mixer_writer, f"S{sid} app->mixer"))
        down = asyncio.create_task(self._pump(mixer_reader, app_writer, f"S{sid} mixer->app"))
        try:
            done, _ = await asyncio.wait({up, down}, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.exception() is not None:
                    self.write(f"{_ts()} S{sid} relay error: {t.exception()!r}")
        finally:
            for t in (up, down):
                t.cancel()
            for w in (mixer_writer, app_writer):
                try:
                    w.close()
                except Exception:  # noqa: BLE001
                    pass
            self.write(f"{_ts()} S{sid} session closed")

    async def _pump(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, label: str) -> None:
        decoder = FrameDecoder()
        while True:
            data = await reader.read(8192)
            if data == b"":
                self.write(f"{_ts()} {label} EOF")
                return
            # Forward first, unchanged, then log.
            writer.write(data)
            await writer.drain()
            ts = _ts()
            self.write(f"{ts} {label} RAW {len(data)}: {data.hex()}", echo=False)
            for line in decoder.feed(data):
                self.write(f"{ts} {label} FRM {line}")


async def main() -> None:
    cfg = load_config()
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--mixer-ip", default=cfg.mixer.ip)
    ap.add_argument("--mixer-port", type=int, default=cfg.mixer.port)
    ap.add_argument("--listen-host", default="127.0.0.1")
    ap.add_argument("--listen-port", type=int, default=6400)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent.parent / "captures"))
    args = ap.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    log_path = out / f"capture-{datetime.datetime.now():%Y%m%d-%H%M%S}.log"
    cap = Capture(args.mixer_ip, args.mixer_port, log_path)

    server = await asyncio.start_server(cap.handle, args.listen_host, args.listen_port)
    cap.write(f"{_ts()} listening on {args.listen_host}:{args.listen_port} -> mixer "
              f"{args.mixer_ip}:{args.mixer_port}; log: {log_path}")
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        sys.exit(0)
