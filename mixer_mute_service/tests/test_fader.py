"""Fader control through the real controller + state store with a stub link.

Verifies: the sent frame keeps the observed mute state and matches the app's
own format, unknown mute state / bad values / panic block the move, and the
store records the commanded value (the mixer does not echo our commands).
"""

import asyncio
import pathlib
import sys
import tempfile
import time

from app.config import Config
from app.controller import Controller
from app.groups import GroupStore
from app.protocol import FrameEvent, fader_hex_to_value, parse_hex_stream
from app.state import StateStore

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


class FakeLink:
    def __init__(self):
        self.connected = True
        self.session_epoch = time.monotonic()
        self.sent = []

    async def send(self, payload):
        self.sent.append(payload.hex())


async def observe(store, ch, muted, fader_hex):
    await store.apply_event(FrameEvent(channel=ch, muted=muted, fader_hex=fader_hex,
                                       raw_match="", end_hex_index=0))


async def main():
    cfg = Config(mixer={"max_channels": 16, "buses": [{"channel": 17, "name": "Echo", "id": "8001"}]})
    store = StateStore()
    link = FakeLink()
    groups = GroupStore(pathlib.Path(tempfile.mkdtemp()) / "groups.json", cfg.valid_channels(), {})
    ctrl = Controller(cfg, store, link, groups)

    print("== fader move keeps mute state ==")
    await observe(store, 8, True, "00000000")  # muted, at -inf
    r = await ctrl.channel_set_fader(8, fader_hex_to_value("f9a2cd42"))  # exact value the app sent
    check("move ok", r.get("ok") is True)
    check("sent exactly what the app sends (muted flag kept)",
          link.sent[-1] == "5a5a5a5a5a5a0000006e00000009" "09080720012df9a2cd42")
    cs = await store.get_channel(8)
    check("store records commanded fader", cs.fader_hex == "f9a2cd42" and cs.muted is True)
    r = await ctrl.channel_set_fader(8, 0)
    check("move to -inf omits fader field", r.get("ok") and link.sent[-1].endswith("040408072001"))

    await observe(store, 12, False, "00c04444")
    r = await ctrl.channel_set_fader(12, 750)
    check("unmuted channel stays unmuted", r.get("ok") and link.sent[-1].endswith("0707080b2d00803b44"))

    await observe(store, 17, True, "00009e43")
    r = await ctrl.channel_set_fader(17, 316)
    check("bus fader uses bus frame", r.get("ok") and link.sent[-1]
          == "5a5a5a5a5a5a000000720000000a0a08800118013d00009e43")

    print("== blocked moves send nothing ==")
    before = len(link.sent)
    r = await ctrl.channel_set_fader(3, 500)
    check("never observed -> blocked", r.get("ok") is False and r["reason"] == "no fader value known")
    await observe(store, 4, None, None)  # an untrusted frame invalidated channel 4
    r = await ctrl.channel_set_fader(4, 500)
    check("invalidated channel -> blocked", r.get("ok") is False)
    for bad in (float("nan"), -1, 1000.1, float("inf")):
        r = await ctrl.channel_set_fader(8, bad)
        check(f"value {bad!r} -> invalid fader value", r.get("reason") == "invalid fader value")
    r = await ctrl.channel_recall_fader(8, "ffffffff")
    check("recall NaN hex -> blocked", r.get("reason") == "invalid fader_hex")
    r = await ctrl.channel_set_fader(99, 500)
    check("invalid channel -> blocked", r.get("reason") == "invalid channel")
    ctrl.enabled = False
    r = await ctrl.channel_set_fader(8, 500)
    check("panic -> blocked", r.get("reason") == "service disabled (panic)")
    ctrl.enabled = True
    link.connected = False
    r = await ctrl.channel_set_fader(8, 500)
    check("no connection -> blocked", r.get("reason") == "no mixer connection")
    link.connected = True
    check("nothing was sent for blocked moves", len(link.sent) == before)

    r = await ctrl.channel_recall_fader(8, "00803b44")
    check("recall valid hex works and keeps mute", r.get("ok") and r["muted"] is True)

    print("== a dump that predates our command must not undo it ==")
    # Minimal dump frame (type 01): channel 8 muted at -inf, i.e. the OLD state.
    old_dump = "5a5a5a5a5a5a" "00000001" "0000000c" "0c" "0a0a4208" "08073a04" "08072001"
    events = parse_hex_stream(old_dump)
    check("dump frame parsed and marked as dump",
          [(e.channel, e.muted, e.fader_hex, e.from_dump) for e in events] == [(8, True, "00000000", True)])
    store2 = StateStore(dump_guard_seconds=0.3)
    ctrl2 = Controller(cfg, store2, link, groups)
    for e in events:
        await store2.apply_event(e)
    r = await ctrl2.channel_set_fader(8, 750)
    check("fader moved to 750", r.get("ok") is True)
    applied = await store2.apply_event(events[0])  # the stale dump arrives right after
    cs = await store2.get_channel(8)
    check("stale dump ignored right after our command", applied is False and cs.fader_hex == "00803b44")
    live = FrameEvent(channel=8, muted=True, fader_hex="0000c843", raw_match="", end_hex_index=0)
    check("live status frame still applied", await store2.apply_event(live) is True
          and (await store2.get_channel(8)).fader_hex == "0000c843")
    check("other channels' dump values still applied",
          await store2.apply_event(FrameEvent(9, True, "00806544", "", 0, from_dump=True)) is True)
    await asyncio.sleep(0.35)
    check("next dump after the guard is applied again", await store2.apply_event(events[0]) is True
          and (await store2.get_channel(8)).fader_hex == "00000000")

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
