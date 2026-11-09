"""Regression tests for the second core review (Nov 2026).

Each section names the finding it guards against. No real hardware and no
network beyond localhost.
"""

import asyncio
import json
import logging
import socket
import struct
import sys
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError

import app.channel_preferences as channel_preferences_mod
import app.groups as groups_mod
import app.link as link_mod
from app.channel_preferences import ChannelPreferences
from app.config import Config
from app.controller import Controller
from app.groups import GroupStore
from app.jsonstore import CorruptJSONError, read_json
from app.mixer_client import MixerClient
from app.protocol import (
    FrameEvent,
    StreamBuffer,
    build_mute_command,
    is_valid_fader_hex,
    parse_hex_stream,
)
from app.safety import check_channel_send
from app.state import StateStore

PASS = 0
FAIL = 0
TMP = Path(tempfile.mkdtemp(prefix="mixer_core_round2_"))


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


class Records(logging.Handler):
    """Collects log records of one logger while active."""

    def __init__(self, name):
        super().__init__(logging.DEBUG)
        self.logger = logging.getLogger(name)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def __enter__(self):
        self.logger.addHandler(self)
        self._level = self.logger.level
        self.logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *exc):
        self.logger.removeHandler(self)
        self.logger.setLevel(self._level)

    def has(self, level, text):
        return any(r.levelno >= level and text in r.getMessage() for r in self.records)


# -- frame helpers -------------------------------------------------------------

def frame6e(payload_hex):
    n = len(payload_hex) // 2
    return "5a5a5a5a5a5a0000006e000000" + f"{n + 1:02x}{n:02x}" + payload_hex


def varint(n):
    out = b""
    while True:
        b, n = n & 0x7F, n >> 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out


def fv(f, n):
    return varint(f << 3) + varint(n)


def ff(f, value):
    return varint(f << 3 | 5) + struct.pack("<f", value)


def fb(f, data):
    return varint(f << 3 | 2) + varint(len(data)) + data


def input_obj(index, section=None):
    """Dump input object: 1 index (omitted for ch1), 2 name, 7 fader section."""
    obj = (fv(1, index) if index else b"") + fb(2, b"In")
    return obj + (fb(7, section) if section is not None else b"")


def fader_section(index, muted=True, value=750.0):
    return (fv(1, index) if index else b"") + (fv(4, 1) if muted else b"") + ff(5, value)


def dump_frame(*input_objects):
    msg = fb(1, b"".join(fb(1, o) for o in input_objects))
    body = varint(len(msg)) + msg
    return bytes.fromhex("5a5a5a5a5a5a00000001") + struct.pack(">I", len(body)) + body


def snapshot(events):
    return [(e.channel, e.muted, e.fader_hex, e.solo) for e in events]


# -- fakes ---------------------------------------------------------------------

class FakeLink:
    def __init__(self):
        self.connected = True
        self.session_epoch = time.monotonic() - 1
        self.stale = False
        self.sent = []

    def start(self):
        pass

    async def stop(self):
        pass

    def link_stale(self):
        return self.stale

    async def send(self, payload):
        self.sent.append(payload.hex())
        await asyncio.sleep(0)  # let a concurrent request run in between


async def observe(store, ch, muted, fader_hex="1f14b643", solo=False):
    await store.apply_event(FrameEvent(ch, muted, fader_hex, solo=solo))


def make_cfg(**safety):
    return Config(
        mixer={"max_channels": 4},
        safety={"exclusive_group_unmute": True, "group_send_delay_ms": 0, **safety},
    )


async def muted(store, *channels):
    return [(await store.get_channel(c)).muted if await store.get_channel(c) else None
            for c in channels]


# -- C1: untrusted frames make a known channel unknown -------------------------

async def c1_untrusted_frames():
    print("== C1 untrusted report on a known channel -> channel unknown ==")
    cfg = make_cfg()
    store = StateStore()
    await store.apply_events(parse_hex_stream(frame6e("080120012d1f14b643")))
    check("clean frame: ch2 known", check_channel_send(2, await store.get_channel(2), cfg,
                                                       session_epoch=time.monotonic() - 5).ok)
    with Records("mixer.protocol") as rec:
        ev = parse_hex_stream(frame6e("0801300120012d00007a44"))  # field 6 unknown
    check("unknown field -> unknown event for ch2", snapshot(ev) == [(2, None, None, None)])
    check("unknown field logged as WARNING with the hex",
          rec.has(logging.WARNING, "0801300120012d00007a44"))
    await store.apply_events(ev)
    cs = await store.get_channel(2)
    check("store forgets ch2's old values", cs.fader_hex is None and cs.muted is None and cs.solo is None)
    check("sends to ch2 now blocked", not check_channel_send(2, cs, cfg, session_epoch=0).ok)

    dup = parse_hex_stream(frame6e("080120012001" "2d1f14b643"))
    check("duplicate field -> ch2 unknown", snapshot(dup) == [(2, None, None, None)])
    bad_mute = parse_hex_stream(frame6e("080120022d1f14b643"))
    check("mute value 2 -> ch2 unknown", snapshot(bad_mute) == [(2, None, None, None)])
    noise = parse_hex_stream(frame6e("9901"))
    check("frame naming no channel -> nothing", noise == [])

    print("== C1 dump input objects ==")
    good = StreamBuffer(max_input=16).feed(dump_frame(input_obj(1, fader_section(1))))
    check("clean dump object parses", snapshot(good) == [(2, True, "00803b44", False)])
    with Records("mixer.protocol") as rec:
        missing = StreamBuffer(max_input=16).feed(dump_frame(input_obj(1)))
    check("object without fader section -> ch2 unknown", snapshot(missing) == [(2, None, None, None)])
    check("missing section logged", rec.has(logging.WARNING, "channel 2"))
    mismatch = StreamBuffer(max_input=16).feed(dump_frame(input_obj(1, fader_section(2))))
    check("section naming ch3 inside ch2's object -> ch2 unknown, ch3 untouched",
          snapshot(mismatch) == [(2, None, None, None)])
    extra = StreamBuffer(max_input=16).feed(dump_frame(input_obj(1, fader_section(1) + fv(9, 1))))
    check("section with unknown field -> ch2 unknown", snapshot(extra) == [(2, None, None, None)])
    two = StreamBuffer(max_input=16).feed(
        dump_frame(input_obj(1, fader_section(1)) + fb(7, fader_section(1))))
    check("object with two sections -> ch2 unknown", snapshot(two) == [(2, None, None, None)])


# -- C2: one group command at a time --------------------------------------------

async def c2_group_lock():
    print("== C2 concurrent group commands are serialized ==")
    cfg = make_cfg()
    store = StateStore()
    link = FakeLink()
    gs = GroupStore(TMP / "c2.json", cfg.valid_channels(), {"a": [1, 2], "b": [3, 4]})
    ctl = Controller(cfg, store, link, gs)
    for ch in (1, 2, 3, 4):
        await observe(store, ch, True)
    ra, rb = await asyncio.wait_for(
        asyncio.gather(ctl.group_set("a", False), ctl.group_set("b", False)), timeout=3)
    check("both group unmutes ok", ra["ok"] and rb["ok"])
    check("last one wins: b up, a muted", await muted(store, 1, 2, 3, 4) == [True, True, False, False])
    # Each command's sends form one block: a's unmutes (its exclusive mutes of
    # 3/4 are skipped, already muted), then b's unmutes, then b mutes 1 and 2.
    order = [r["channel"] for r in ra["results"]] + [r["channel"] for r in rb["results"]] \
        + [r["channel"] for r in rb.get("muted_others", [])]
    check("sends not interleaved", len(link.sent) == len(order) == 6 and order == [1, 2, 3, 4, 1, 2])

    print("== C2 concurrent toggles decide one after the other ==")
    for ch in (1, 2, 3, 4):
        await observe(store, ch, True)
    t1, t2 = await asyncio.wait_for(
        asyncio.gather(ctl.group_toggle("a"), ctl.group_toggle("a")), timeout=3)
    check("first toggle unmutes, second mutes again",
          t1.get("target_muted") is False and t2.get("target_muted") is True)
    check("group a muted in the end", await muted(store, 1, 2) == [True, True])

    print("== C2 group and single-channel commands do not deadlock ==")
    res = await asyncio.wait_for(asyncio.gather(
        ctl.group_set("a", False), ctl.channel_set(3, False), ctl.channel_toggle(4),
        ctl.group_toggle("b")), timeout=3)
    check("all four finished", len(res) == 4)


# -- H1: an empty or partial result never counts as "group up" -------------------

async def h1_partial_groups():
    print("== H1 partial groups ==")
    cfg = make_cfg(allow_partial_groups=True)
    store = StateStore()
    link = FakeLink()
    gs = GroupStore(TMP / "h1.json", cfg.valid_channels(), {"g": [1, 2]})
    ctl = Controller(cfg, store, link, gs)
    await observe(store, 3, False)
    await observe(store, 4, False)
    res = await ctl.group_set("g", False)  # 1 and 2 have no state at all
    check("all members unsafe -> ok False", res["ok"] is False and res["skipped"] == [1, 2])
    check("nothing sent, others not muted", link.sent == [] and await muted(store, 3, 4) == [False, False])
    await observe(store, 2, True)
    res = await ctl.group_set("g", False)  # 1 still unknown
    check("one member unsafe -> ok False", res["ok"] is False and res["skipped"] == [1])
    check("others not muted around a half-up group", await muted(store, 3, 4) == [False, False])


# -- M1: link liveness from readable dumps only ----------------------------------

def m1_liveness():
    print("== M1 direct link: only a readable dump proves the link alive ==")
    cfg = Config(mixer={"mode": "direct", "max_channels": 8, "keepalive_seconds": 10})
    client = MixerClient(cfg, StateStore())
    client.connected = True
    client.session_epoch = time.monotonic() - 100
    client.last_dump_ok_at = time.monotonic() - 30
    check("no dump for 30 s -> stale", client.link_stale())
    buf = client._new_buffer()
    no_inputs = dump_frame()  # decodes, but holds no input channels
    buf.feed(no_inputs)
    check("dump without inputs does not refresh", client.link_stale())
    buf.feed(bytes.fromhex("5a5a5a5a5a5a00000001") + struct.pack(">I", 3) + b"\x02\xff\xff")
    check("undecodable dump does not refresh", client.link_stale())
    buf.feed(dump_frame(input_obj(1, fader_section(1))))
    check("readable dump refreshes", not client.link_stale())


# -- M2 / L8: keepalive and connect timeout ----------------------------------------

async def m2_l8_connection():
    print("== M2 TCP keepalive on the mixer connection ==")
    srv = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    ok = link_mod.enable_tcp_keepalive(writer)
    sock = writer.get_extra_info("socket")
    check("keepalive enabled", ok and sock.getsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE) != 0)
    writer.close()
    srv.close()

    print("== L8 connect timeout ==")
    real_open, real_timeout = link_mod.asyncio.open_connection, link_mod.CONNECT_TIMEOUT_SECONDS

    async def hang(*args, **kwargs):
        await asyncio.sleep(60)

    link_mod.asyncio.open_connection = hang
    link_mod.CONNECT_TIMEOUT_SECONDS = 0.1
    try:
        cfg = Config(mixer={"ip": "127.0.0.1", "port": 9})
        start = time.monotonic()
        try:
            await link_mod.open_mixer_connection(cfg)
            timed_out = False
        except asyncio.TimeoutError:
            timed_out = True
        check("unanswered connect gives up after the timeout",
              timed_out and time.monotonic() - start < 1.0)
    finally:
        link_mod.asyncio.open_connection = real_open
        link_mod.CONNECT_TIMEOUT_SECONDS = real_timeout


# -- M4: strict fader hex ----------------------------------------------------------

def m4_fader_hex():
    print("== M4 fader hex is exactly 8 hex digits ==")
    check("'1f 14 b6' rejected", not is_valid_fader_hex("1f 14 b6"))
    check("'1f14b64g' rejected", not is_valid_fader_hex("1f14b64g"))
    check("'1f14b643' accepted", is_valid_fader_hex("1f14b643"))
    try:
        build_mute_command(2, True, "1f 14 b6")
        built = True
    except ValueError:
        built = False
    check("builder refuses it", not built)


# -- M5: panic is logged and survives a restart ----------------------------------

async def m5_panic():
    print("== M5 panic persisted and logged ==")
    cfg = make_cfg()
    path = TMP / "panic_state.json"
    gs = GroupStore(TMP / "m5.json", cfg.valid_channels(), {})
    store = StateStore()
    await observe(store, 2, False)
    ctl = Controller(cfg, store, FakeLink(), gs, panic_path=path)
    check("enabled by default (no file)", ctl.enabled is True)
    with Records("mixer.controller") as rec:
        ctl.enabled = False
    check("disable logged at WARNING", rec.has(logging.WARNING, "PANIC sending DISABLED"))
    check("saved to disk", json.loads(path.read_text()) == {"enabled": False})
    restarted = Controller(cfg, store, FakeLink(), gs, panic_path=path)
    check("still disabled after a restart", restarted.enabled is False)
    res = await restarted.channel_set(2, True)
    check("sends blocked after the restart", res["reason"] == "service disabled (panic)")
    restarted.enabled = True
    check("enable saved", Controller(cfg, store, FakeLink(), gs, panic_path=path).enabled is True)
    path.write_text("{broken")
    check("unreadable panic file -> disabled",
          Controller(cfg, store, FakeLink(), gs, panic_path=path).enabled is False)


# -- L1 / L13: config ----------------------------------------------------------------

def l1_l13_config():
    print("== L1 seed group with a bus not known yet ==")
    cfg = Config(mixer={"max_channels": 16}, groups={"band": {"channels": [1, 20]}})
    try:
        cfg.validate_groups()
        loaded = True
    except ValueError:
        loaded = False
    check("channel 20 (later discovered bus) accepted", loaded)
    try:
        Config(groups={"bad": {"channels": [0]}}).validate_groups()
        rejected = False
    except (ValueError, ValidationError):
        rejected = True
    check("channel 0 rejected", rejected)

    print("== L13 age caps ==")
    try:
        Config(safety={"max_fader_age_seconds": -1})
        negative = False
    except ValidationError:
        negative = True
    check("negative age cap rejected", negative)
    with Records("mixer.config") as rec:
        Config(mixer={"mode": "direct", "keepalive_seconds": 10},
               safety={"max_fader_age_seconds": 0.5}).warn_about_settings()
    check("cap shorter than the dump interval warned", rec.has(logging.WARNING, "shorter than the dump"))
    with Records("mixer.config") as rec:
        Config(mixer={"mode": "direct", "keepalive_seconds": 10}).warn_about_settings()
    check("no cap -> no warning", not rec.records)


# -- L2: one "safe" rule for the UI and the send path -------------------------------

async def l2_safe_flag():
    print("== L2 UI safe flag includes a stale link ==")
    cfg = make_cfg()
    store = StateStore()
    link = FakeLink()
    ctl = Controller(cfg, store, link, GroupStore(TMP / "l2.json", cfg.valid_channels(), {}))
    await observe(store, 2, False)
    cs = await store.get_channel(2)
    check("fresh -> safe", ctl.is_send_safe(2, cs))
    link.stale = True
    check("stale link -> not safe", not ctl.is_send_safe(2, cs))
    link.stale = False
    ctl.enabled = False
    check("panic -> not safe", not ctl.is_send_safe(2, cs))
    ctl.enabled = True
    link.connected = False
    check("disconnected -> not safe", not ctl.is_send_safe(2, cs))


# -- L3: an event that does not state solo blocks sends -----------------------------

async def l3_solo_default():
    print("== L3 solo defaults to unknown ==")
    cfg = make_cfg()
    store = StateStore()
    link = FakeLink()
    ctl = Controller(cfg, store, link, GroupStore(TMP / "l3.json", cfg.valid_channels(), {}))
    await store.apply_event(FrameEvent(2, False, "1f14b643"))
    res = await ctl.channel_set(2, True)
    check("event without solo -> send blocked", res["reason"] == "solo state unknown" and link.sent == [])


# -- L4: our own commands are not mixer updates --------------------------------------

async def l4_last_update():
    print("== L4 last_update_at only moves on observed frames ==")
    store = StateStore()
    await observe(store, 2, False)
    seen = store.last_update_at
    await asyncio.sleep(0.01)
    await store.set_muted(2, True)
    await store.set_fader(2, "00803b44")
    check("own commands leave it alone", store.last_update_at == seen)


# -- L5: empty file is corrupt ------------------------------------------------------

def l5_empty_file():
    print("== L5 empty JSON file counts as corrupt ==")
    path = TMP / "empty.json"
    path.write_text("  \n")
    try:
        read_json(path, dict)
        raised = False
    except CorruptJSONError:
        raised = True
    check("raises CorruptJSONError", raised)
    check("backup kept", (TMP / "empty.json.corrupt").exists())


# -- L6: a failed write leaves memory unchanged ---------------------------------------

async def l6_write_failure():
    print("== L6 failed writes do not change memory ==")
    gs = GroupStore(TMP / "l6.json", set(range(1, 9)), {"a": [1]})

    def fail(*args, **kwargs):
        raise OSError("disk full")

    real = groups_mod.write_json_atomic
    groups_mod.write_json_atomic = fail
    try:
        for op in (gs.create("b", [2]), gs.update("a", [3]), gs.rename("a", "c"), gs.delete("a")):
            try:
                await op
            except OSError:
                pass
    finally:
        groups_mod.write_json_atomic = real
    check("groups unchanged in memory", await gs.list() == {"a": [1]})

    prefs = ChannelPreferences(TMP / "l6_prefs.json")
    real = channel_preferences_mod.write_json_atomic
    channel_preferences_mod.write_json_atomic = fail
    try:
        try:
            await prefs.set_always_on(3, True)
        except OSError:
            pass
    finally:
        channel_preferences_mod.write_json_atomic = real
    check("always-on unchanged in memory", await prefs.list_always_on() == set())


# -- L7: unreadable preferences stop the exclusive unmute ------------------------------

async def l7_unreadable_preferences():
    print("== L7 unreadable always-on list ==")
    path = TMP / "l7_prefs.json"
    path.write_text("{not json")
    prefs = ChannelPreferences(path)
    check("marked unreadable", prefs.readable is False)
    cfg = make_cfg()
    store = StateStore()
    link = FakeLink()
    gs = GroupStore(TMP / "l7.json", cfg.valid_channels(), {"g": [1, 2]})
    ctl = Controller(cfg, store, link, gs, prefs)
    for ch in (1, 2):
        await observe(store, ch, True)
    for ch in (3, 4):
        await observe(store, ch, False)
    with Records("mixer.controller") as rec:
        res = await ctl.group_set("g", False)
    check("group itself unmuted", res["ok"] and await muted(store, 1, 2) == [False, False])
    check("others left alone", await muted(store, 3, 4) == [False, False] and "muted_others" not in res)
    check("reason reported and logged as ERROR",
          "unreadable" in res.get("others_skipped_reason", "") and rec.has(logging.ERROR, "NOT muted"))
    await prefs.set_always_on(3, True)
    check("setting a switch makes the list known again", prefs.readable is True)


# -- L11: blocked actions are logged ---------------------------------------------------

async def l11_blocked_logs():
    print("== L11 every refusal is logged ==")
    cfg = make_cfg()
    ctl = Controller(cfg, StateStore(), FakeLink(), GroupStore(TMP / "l11.json", cfg.valid_channels(), {}))
    with Records("mixer.controller") as rec:
        await ctl.channel_set(99, True)
        await ctl.group_set("nope", True)
        await ctl.group_toggle("nope")
        ctl.enabled = False
        await ctl.group_toggle("nope")
    check("invalid channel logged", rec.has(logging.WARNING, "BLOCKED channel=99"))
    check("unknown group logged", rec.has(logging.WARNING, "reason='unknown group'"))
    check("toggle refusal logged", rec.has(logging.WARNING, "action=toggle"))


# -- H2: an optional feature failing does not stop the service -------------------------

def h2_optional_features():
    print("== H2 optional features ==")
    from app.main import _optional

    fake = SimpleNamespace(state=SimpleNamespace(feature_errors={}))

    def broken():
        raise RuntimeError("bad camera entry")

    check("failing feature -> None", _optional(fake, "cameras", broken) is None)
    check("failure recorded", "bad camera entry" in fake.state.feature_errors.get("cameras", ""))
    check("working feature returned", _optional(fake, "songs", lambda: 42) == 42)


async def main():
    await c1_untrusted_frames()
    await c2_group_lock()
    await h1_partial_groups()
    m1_liveness()
    await m2_l8_connection()
    m4_fader_hex()
    await m5_panic()
    l1_l13_config()
    await l2_safe_flag()
    await l3_solo_default()
    await l4_last_update()
    l5_empty_file()
    await l6_write_failure()
    await l7_unreadable_preferences()
    await l11_blocked_logs()
    h2_optional_features()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
