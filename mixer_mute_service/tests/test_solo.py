"""Solo-on-unmute: unmuting a group mutes all other channels; muting a group
leaves others alone. Uses the real controller + state store with a stub link."""

import asyncio
import sys
import time

from app.config import Config
from app.controller import Controller
from app.groups import GroupStore
from app.protocol import FrameEvent
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
        self.sent.append(payload)


async def set_state(store, ch, muted):
    await store.apply_event(FrameEvent(channel=ch, muted=muted, fader_hex="1f14b643",
                                       raw_match="", end_hex_index=0))


async def muted_map(store, channels):
    out = {}
    for ch in channels:
        cs = await store.get_channel(ch)
        out[ch] = None if cs is None else cs.muted
    return out


async def main():
    cfg = Config(
        mixer={"max_channels": 4},
        safety={"freshness_mode": "latest", "exclusive_group_unmute": True, "group_send_delay_ms": 0},
        groups={"g": {"channels": [1, 2]}},
    )
    store = StateStore()
    gs = GroupStore(__import__("pathlib").Path("/tmp/_solo_groups.json"), cfg.valid_channels(), {"g": [1, 2]})
    ctrl = Controller(cfg, store, FakeLink(), gs)

    # all four channels live (unmuted) and safe
    for ch in (1, 2, 3, 4):
        await set_state(store, ch, False)

    # UNMUTE group [1,2] -> 1,2 stay/!unmuted, others (3,4) get muted
    await ctrl.group_set("g", False)
    m = await muted_map(store, (1, 2, 3, 4))
    check("solo: group channels unmuted", m[1] is False and m[2] is False)
    check("solo: other channels muted", m[3] is True and m[4] is True)

    # reset others to unmuted, then MUTE the group -> others must stay untouched
    await set_state(store, 3, False)
    await set_state(store, 4, False)
    await ctrl.group_set("g", True)
    m = await muted_map(store, (1, 2, 3, 4))
    check("mute group: group channels muted", m[1] is True and m[2] is True)
    check("mute group: others NOT changed", m[3] is False and m[4] is False)

    # an 'other' channel with no safe fader is skipped, not forced
    store2 = StateStore()
    ctrl2 = Controller(cfg, store2, FakeLink(), gs)
    for ch in (1, 2):
        await set_state(store2, ch, False)
    # channel 3 known unmuted+safe, channel 4 has NO data (unsafe)
    await set_state(store2, 3, False)
    res = await ctrl2.group_set("g", False)
    m = await muted_map(store2, (3, 4))
    check("solo: safe other muted", m[3] is True)
    check("solo: unsafe other skipped (stays unknown)", m[4] is None)
    check("solo: skipped other reported", 4 in res.get("others_skipped", []))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
