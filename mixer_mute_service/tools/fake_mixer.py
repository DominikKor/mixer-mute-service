"""A stand-in for the DigiLive 16 on 127.0.0.1, for browser checks of the UI.

It speaks the verified protocol (CLAUDE.md, app/protocol.py):
- every subscribe handshake (type 01) is answered with a full state dump:
  inputs 1..16 in the top-level fields 1/2 (fader section in field 7, the
  processing sections in 3/4/5), buses in field 3 (fader section in 6);
- input commands (type 6e) and bus commands (type 72) change its state;
- like the real console it does not echo a client's own commands; the next
  dump (the service asks every 10 s) shows them.

Run alone:  python -m tools.fake_mixer [--port 16500]
tools/run_local_test.py starts it together with the service.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import struct
from dataclasses import dataclass, field
from typing import Dict, Optional

from app.protocol import decode_fields

log = logging.getLogger("fake_mixer")

MAGIC = bytes.fromhex("5a" * 6)
TYPE_DUMP, TYPE_INPUT_CMD, TYPE_BUS_CMD = 0x01, 0x6E, 0x72
CH1_FIXED = bytes.fromhex("00abc93d")  # channel 1's own field 2 (seen on the real console)

# Real processing sections of a channel (gain, HPF, EQ, dynamics), from the
# live dump of 2026-11-09 (tests/test_channel_settings.py).
INPUT_SECTION = bytes.fromhex("08011001" "3500001041" "4501008041" "a501e69c9ec0")
EQ_SECTION = bytes.fromhex("0801" "1d00007a45" "2d00004040" "3500007a44" "4500004040"
                           "4d00004843" "5d00004040" "6500007042" "7500004040" "8001" "04")
DYN_SECTION = bytes.fromhex("0801" "1d000094c2" "25c0fcc741" "2d00001643" "3500004843" "3d000078c2"
                            "4001" "4ddddd27c2" "551438c140" "5d00007042" "6d00008040" "7802")

INPUT_NAMES = ["Kanzel", "Gebet", "Gesang 1", "Gesang 2", "Gitarre", "Keys", "Bass", "Drums",
               "Bühne L", "Bühne R", "Funk 1", "Funk 2", "PC 1", "PC 2", "Übersetzung", "Ambience"]
BUSES = {128: "Hall 1", 131: "Monitor", 132: "PC Input", 255: "Saal L/R MIX OUTPUT"}


def varint(n: int) -> bytes:
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def fv(f: int, v: int) -> bytes:
    return varint(f << 3) + varint(v)


def ff(f: int, v: float) -> bytes:
    return varint(f << 3 | 5) + struct.pack("<f", v)


def fb(f: int, data: bytes) -> bytes:
    return varint(f << 3 | 2) + varint(len(data)) + data


@dataclass
class Strip:
    muted: bool = True
    fader: float = 0.0  # 0..1000; 0 is omitted on the wire (-inf)
    solo: bool = False


@dataclass
class MixerState:
    inputs: Dict[int, Strip] = field(default_factory=dict)
    buses: Dict[int, Strip] = field(default_factory=dict)

    @classmethod
    def default(cls) -> "MixerState":
        state = cls()
        for ch in range(1, 17):
            # A plausible Sunday: speech and band up, the rest down.
            up = ch in (1, 2, 3, 5, 6)
            state.inputs[ch] = Strip(muted=not up, fader=700.0 + ch * 10 if ch != 8 else 0.0)
        for ident in BUSES:
            state.buses[ident] = Strip(muted=ident == 131, fader=750.0 if ident == 255 else 600.0)
        return state

    # -- encoding --------------------------------------------------------------

    def input_section(self, ch: int) -> bytes:
        s = self.inputs[ch]
        index = ch - 1
        out = fv(1, index) if index else b""
        if ch == 1:
            out += varint(2 << 3 | 5) + CH1_FIXED
        out += (fv(3, 1) if s.solo else b"") + (fv(4, 1) if s.muted else b"")
        return out + (ff(5, s.fader) if s.fader > 0 else b"")

    def bus_section(self, ident: int) -> bytes:
        s = self.buses[ident]
        return (fv(1, ident) + (fv(2, 1) if s.solo else b"") + (fv(3, 1) if s.muted else b"")
                + (ff(7, s.fader) if s.fader > 0 else b""))

    def dump_message(self) -> bytes:
        def input_obj(ch: int) -> bytes:
            index = ch - 1
            return ((fv(1, index) if index else b"") + fb(2, INPUT_NAMES[index].encode())
                    + fb(3, INPUT_SECTION) + fb(4, EQ_SECTION) + fb(5, DYN_SECTION)
                    + fb(7, self.input_section(ch)))

        group1 = b"".join(fb(1, input_obj(ch)) for ch in range(1, 9))
        group2 = b"".join(fb(1, input_obj(ch)) for ch in range(9, 17))
        buses = b"".join(fb(1, fv(1, i) + fb(2, n.encode()) + fb(6, self.bus_section(i)))
                         for i, n in BUSES.items())
        return fb(1, group1) + fb(2, group2) + fb(3, buses)

    # -- commands --------------------------------------------------------------

    def apply(self, ftype: int, msg: bytes) -> Optional[str]:
        fields = {f: v for f, _, v in decode_fields(msg) or []}
        if ftype == TYPE_INPUT_CMD:
            ch = fields.get(1, 0) + 1
            strip = self.inputs.get(ch)
        elif ftype == TYPE_BUS_CMD:
            ch = fields.get(1)
            strip = self.buses.get(ch)
        else:
            return None
        if strip is None:
            return f"ignored command for unknown {'input' if ftype == TYPE_INPUT_CMD else 'bus'} {ch}"
        fader_field, mute_field, solo_field = (5, 4, 3) if ftype == TYPE_INPUT_CMD else (7, 3, 2)
        raw = fields.get(fader_field)
        fader = struct.unpack("<f", raw)[0] if isinstance(raw, (bytes, bytearray)) else 0.0
        if not 0.0 <= fader <= 1000.0:
            # The real console has crashed on such values: shout.
            return f"REFUSED fader {fader} outside 0..1000 for {ch}"
        strip.fader, strip.muted, strip.solo = fader, bool(fields.get(mute_field)), bool(fields.get(solo_field))
        return f"{'input' if ftype == TYPE_INPUT_CMD else 'bus'} {ch}: muted={strip.muted} fader={fader:.1f} solo={strip.solo}"


def frame(ftype: int, msg: bytes) -> bytes:
    body = varint(len(msg)) + msg
    return MAGIC + struct.pack(">II", ftype, len(body)) + body


async def serve(host: str = "127.0.0.1", port: int = 16500, state: Optional[MixerState] = None):
    state = state or MixerState.default()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        log.info("client connected")
        buf = b""
        try:
            while data := await reader.read(65536):
                buf += data
                while True:
                    # As app/protocol._scan: the header's length is not used
                    # for framing (a command counts only its message, a dump
                    # also the varint); the varint after the header is.
                    start = buf.find(MAGIC)
                    if start < 0 or len(buf) - start < 14:
                        break
                    ftype, length = struct.unpack(">II", buf[start + 6:start + 14])
                    if length == 0:  # the subscribe handshake: no message at all
                        buf = buf[start + 14:]
                        if ftype == TYPE_DUMP:
                            writer.write(frame(TYPE_DUMP, state.dump_message()))
                            await writer.drain()
                        continue
                    msg_len, shift, pos = 0, 0, start + 14
                    while pos < len(buf):
                        b = buf[pos]
                        pos += 1
                        msg_len |= (b & 0x7F) << shift
                        shift += 7
                        if not b & 0x80:
                            break
                    else:
                        break  # varint not complete yet
                    if len(buf) < pos + msg_len:
                        break
                    msg, buf = buf[pos:pos + msg_len], buf[pos + msg_len:]
                    if ftype == TYPE_DUMP:
                        writer.write(frame(TYPE_DUMP, state.dump_message()))
                        await writer.drain()
                        continue
                    note = state.apply(ftype, msg)
                    if note:
                        log.info(note)
        except ConnectionError:
            pass
        finally:
            log.info("client gone")
            writer.close()

    server = await asyncio.start_server(handle, host, port)
    log.info("fake mixer on %s:%s", host, port)
    return server, state


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--port", type=int, default=16500)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")

    async def run():
        server, _ = await serve(port=args.port)
        async with server:
            await server.serve_forever()

    asyncio.run(run())


if __name__ == "__main__":
    main()
