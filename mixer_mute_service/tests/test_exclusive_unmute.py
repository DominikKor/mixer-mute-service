"""Exclusive unmute (config exclusive_group_unmute): unmuting a group mutes all
other channels; muting a group leaves the others alone. Uses the real
controller + state store with a stub link."""

import asyncio
import sys
import tempfile
import time
from pathlib import Path

from app.config import Config
from app.channel_preferences import ChannelPreferences
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

    def link_stale(self):
        return False

    async def send(self, payload):
        self.sent.append(payload)


async def set_state(store, ch, muted):
    await store.apply_event(FrameEvent(channel=ch, muted=muted, fader_hex="1f14b643",
                                       raw_match="", end_hex_index=0, solo=False))


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
    gs = GroupStore(Path(tempfile.mkdtemp()) / "groups.json", cfg.valid_channels(), {"g": [1, 2]})
    ctrl = Controller(cfg, store, FakeLink(), gs)

    # all four channels live (unmuted) and safe
    for ch in (1, 2, 3, 4):
        await set_state(store, ch, False)

    # UNMUTE group [1,2] -> 1,2 stay/!unmuted, others (3,4) get muted
    await ctrl.group_set("g", False)
    m = await muted_map(store, (1, 2, 3, 4))
    check("exclusive unmute: group channels unmuted", m[1] is False and m[2] is False)
    check("exclusive unmute: other channels muted", m[3] is True and m[4] is True)

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
    check("exclusive unmute: safe other muted", m[3] is True)
    check("exclusive unmute: unsafe other skipped (stays unknown)", m[4] is None)
    check("exclusive unmute: skipped other reported", 4 in res.get("others_skipped", []))

    print("== always-on channels ==")
    preferences = ChannelPreferences(Path(tempfile.mkdtemp()) / "channel_preferences.json")
    await preferences.set_always_on(1, True)
    await preferences.set_always_on(3, True)
    store3 = StateStore()
    ctrl3 = Controller(cfg, store3, FakeLink(), gs, preferences)
    for ch in (1, 2, 3, 4):
        await set_state(store3, ch, False)
    result = await ctrl3.group_set("g", True)
    muted = await muted_map(store3, (1, 2, 3, 4))
    check("group mute skips always-on member", muted[1] is False and muted[2] is True)
    check("group mute reports protected member", result.get("protected") == [1])

    await set_state(store3, 1, False)
    await set_state(store3, 2, False)
    toggle_result = await ctrl3.group_toggle("g")
    muted = await muted_map(store3, (1, 2))
    check("group toggle skips always-on member", muted[1] is False and muted[2] is True)
    check("group toggle reports protected member", toggle_result.get("protected") == [1])

    await set_state(store3, 2, False)
    await set_state(store3, 4, False)
    exclusive_result = await ctrl3.group_set("g", False)
    muted = await muted_map(store3, (1, 2, 3, 4))
    check("exclusive unmute does not mute always-on other", muted[3] is False and muted[4] is True)
    check("exclusive unmute reports protected other", exclusive_result.get("protected_others") == [3])

    direct = await ctrl3.channel_set(1, True)
    muted = await muted_map(store3, (1,))
    check("direct channel mute remains available", direct["ok"] and muted[1] is True)

    store6 = StateStore()
    ctrl6 = Controller(cfg, store6, FakeLink(), gs, preferences)
    for ch, is_muted in ((1, False), (2, True), (3, False), (4, True)):
        await set_state(store6, ch, is_muted)
    capture_name = f"captured-{time.monotonic_ns()}"
    captured_group = await ctrl6.create_group_from_current_status(capture_name)
    check("capture creates a regular group from unmuted channels",
          captured_group["ok"] and captured_group["channels"] == [1, 3]
                    and await gs.get(capture_name) == [1, 3])
    check("capture preserves observed fader presets",
          captured_group["fader_hex"] == {"1": "1f14b643", "3": "1f14b643"})

    store7 = StateStore()
    ctrl7 = Controller(cfg, store7, FakeLink(), gs, preferences)
    await set_state(store7, 1, False)
    incomplete_group = await ctrl7.create_group_from_current_status("incomplete")
    check("capture rejects unknown muted channel status",
          not incomplete_group["ok"] and incomplete_group["reason"] == "cannot capture current channel state"
          and await gs.get("incomplete") is None)

    print("== main L/R output (id 255) ==")
    from app.protocol import build_bus_command
    main_cfg = Config(
        mixer={"max_channels": 4, "buses": [{"channel": 5, "name": "Saal L/R MIX OUTPUT", "id": "ff01"}]},
        safety={"freshness_mode": "latest", "exclusive_group_unmute": True, "group_send_delay_ms": 0},
    )
    main_groups = GroupStore(Path(tempfile.mkdtemp()) / "groups.json", main_cfg.valid_channels(),
                             {"g": [1, 2], "with_main": [1, 5]})
    store8 = StateStore()
    link8 = FakeLink()
    ctrl8 = Controller(main_cfg, store8, link8, main_groups)
    for ch in (1, 2, 3, 4, 5):
        await set_state(store8, ch, False)
    solo = await ctrl8.group_set("g", False)
    muted = await muted_map(store8, (3, 4, 5))
    check("solo mutes the other inputs but never the main output",
          muted[3] is True and muted[4] is True and muted[5] is False)
    check("solo reports the main output as protected", solo.get("protected_others") == [5])

    link8.sent.clear()
    result = await ctrl8.group_set("with_main", True)
    muted = await muted_map(store8, (1, 5))
    check("group mute leaves a main-output member alone", muted[1] is True and muted[5] is False)
    check("group mute reports it protected", result.get("protected") == [5] and len(link8.sent) == 1)
    await set_state(store8, 5, True)
    result = await ctrl8.group_set("with_main", False)
    muted = await muted_map(store8, (1, 5))
    check("group unmute leaves it alone too", muted[1] is False and muted[5] is True
          and result.get("protected") == [5])
    await set_state(store8, 5, False)
    result = await ctrl8.group_toggle("with_main")
    muted = await muted_map(store8, (1, 5))
    check("group toggle decides and switches without the main output",
          result.get("target_muted") is True and muted[1] is True and muted[5] is False)

    for ch in (1, 2, 3, 4, 5):
        await set_state(store8, ch, False)
    captured = await ctrl8.create_group_from_current_status(f"main-{time.monotonic_ns()}")
    check("group from current status leaves out the main output",
          captured["ok"] and captured["channels"] == [1, 2, 3, 4])

    link8.sent.clear()
    direct = await ctrl8.channel_set(5, True)
    muted = await muted_map(store8, (5,))
    check("main output mutes through its own channel", direct["ok"] and muted[5] is True)
    check("as a bus command with the observed fader",
          link8.sent == [build_bus_command("ff01", True, "1f14b643")])
    direct = await ctrl8.channel_set(5, False)
    check("and unmutes again", direct["ok"] and link8.sent[-1] == build_bus_command("ff01", False, "1f14b643"))

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
