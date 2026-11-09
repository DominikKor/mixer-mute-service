"""Channel settings snapshots per mute group: decoding (field map measured on
the real console on 2026-11-09), capture rules and persistence."""

import asyncio
import struct
import sys
import tempfile
import time
from pathlib import Path

from app.channel_settings import (
    ChannelSettingsCache,
    ChannelSettingsStore,
    SettingsError,
    decode,
)
from app.protocol import BusMap, StreamBuffer, dump_channel_settings

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def varint(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def fv(field, value):            # varint field
    return varint(field << 3) + varint(value)


def ff(field, value):            # float (fixed32) field
    return varint(field << 3 | 5) + struct.pack("<f", value)


def fb(field, data):             # length-delimited field
    return varint(field << 3 | 2) + varint(len(data)) + data


# Real sections of channel 2 ("Gebet") from the live dump.
CH2_INPUT = bytes.fromhex("08011001" "3500001041" "4501008041" "a501e69c9ec0")
CH2_EQ = bytes.fromhex("0801" "1d00007a45" "2d00004040" "3500007a44" "4500004040"
                       "4d00004843" "5d00004040" "6500007042" "7500004040" "8001" "04")
CH2_DYN = bytes.fromhex("0801" "1d000094c2" "25c0fcc741" "2d00001643" "3500004843" "3d000078c2"
                        "4001" "4ddddd27c2" "551438c140" "5d00007042" "6d00008040" "7802")


def input_object(index, input_sec, eq_sec, dyn_sec):
    """A dump input channel object: 1 index, 3 input, 4 EQ, 5 dynamics, 7 fader."""
    fader = (fv(1, index) if index else b"") + fv(4, 1) + ff(5, 750.0)
    return (fv(1, index) if index else b"") + fb(2, b"Name") + fb(3, input_sec) \
        + fb(4, eq_sec) + fb(5, dyn_sec) + fb(7, fader)


def dump_msg(objects):
    return fb(1, b"".join(fb(1, o) for o in objects))


async def main():
    print("== decode (real channel 2) ==")
    s = decode({"input": CH2_INPUT, "eq": CH2_EQ, "dynamics": CH2_DYN})
    check("gain", s["gain_db"] == -5.0)
    check("HPF off at 16 Hz", s["hpf"] == {"on": False, "freq_hz": 16.0})
    check("EQ off, 4 bands sorted by frequency",
          s["eq"]["on"] is False and [b["freq_hz"] for b in s["eq"]["bands"]] == [60, 200, 1000, 4000]
          and all(b["q"] == 3.0 and b["gain_db"] == 0.0 for b in s["eq"]["bands"]))
    check("gate", s["gate"] == {"on": False, "threshold_db": -74.0, "attack_ms": 25.0,
                                "hold_ms": 150.0, "release_ms": 200.0, "range_db": -62.0})
    check("compressor", s["compressor"] == {"on": True, "threshold_db": -42.0, "ratio": 4.0,
                                            "attack_ms": 6.0, "release_ms": 60.0, "makeup_db": 0.0})
    check("unmapped values kept visible", s["other"] == {"input.2": 1, "input.6": 9.0, "eq.16": 4, "dynamics.15": 2})

    print("== decode (values as changed on channel 8) ==")
    s = decode({
        "input": fv(1, 7) + fv(7, 1) + ff(8, 120.0) + ff(20, 12.5),
        "eq": fv(1, 7) + fv(2, 1) + ff(9, 263.65) + ff(10, 12.0) + ff(11, 0.7),
        "dynamics": fv(1, 7) + fv(2, 1) + ff(3, -43.33) + fv(8, 1) + ff(9, -28.0)
                    + ff(10, 11.14) + ff(11, 70.0) + ff(12, 7.0) + ff(13, 5.254),
    })
    check("gain and HPF on", s["gain_db"] == 12.5 and s["hpf"] == {"on": True, "freq_hz": 120.0})
    band = next(b for b in s["eq"]["bands"] if b["freq_hz"] > 100)
    check("EQ band", s["eq"]["on"] and band == {"freq_hz": 264.0, "gain_db": 12.0, "q": 0.7})
    check("gate on", s["gate"]["on"] and s["gate"]["threshold_db"] == -43.3)
    c = s["compressor"]
    check("compressor values", c["on"] and c["threshold_db"] == -28.0 and c["ratio"] == 5.25
          and c["attack_ms"] == 11.1 and c["release_ms"] == 70.0 and c["makeup_db"] == 7.0)

    print("== from a dump frame ==")
    msg = dump_msg([input_object(1, CH2_INPUT, CH2_EQ, CH2_DYN), input_object(20, CH2_INPUT, CH2_EQ, CH2_DYN)])
    sections = dump_channel_settings(msg, max_input=16)
    check("input channels only (index 20 = beyond 16 inputs is dropped)", list(sections) == [2])
    check("sections are the raw bytes", sections[2] == {"input": CH2_INPUT, "eq": CH2_EQ, "dynamics": CH2_DYN})
    cache = ChannelSettingsCache()
    body = varint(len(msg)) + msg
    frame = bytes.fromhex("5a" * 6) + struct.pack(">II", 1, len(body)) + body
    StreamBuffer(buses=BusMap(on_settings=cache.update), max_input=16).feed(frame)
    check("stream parser hands dump settings to the cache", cache.get(2) is not None)

    print("== capture rules ==")
    path = Path(tempfile.mkdtemp()) / "channel_settings.json"
    store = ChannelSettingsStore(path)
    epoch = time.monotonic() - 1
    try:
        await store.capture("Gebet", [2, 5], cache, epoch, 16)
        check("missing channel blocks the whole capture", False)
    except SettingsError as exc:
        check("missing channel blocks the whole capture",
              exc.details == [{"channel": 5, "reason": "no settings from the mixer yet"}])
    check("nothing saved after a refused capture", await store.get("Gebet") is None)
    try:
        await store.capture("Gebet", [2], cache, time.monotonic() + 5, 16)
        check("values from before the current link are refused", False)
    except SettingsError as exc:
        check("values from before the current link are refused",
              exc.details[0]["reason"] == "settings predate the current mixer link")
    snap = await store.capture("Gebet", [2, 21], cache, epoch, 16)
    check("capture saves inputs, skips buses", list(snap["channels"]) == ["2"] and snap["skipped"] == [21])
    check("saved view is decoded", snap["channels"]["2"]["compressor"]["ratio"] == 4.0)

    reloaded = ChannelSettingsStore(path)
    again = await reloaded.get("Gebet")
    check("survives a restart", again is not None and again["channels"]["2"]["gain_db"] == -5.0)
    check("rename moves the snapshot", await reloaded.rename_group("Gebet", "Gebet neu")
          and await reloaded.get("Gebet") is None and await reloaded.get("Gebet neu") is not None)
    check("delete removes it", await reloaded.delete_group("Gebet neu") and await reloaded.get("Gebet neu") is None)

    print(f"\n{PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
