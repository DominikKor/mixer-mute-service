"""Regression tests for the safety-critical core (code review, Nov 2026).

Each section names the problem it guards against. No real hardware: fake
mixer / fake app sockets on localhost only.
"""

import asyncio
import json
import sys
import tempfile
import time
from pathlib import Path

from pydantic import ValidationError

from app.buses import BusRegistry
from app.config import Config
from app.controller import Controller
from app.groups import GroupStore
from app.mixer_client import MixerClient
from app.protocol import (
    FrameEvent,
    StreamBuffer,
    fader_value_to_hex,
    parse_hex_stream,
)
from app.proxy import ProxyServer
from app.safety import check_channel_send
from app.state import ChannelState, StateStore

PASS = 0
FAIL = 0

IPAD_MUTE_CH2 = bytes.fromhex("5a5a5a5a5a5a0000006e0000000909080120012d1f14b643")
INJECT_UNMUTE_CH3 = bytes.fromhex("5a5a5a5a5a5a0000006e000000070708022d00005143")
STATUS_MUTE_CH4 = bytes.fromhex("5a5a5a5a5a5a0000006d0000010009080320012d00805844")
STATUS_MUTE_CH9 = bytes.fromhex("5a5a5a5a5a5a0000006d0000010009080820012d00805844")
STATUS_BADMUTE_CH4 = bytes.fromhex("5a5a5a5a5a5a0000006d0000010009080320022d00805844")

TMP = Path(tempfile.mkdtemp(prefix="mixer_core_safety_"))


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


async def wait_for(predicate, timeout=2.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def groups_store(cfg, name="g.json", seed=None):
    return GroupStore(TMP / name, cfg.valid_channels(), seed or {})


class FakeLink:
    def __init__(self, fail_on=None):
        self.connected = True
        self.session_epoch = time.monotonic() - 1
        self.sent = []
        self.fail_on = fail_on  # payload hex fragment that makes send() fail

    def link_stale(self):
        return False

    async def send(self, payload):
        if self.fail_on and self.fail_on in payload.hex():
            raise ConnectionError("simulated write failure")
        self.sent.append(payload.hex())


async def proxy_mid_frame():
    print("== 1/8 proxy: injection never lands inside an app frame ==")
    rx = bytearray()

    async def fake_mixer(r, w):
        while (d := await r.read(4096)):
            rx.extend(d)

    srv = await asyncio.start_server(fake_mixer, "127.0.0.1", 16521)
    cfg = Config(mixer={"mode": "proxy", "ip": "127.0.0.1", "port": 16521,
                        "listen_host": "127.0.0.1", "listen_port": 16520, "max_channels": 8})
    proxy = ProxyServer(cfg, StateStore())
    proxy.start()
    await asyncio.sleep(0.2)
    r_a, w_a = await asyncio.open_connection("127.0.0.1", 16520)
    await wait_for(lambda: proxy.connected)
    epoch_a = proxy.session_epoch

    # App frame split across two TCP segments, injection in between.
    w_a.write(IPAD_MUTE_CH2[:20])
    await w_a.drain()
    await asyncio.sleep(0.1)
    inject = asyncio.create_task(proxy.send(INJECT_UNMUTE_CH3))
    await asyncio.sleep(0.1)
    check("injection waits while the app frame is incomplete", not inject.done())
    w_a.write(IPAD_MUTE_CH2[20:])
    await w_a.drain()
    await asyncio.wait_for(inject, 2)
    await wait_for(lambda: len(rx) >= len(IPAD_MUTE_CH2) + len(INJECT_UNMUTE_CH3))
    check("mixer gets the app frame intact, then the injected one",
          bytes(rx) == IPAD_MUTE_CH2 + INJECT_UNMUTE_CH3)

    # A frame that never completes: the injection fails instead of corrupting.
    rx.clear()
    w_a.write(IPAD_MUTE_CH2[:20])
    await w_a.drain()
    await asyncio.sleep(0.1)
    try:
        await proxy.send(INJECT_UNMUTE_CH3)
        refused = False
    except ConnectionError:
        refused = True
    check("injection refused if the app frame never completes", refused)
    check("nothing injected into the incomplete frame", bytes(rx) == IPAD_MUTE_CH2[:20])
    w_a.write(IPAD_MUTE_CH2[20:])
    await w_a.drain()

    print("== 8 proxy: a second app closing first keeps the first one usable ==")
    r_b, w_b = await asyncio.open_connection("127.0.0.1", 16520)
    await wait_for(lambda: len(proxy._sessions) == 2)
    check("session epoch kept while a session stays live", proxy.session_epoch == epoch_a)
    w_b.close()
    await wait_for(lambda: len(proxy._sessions) == 1)
    rx.clear()
    try:
        await proxy.send(INJECT_UNMUTE_CH3)
        sent_ok = True
    except ConnectionError:
        sent_ok = False
    await wait_for(lambda: INJECT_UNMUTE_CH3 in bytes(rx))
    check("injection goes through the remaining session", sent_ok and INJECT_UNMUTE_CH3 in bytes(rx))
    check("still connected with the first session's epoch",
          proxy.connected and proxy.session_epoch == epoch_a)
    w_a.close()
    await wait_for(lambda: not proxy.connected)
    check("no session -> disconnected, no epoch", not proxy.connected and proxy.session_epoch is None)
    await proxy.stop()
    srv.close()


async def same_channel_race():
    print("== 2 controller: concurrent commands on one channel stay consistent ==")

    class SlowLink(FakeLink):
        def __init__(self):
            super().__init__()
            self.lock = asyncio.Lock()

        async def send(self, payload):
            async with self.lock:
                self.sent.append(payload.hex())
                await asyncio.sleep(0.01)  # a drain that yields

    cfg = Config(mixer={"max_channels": 16})
    store = StateStore()
    link = SlowLink()
    ctl = Controller(cfg, store, link, groups_store(cfg, "race.json"))
    await store.apply_event(FrameEvent(2, False, fader_value_to_hex(750.0), solo=False))
    await link.lock.acquire()  # e.g. a keepalive is draining
    a = asyncio.create_task(ctl.channel_set(2, True))           # Stream Deck mute
    b = asyncio.create_task(ctl.channel_set_fader(2, 500.0))    # UI fader drag
    await asyncio.sleep(0)
    link.lock.release()
    ra, rb = await asyncio.gather(a, b)
    final = parse_hex_stream(link.sent[-1])[0]
    stored = await store.get_channel(2)
    check("both commands succeed", ra["ok"] and rb["ok"])
    check("fader move re-sends the new mute state", final.muted is True)
    check("mixer and store agree", final.muted == stored.muted and final.fader_hex == stored.fader_hex)


async def link_liveness():
    print("== 3 direct client: a silent link is stale and gets reconnected ==")
    connections = []

    async def silent_mixer(r, w):
        connections.append(1)
        while await r.read(4096):
            pass  # accept handshakes, never answer

    srv = await asyncio.start_server(silent_mixer, "127.0.0.1", 16522)
    cfg = Config(mixer={"mode": "direct", "ip": "127.0.0.1", "port": 16522, "keepalive_seconds": 0.2})
    client = MixerClient(cfg, StateStore())
    client.start()
    await wait_for(lambda: client.connected)
    check("fresh connection is not stale", not client.link_stale())
    reconnected = await wait_for(lambda: len(connections) >= 2, timeout=3.0)
    check("no data for 2.5 keepalives -> reconnect", reconnected)
    await client.stop()
    srv.close()

    ctl_cfg = Config(mixer={"mode": "direct", "max_channels": 8, "keepalive_seconds": 10})
    stale = MixerClient(ctl_cfg, StateStore())
    stale.connected = True
    stale.session_epoch = time.monotonic() - 100
    stale.last_dump_ok_at = time.monotonic() - 30
    store = StateStore()
    await store.apply_event(FrameEvent(2, False, "1f14b643", solo=False))
    ctl = Controller(ctl_cfg, store, stale, groups_store(ctl_cfg, "stale.json"))
    res = await ctl.channel_set(2, True)
    check("sends refused while the link is stale",
          not res["ok"] and res["reason"] == "no data from mixer (link stale)")


def latest_mode_session():
    print("== 4 latest mode: values from before a reconnect are not trusted ==")
    cfg = Config()
    now = time.monotonic()
    epoch = now - 5
    before = ChannelState("1f14b643", now - 3600, False, now - 3600, solo=False)
    after = ChannelState("1f14b643", now - 1, False, now - 1, solo=False)
    check("value from an earlier connection blocked",
          check_channel_send(2, before, cfg, session_epoch=epoch).reason
          == "fader value predates current session")
    check("no live session blocked",
          check_channel_send(2, after, cfg).reason == "no live mixer session")
    check("value from this connection (any age) usable",
          check_channel_send(2, after, cfg, session_epoch=epoch).ok)
    check("stale mute state from an earlier connection blocks toggles",
          check_channel_send(2, ChannelState("1f14b643", now - 1, False, now - 3600, solo=False), cfg,
                             session_epoch=epoch, require_mute_state=True).reason
          == "mute state predates current session")


async def buffer_reset():
    print("== 5 direct client: a new connection starts with an empty parse buffer ==")
    n = []

    async def mixer(r, w):
        n.append(1)
        await r.read(4096)  # handshake
        if len(n) == 1:
            w.write(STATUS_MUTE_CH9[:16])  # connection drops mid-frame
            await w.drain()
            w.close()
            return
        w.write(STATUS_MUTE_CH4)
        await w.drain()
        while await r.read(4096):
            pass

    srv = await asyncio.start_server(mixer, "127.0.0.1", 16523)
    store = StateStore()
    cfg = Config(mixer={"mode": "direct", "ip": "127.0.0.1", "port": 16523, "keepalive_seconds": 0})
    client = MixerClient(cfg, store)
    client.start()
    got = await wait_for(lambda: store.last_update_at is not None, timeout=4.0)
    cs = await store.get_channel(4)
    check("first frame of the new connection parsed",
          got and cs is not None and cs.muted is True and cs.fader_hex == "00805844")
    await client.stop()
    srv.close()


def channel_space():
    print("== 6 inputs and buses never share a channel number ==")
    try:
        Config(mixer={"max_channels": 18, "buses": [{"channel": 17, "name": "Echo", "id": "8001"}]})
        rejected = False
    except ValidationError:
        rejected = True
    check("bus on an input channel number rejected", rejected)
    try:
        Config(mixer={"buses": [{"channel": 17, "name": "A", "id": "8001"},
                                {"channel": 17, "name": "B", "id": "8101"}]})
        dup_channel = False
    except ValidationError:
        dup_channel = True
    check("duplicate bus channel rejected", dup_channel)
    try:
        Config(mixer={"buses": [{"channel": 17, "name": "A", "id": "8001"},
                                {"channel": 18, "name": "B", "id": "8001"}]})
        dup_id = False
    except ValidationError:
        dup_id = True
    check("duplicate bus id rejected", dup_id)
    check("default input count is 16", Config().mixer.max_channels == 16)
    check("input frame above max_channels dropped",
          parse_hex_stream(STATUS_MUTE_CH9.hex(), max_input=8) == [])
    check("input frame within max_channels kept",
          [e.channel for e in parse_hex_stream(STATUS_MUTE_CH4.hex(), max_input=8)] == [4])
    buf = StreamBuffer(max_input=8)
    check("stream buffer drops input 9 with 8 inputs", buf.feed(STATUS_MUTE_CH9) == [])

    cfg = Config(mixer={"max_channels": 8})
    check("bus label is not the input's site name",
          Config(mixer={"max_channels": 8, "buses": [{"channel": 9, "name": "Hall 1", "id": "8001"}]})
          .channel_label(9) == "Hall 1")
    check("input keeps its site name", cfg.channel_label(9) == "Channel 9"
          and Config().channel_label(9) == "Gruen")


def config_literals():
    print("== 7 mistyped safety settings are rejected ==")
    for key, value in (("safety", {"freshness_mode": "sesion"}),
                       ("safety", {"freshness_mode": "Age"}),
                       ("mixer", {"mode": "Proxy"})):
        try:
            Config(**{key: value})
            ok = False
        except ValidationError:
            ok = True
        check(f"{key} {value} rejected", ok)
    check("default mode is direct", Config().mixer.mode == "direct")
    check("old 'companion' block still loads",
          Config(**{"companion": {"base_url": "http://127.0.0.1:8000"}}).mixer.ip)
    check("old allow_unsafe_debug_send key ignored",
          not hasattr(Config(safety={"allow_unsafe_debug_send": False}).safety,
                      "allow_unsafe_debug_send"))


def bus_persistence():
    print("== 10 a failing buses.json write does not break parsing ==")
    blocker = TMP / "not_a_dir"
    blocker.write_text("x")
    cfg = Config(mixer={"max_channels": 16})
    reg = BusRegistry(cfg, TMP / "buses.json")
    reg._path = blocker / "buses.json"  # the save now fails (parent is a file)
    seen = []
    reg.on_new_bus(seen.append)
    try:
        reg.discover([(128, "Hall 1")])
        raised = False
    except Exception:  # noqa: BLE001
        raised = True
    check("discovery survives the write error", not raised)
    check("listener still told about the new bus", seen == [17])
    check("bus usable at runtime", 17 in cfg.valid_channels())


async def groups_file():
    print("== 11 groups.json: corrupt, empty and invalid files ==")
    cfg = Config(mixer={"max_channels": 8})
    seed = {"drums": [1, 2]}

    path = TMP / "corrupt.json"
    path.write_text("{ not json")
    gs = GroupStore(path, cfg.valid_channels(), seed)
    check("corrupt file kept as .corrupt", (TMP / "corrupt.json.corrupt").read_text() == "{ not json")
    check("corrupt file not overwritten on start", path.read_text() == "{ not json")
    check("seed groups used in memory", await gs.list() == seed)

    path = TMP / "empty.json"
    path.write_text("{}")
    gs = GroupStore(path, cfg.valid_channels(), seed)
    check("'{}' is kept (no reseed)", await gs.list() == {} and json.loads(path.read_text()) == {})

    path = TMP / "invalid.json"
    path.write_text(json.dumps({"ok": [1, 2], "bad name!": [1], "nochans": [], "str": "x",
                                "zero": [0], "laterbus": [3, 19]}))
    gs = GroupStore(path, cfg.valid_channels(), seed)
    check("invalid entries dropped, unknown-yet bus channels kept",
          await gs.list() == {"ok": [1, 2], "laterbus": [3, 19]})

    path = TMP / "missing.json"
    gs = GroupStore(path, cfg.valid_channels(), seed)
    check("missing file seeded and written", json.loads(path.read_text()) == seed)
    await gs.create("New", [3])
    check("writes leave no temp file", not (TMP / "missing.json.tmp").exists())


def bad_input_mute():
    print("== 12 an input frame with an unknown mute value makes the channel unknown ==")
    events = parse_hex_stream(STATUS_BADMUTE_CH4.hex())
    check("event emitted with unknown state",
          [(e.channel, e.muted, e.fader_hex) for e in events] == [(4, None, None)])


async def solo_after_failure():
    print("== 13 solo: others stay untouched if the group did not fully unmute ==")
    cfg = Config(mixer={"max_channels": 4},
                 safety={"exclusive_group_unmute": True, "group_send_delay_ms": 0})
    store = StateStore()
    # channel 2's unmute command fails to send
    link = FakeLink(fail_on="070708012d")
    ctl = Controller(cfg, store, link, groups_store(cfg, "solo.json", {"g": [1, 2]}))
    for ch in (1, 2):
        await store.apply_event(FrameEvent(ch, True, "1f14b643", solo=False))
    for ch in (3, 4):
        await store.apply_event(FrameEvent(ch, False, "1f14b643", solo=False))
    res = await ctl.group_set("g", False)
    check("group result not ok", res["ok"] is False)
    check("no other channel muted", "muted_others" not in res
          and all(parse_hex_stream(h)[0].channel in (1, 2) for h in link.sent))


async def main():
    await proxy_mid_frame()
    await same_channel_race()
    await link_liveness()
    latest_mode_session()
    await buffer_reset()
    channel_space()
    config_literals()
    bus_persistence()
    await groups_file()
    bad_input_mute()
    await solo_after_failure()
    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    asyncio.run(main())
