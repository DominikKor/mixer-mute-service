"""State-dump test with the real dump from a capture (no real hardware).

Verifies: the full state dump (frame type 01) the mixer sends after the
subscribe handshake yields mute + fader for every input channel and every
configured bus, also when split across reads; unconfigured buses and sections
that do not match their object are ignored.

Fixture: the dump frame (header + 16384 bytes incl. zero padding) recorded in
captures/capture-20261026-183621.log, session S3 mixer->app, 19:02:14.087;
zlib-compressed and base64 encoded.
"""

import base64
import hashlib
import struct
import sys
import tempfile
import zlib
from pathlib import Path

from app.buses import BusRegistry
from app.config import load_config
from app.protocol import StreamBuffer, fader_hex_to_value, parse_hex_stream

DUMP_FRAME = zlib.decompress(base64.b64decode("""
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
G8f/yfE/6HMGoQ==
"""))
DUMP_SHA256 = "b77cb3075e45f002b95d62971a3876cc946841b78ab91b9e4330185f129570bf"

BUSES = [(17, "8001"), (18, "8301")]  # Echo/Hall 1 = 128, Monitor = 131

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def state(events):
    """{channel: (muted, fader value)} of the last event per channel."""
    return {e.channel: (e.muted, fader_hex_to_value(e.fader_hex)) for e in events}


# -- tiny protobuf encoder for synthetic dumps --------------------------------

def varint(n):
    out = bytearray()
    while True:
        b, n = n & 0x7F, n >> 7
        out.append(b | (0x80 if n else 0))
        if not n:
            return bytes(out)


def vfield(f, n):
    return varint(f << 3) + varint(n)


def bfield(f, data):
    return varint(f << 3 | 2) + varint(len(data)) + data


def f32field(f, value):
    return varint(f << 3 | 5) + struct.pack("<f", value)


def bus_obj(obj_id, *sections):
    return vfield(1, obj_id) + bfield(2, b"Bus") + b"".join(bfield(6, s) for s in sections)


def dump_frame(*bus_objects):
    msg = bfield(3, b"".join(bfield(1, o) for o in bus_objects))
    body = varint(len(msg)) + msg
    return bytes.fromhex("5a5a5a5a5a5a00000001") + struct.pack(">I", len(body)) + body


def main():
    print("== real dump ==")
    check("fixture intact", hashlib.sha256(DUMP_FRAME).hexdigest() == DUMP_SHA256)
    events = StreamBuffer(buses=BUSES).feed(DUMP_FRAME)
    s = state(events)
    check("one event per channel 1..18", sorted(e.channel for e in events) == list(range(1, 19)))
    check("ch1 unmuted 852.0", s[1] == (False, 852.0))
    check("ch8 muted 0.0 (fader field omitted)", s[8] == (True, 0.0))
    check("ch12 unmuted 787.0", s[12] == (False, 787.0))
    others = [c for c in range(2, 17) if c not in (8, 12)]
    check("ch2-7/9-11/13-16 muted", all(s[c][0] is True for c in others))
    check("all inputs have a real fader value", all(s[c][1] is not None for c in range(1, 17)))
    check("bus 17 (Hall 1, id 128) muted 321.0", s[17] == (True, 321.0))
    check("bus 18 (Monitor, id 131) muted 669.0", s[18] == (True, 669.0))

    print("== split across reads ==")
    buf = StreamBuffer(buses=BUSES)
    split = []
    for i in range(0, len(DUMP_FRAME), 1000):
        split += buf.feed(DUMP_FRAME[i:i + 1000])
    check("same result fed in 1000-byte chunks", state(split) == s and len(split) == 18)
    check("hex parser agrees", state(parse_hex_stream(DUMP_FRAME.hex(), buses=BUSES)) == s)

    print("== bus configuration ==")
    plain = state(StreamBuffer().feed(DUMP_FRAME))
    check("no buses configured -> inputs only", sorted(plain) == list(range(1, 17)))
    hall2 = state(StreamBuffer(buses=[(19, "8101")]).feed(DUMP_FRAME))
    check("other configured bus (Hall 2, id 129) muted 0.0", hall2.get(19) == (True, 0.0))
    check("unconfigured buses ignored", 17 not in hall2 and 18 not in hall2)

    print("== buses discovered automatically (config.yaml pins only 17/18) ==")
    tmp = Path(tempfile.mkdtemp(prefix="buses_test_"))
    cfg = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    check("config pins Echo=17 and Monitor=18 only",
          [(b.channel, b.id) for b in cfg.mixer.buses] == [(17, "8001"), (18, "8301")])
    reg = BusRegistry(cfg, tmp / "buses.json")
    new_channels = []
    reg.on_new_bus(new_channels.append)
    all_buses = state(StreamBuffer(buses=reg.bus_map).feed(DUMP_FRAME))
    expected = {
        17: (True, 321.0),   # Hall 1 / Echo (pinned)
        18: (True, 669.0),   # Monitor (pinned)
        19: (True, 0.0),     # Hall 2
        20: (True, 0.0),     # EarMonitor
        21: (False, 616.0),  # PC Input
        22: (False, 512.0),  # Keller+Kinder
        23: (True, 0.0),     # Bus 7
        24: (True, 0.0),     # Bus 8
    }
    check("8 buses are channels 17..24", sorted(b.channel for b in cfg.mixer.buses) == list(range(17, 25)))
    check("discovered buses get state from the same dump", {c: all_buses.get(c) for c in expected} == expected)
    check("main L/R (id 255) not added", sorted(all_buses) == list(range(1, 25)))
    check("names come from the console",
          {b.channel: b.name for b in cfg.mixer.buses if b.channel > 18}
          == {19: "Hall 2", 20: "EarMonitor", 21: "PC Input", 22: "Keller+Kinder", 23: "Bus 7", 24: "Bus 8"})
    check("listeners told about each new channel", new_channels == list(range(19, 25)))
    check("valid channels now 1..24", cfg.all_channels() == list(range(1, 25)))
    StreamBuffer(buses=reg.bus_map).feed(DUMP_FRAME)
    check("second dump adds nothing", len(cfg.mixer.buses) == 8 and new_channels == list(range(19, 25)))

    # After a restart the persisted numbers are used before any dump arrives.
    cfg2 = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    reg2 = BusRegistry(cfg2, tmp / "buses.json")
    check("restart: buses known before the first dump",
          [(b.channel, b.id) for b in cfg2.mixer.buses] == [(b.channel, b.id) for b in cfg.mixer.buses])
    check("restart: same state from the dump", state(StreamBuffer(buses=reg2.bus_map).feed(DUMP_FRAME)) == all_buses)

    # Config always wins: pinning Hall 2 to 30 must not leave a second number.
    cfg3 = load_config(Path(__file__).resolve().parent.parent / "config.yaml")
    from app.config import BusConfig
    cfg3.mixer.buses.append(BusConfig(channel=30, name="Hall 2", id="8101"))
    BusRegistry(cfg3, tmp / "buses.json")
    check("persisted bus clashing with config is dropped",
          [b.channel for b in cfg3.mixer.buses if b.id == "8101"] == [30])

    print("== untrusted dump sections ==")
    good = vfield(1, 128) + vfield(3, 1) + f32field(7, 321.0)
    check("synthetic bus dump parses",
          state(StreamBuffer(buses=BUSES).feed(dump_frame(bus_obj(128, good)))) == {17: (True, 321.0)})
    wrong_id = vfield(1, 131) + f32field(7, 500.0)
    check("section naming another bus is ignored",
          StreamBuffer(buses=BUSES).feed(dump_frame(bus_obj(128, wrong_id))) == [])
    check("object with two sections is ignored",
          StreamBuffer(buses=BUSES).feed(dump_frame(bus_obj(128, good, good))) == [])
    extra = good + vfield(9, 1)
    check("section with unknown field is ignored",
          StreamBuffer(buses=BUSES).feed(dump_frame(bus_obj(128, extra))) == [])
    nan = vfield(1, 128) + varint(7 << 3 | 5) + bytes.fromhex("0000c07f")
    bad = StreamBuffer(buses=BUSES).feed(dump_frame(bus_obj(128, nan)))
    check("non-position fader -> bus unknown",
          len(bad) == 1 and bad[0].channel == 17 and bad[0].muted is None and bad[0].fader_hex is None)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
