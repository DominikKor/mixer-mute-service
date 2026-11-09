"""Console solo (headphone monitoring) on inputs and buses.

Recorded live on 2026-11-09: a soloed channel's status carries an extra solo
field (inputs: field 3, buses: field 2). It used to make the whole section
"unknown" (question marks, channel locked). The service must read it and send
it back unchanged with every command, or a mute from the app would switch the
console's solo off.
"""

import asyncio
import struct
import sys
import tempfile
import time
from pathlib import Path

from app.config import Config
from app.controller import Controller
from app.groups import GroupStore
from app.protocol import (
    FrameEvent,
    StreamBuffer,
    build_bus_command,
    build_mute_command,
    parse_hex_stream,
)
from app.safety import check_channel_send
from app.state import ChannelState, StateStore

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def frame(ftype: int, msg_hex: str) -> bytes:
    msg = bytes.fromhex(msg_hex)
    body = bytes([len(msg)]) + msg
    return bytes.fromhex("5a" * 6) + struct.pack(">II", ftype, len(body)) + body


# Exact sections from the live capture (channel 2 "Gebet", bus 132 "PC Input").
CH2_SOLO_MUTED = "0801" "1801" "2001" "2d00c06b44"
CH2_SOLO_UNMUTED = "0801" "1801" "2d00c06b44"
CH2_MUTED = "0801" "2001" "2d00c06b44"
PC_INPUT_SOLO = "088401" "1001" "3d00002244"


class FakeLink:
    def __init__(self):
        self.connected = True
        self.session_epoch = time.monotonic() - 1
        self.sent = []

    def link_stale(self):
        return False

    async def send(self, payload):
        self.sent.append(payload)


async def main():
    print("== parser ==")
    ev = parse_hex_stream(frame(0x6D, CH2_SOLO_MUTED).hex())
    check("soloed muted channel parsed (not unknown)",
          len(ev) == 1 and ev[0].channel == 2 and ev[0].muted is True
          and ev[0].fader_hex == "00c06b44" and ev[0].solo is True)
    ev = parse_hex_stream(frame(0x6D, CH2_SOLO_UNMUTED).hex())
    check("soloed unmuted channel parsed", ev[0].muted is False and ev[0].solo is True)
    ev = parse_hex_stream(frame(0x6D, CH2_MUTED).hex())
    check("channel without solo field -> solo False", ev[0].solo is False and ev[0].muted is True)
    ev = parse_hex_stream(frame(0x6D, "0801" "1802" "2d00c06b44").hex())
    check("invalid solo value -> channel unknown",
          ev[0].muted is None and ev[0].fader_hex is None and ev[0].solo is None)

    sb = StreamBuffer(buses=[(21, "8401")])
    ev = sb.feed(frame(0x71, PC_INPUT_SOLO))
    check("soloed bus parsed", len(ev) == 1 and ev[0].channel == 21 and ev[0].muted is False
          and ev[0].solo is True and ev[0].fader_hex == "00002244")

    print("== commands send solo back ==")
    check("input: soloed mute is byte-identical to the console's own status",
          build_mute_command(2, True, "00c06b44", solo=True).hex()
          == "5a5a5a5a5a5a0000006e000000" + "0b0b" + CH2_SOLO_MUTED)
    check("input: soloed unmute",
          build_mute_command(2, False, "00c06b44", solo=True).hex()
          == "5a5a5a5a5a5a0000006e000000" + "0909" + CH2_SOLO_UNMUTED)
    check("input: without solo unchanged",
          build_mute_command(2, True, "1f14b643").hex()
          == "5a5a5a5a5a5a0000006e0000000909080120012d1f14b643")
    check("channel 1 keeps its id field first, solo after it",
          build_mute_command(1, True, "00007a44", solo=True).hex().endswith("1500abc93d" "1801" "2001" "2d00007a44"))
    bus = build_bus_command("8401", False, "00002244", solo=True).hex()
    check("bus: soloed unmute matches the console's section", bus.endswith("0a0a" + PC_INPUT_SOLO))

    print("== safety ==")
    cfg = Config()
    now = time.monotonic()
    unknown = ChannelState("1f14b643", now, False, now)  # solo never observed
    check("unknown solo blocks sends",
          check_channel_send(2, unknown, cfg, session_epoch=now - 5).reason == "solo state unknown")

    print("== controller keeps the solo ==")
    cfg = Config(mixer={"max_channels": 16},
                 safety={"group_send_delay_ms": 0, "exclusive_group_unmute": False})
    store = StateStore()
    link = FakeLink()
    groups = GroupStore(Path(tempfile.mkdtemp()) / "groups.json", cfg.valid_channels(), {"g": [2, 3]})
    ctrl = Controller(cfg, store, link, groups)
    for e in parse_hex_stream(frame(0x6D, CH2_SOLO_UNMUTED).hex()):
        await store.apply_event(e)
    await store.apply_event(FrameEvent(3, False, "1f14b643", solo=False))
    result = await ctrl.channel_set(2, True)
    check("mute on a soloed channel is allowed", result["ok"] is True)
    check("... and sends solo back", link.sent[-1].hex().endswith(CH2_SOLO_MUTED))
    result = await ctrl.channel_set_fader(2, 500)
    check("fader move on a soloed channel keeps solo", result["ok"] and "1801" in link.sent[-1].hex()[30:])
    result = await ctrl.channel_toggle(2)
    check("toggle on a soloed channel keeps solo", result["ok"] and link.sent[-1].hex()[30:].startswith("0801" "1801"))
    link.sent.clear()
    result = await ctrl.group_set("g", True)
    sent = [p.hex()[30:] for p in link.sent]  # after prefix and LL LL
    check("group mute: soloed channel keeps solo, the other gets none",
          result["ok"] and any(s.startswith("0801" "1801") for s in sent)
          and any(s.startswith("0802" "2001") for s in sent))
    cs = await store.get_channel(2)
    check("store still knows the solo after our commands", cs.solo is True)

    print(f"\n{PASS} passed, {FAIL} failed")
    return FAIL == 0


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
