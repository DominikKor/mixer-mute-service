"""Offline self-test: protocol round-trips, parser, safety, and a live HTTP
smoke test (no real mixer -> sends must be blocked)."""

import asyncio
import sys
import time

from app.protocol import build_mute_command, parse_hex_stream, is_valid_fader_hex
from app.config import Config
from app.state import ChannelState
from app.safety import check_channel_send

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok   {name}")
    else:
        FAIL += 1
        print(f"  FAIL {name}")


print("== protocol: build_mute_command ==")
mute = build_mute_command(2, True, "1f14b643").hex()
unmute = build_mute_command(2, False, "1f14b643").hex()
check("ch2 mute matches spec", mute == "5a5a5a5a5a5a0000006e0000000909080120012d1f14b643")
check("ch2 unmute matches spec", unmute == "5a5a5a5a5a5a0000006e000000070708012d1f14b643")
# channel 1 is addressed by object id, not 08 00
c1u = build_mute_command(1, False, "00007a44").hex()
c1m = build_mute_command(1, True, "00007a44").hex()
check("ch1 unmute uses id form (observed)", c1u == "5a5a5a5a5a5a0000006e0000000a0a1500abc93d2d00007a44")
check("ch1 mute uses id form", c1m == "5a5a5a5a5a5a0000006e0000000c0c1500abc93d20012d00007a44")
# channels 9-16 use positional 08 XX (index 08..0f)
c9 = build_mute_command(9, False, "1f14b643", max_channels=16).hex()
c16 = build_mute_command(16, True, "1f14b643", max_channels=16).hex()
check("ch9 unmute -> 08 08", c9 == "5a5a5a5a5a5a0000006e000000070708082d1f14b643")
check("ch16 mute -> 08 0f", c16 == "5a5a5a5a5a5a0000006e0000000909080f20012d1f14b643")
# and they round-trip through the parser to the right channel
rt = parse_hex_stream(c9 + c16)
check("ch9/ch16 parse back correctly", [e.channel for e in rt] == [9, 16])

print("== protocol: buses (type 72 cmd / 71 status) ==")
from app.protocol import build_bus_command
eb_m = build_bus_command("8001", True, "00009e43").hex()
eb_u = build_bus_command("8001", False, "00009e43").hex()
check("Echo mute cmd matches observed",
      eb_m == "5a5a5a5a5a5a000000720000000a0a08800118013d00009e43")
check("Echo unmute cmd matches observed",
      eb_u == "5a5a5a5a5a5a0000007200000008080880013d00009e43")
busev = parse_hex_stream(
    "5a5a5a5a5a5a00000071000001000a08800118013d00009e43"   # Echo mute (status)
    "5a5a5a5a5a5a0000007100000100080883013d00801b44",      # Monitor unmute (status)
    buses=[(17, "8001"), (18, "8301")],
)
check("bus status frames parsed (2)", len(busev) == 2)
check("Echo -> ch17 muted", any(e.channel == 17 and e.muted and e.fader_hex == "00009e43" for e in busev))
check("Monitor -> ch18 unmuted", any(e.channel == 18 and not e.muted and e.fader_hex == "00801b44" for e in busev))

print("== protocol: fader encoding (little-endian float32, 0..1000) ==")
from app.protocol import fader_hex_to_value, fader_value_to_hex, StreamBuffer
check("00007a44 = 1000 (top)", fader_hex_to_value("00007a44") == 1000.0)
check("0000803f = 1.0", fader_hex_to_value("0000803f") == 1.0)
check("NaN rejected", fader_hex_to_value("ffffffff") is None)
check("negative rejected", fader_hex_to_value("000080bf") is None)
check("above 1000 rejected", fader_hex_to_value("00807a44") is None)
check("encode 1000", fader_value_to_hex(1000) == "00007a44")
for bad in (float("nan"), float("inf"), -1.0, 1000.5, True, "750"):
    try:
        fader_value_to_hex(bad)
        check(f"encode rejects {bad!r}", False)
    except ValueError:
        check(f"encode rejects {bad!r}", True)
# Byte-exact against frames the real app sent (capture 2026-10-26, channel 8 muted).
check("app frame: ch8 muted fader 102.8",
      build_mute_command(8, True, "f9a2cd42").hex()
      == "5a5a5a5a5a5a0000006e00000009" "09080720012df9a2cd42")
check("app frame: ch8 muted fader -inf omits the fader field",
      build_mute_command(8, True, "00000000").hex() == "5a5a5a5a5a5a0000006e0000000404" "08072001")
check("ch8 unmuted at -inf", build_mute_command(8, False, "00000000").hex() == "5a5a5a5a5a5a0000006e0000000202" "0807")
check("ch1 at -inf keeps its extra field",
      build_mute_command(1, False, "00000000").hex() == "5a5a5a5a5a5a0000006e0000000505" "1500abc93d")
check("bus muted at -inf", build_bus_command("8001", True, "00000000").hex()
      == "5a5a5a5a5a5a000000720000000505" "0880011801")
try:
    build_mute_command(2, True, "ffffffff")
    check("build rejects NaN fader", False)
except ValueError:
    check("build rejects NaN fader", True)

print("== parser: real status frames without fader field ==")
pad = "00" * 248
ev = parse_hex_stream("5a5a5a5a5a5a0000006d000001000408072001" + pad
                      + "5a5a5a5a5a5a0000006d000001000208 07".replace(" ", "") + pad)
check("ch8 muted at -inf parsed", (ev[0].channel, ev[0].muted, ev[0].fader_hex) == (8, True, "00000000"))
check("ch8 unmuted at -inf parsed", (ev[1].channel, ev[1].muted, ev[1].fader_hex) == (8, False, "00000000"))
ev = parse_hex_stream("5a5a5a5a5a5a0000006d000001000708012dffffffff" + pad)
check("NaN fader frame invalidates channel", len(ev) == 1 and ev[0].channel == 2 and ev[0].fader_hex is None and ev[0].muted is None)
ev = parse_hex_stream("5a5a5a5a5a5a0000006e00000005052d00007a44")
check("ch1 frame without its extra field invalidates ch1", len(ev) == 1 and ev[0].channel == 1 and ev[0].fader_hex is None)
ev = parse_hex_stream("5a5a5a5a5a5a0000006e000000090908011801 2d00007a44".replace(" ", ""))
check("unknown field -> frame ignored", ev == [])
sb = StreamBuffer()
frame = bytes.fromhex("5a5a5a5a5a5a0000006d0000010009080720012d0000c643" + pad)
got = []
for i in range(0, len(frame), 5):
    got += sb.feed(frame[i:i + 5])
check("frame split in 5-byte reads parsed once", [(e.channel, e.fader_hex) for e in got] == [(8, "0000c643")])

print("== protocol: is_valid_fader_hex ==")
check("valid 8-hex", is_valid_fader_hex("1f14b643"))
check("reject 7-hex", not is_valid_fader_hex("1f14b6"))
check("reject non-hex", not is_valid_fader_hex("zzzzzzzz"))
check("reject non-str", not is_valid_fader_hex(123))

print("== protocol: build rejects bad input ==")
try:
    build_mute_command(99, True, "1f14b643")
    check("reject out-of-range channel", False)
except ValueError:
    check("reject out-of-range channel", True)
try:
    build_mute_command(2, True, "00")
    check("reject bad fader", False)
except ValueError:
    check("reject bad fader", True)

print("== protocol: parse_hex_stream (6e + 6d, padding, split) ==")
stream = (
    "deadbeef"  # leading garbage
    "5a5a5a5a5a5a0000006e0000000909080120012d1f14b643"  # 6e mute ch2
    "5a5a5a5a5a5a0000006d000001000708012d000051430000000000000000"  # 6d unmute ch2 + padding
)
events = parse_hex_stream(stream)
check("found 2 events", len(events) == 2)
e0 = events[0]
check("first is ch2 mute fader 1f14b643",
      e0.channel == 2 and e0.muted is True and e0.fader_hex == "1f14b643")
e1 = events[1]
check("second is ch2 unmute fader 00005143",
      e1.channel == 2 and e1.muted is False and e1.fader_hex == "00005143")
check("events sorted by position", events[0].end_hex_index < events[1].end_hex_index)

# channel 1 (id-addressed) frames from real captures
c1 = parse_hex_stream(
    "5a5a5a5a5a5a0000006e0000000a0a1500abc93d2d00007a44"   # 6e unmote ch1
    "5a5a5a5a5a5a0000006d000001000a1500abc93d2d00007a44"   # 6d unmute ch1 (status)
    "5a5a5a5a5a5a0000006e0000000c0c1500abc93d20012d00007a44"  # 6e mute ch1
)
check("ch1 frames parsed (3)", len(c1) == 3)
check("ch1 mapped to channel 1", all(e.channel == 1 for e in c1))
check("ch1 fader extracted", all(e.fader_hex == "00007a44" for e in c1))
check("ch1 mute flag correct", [e.muted for e in c1] == [False, False, True])
# id-addressed channel-1 frame must NOT be misread as a positional channel
check("ch1 not misparsed as 08-form", all(e.channel == 1 for e in c1))

print("== safety (freshness modes) ==")
now = time.monotonic()
session = now - 10  # current session began 10s ago
EP = {"session_epoch": session}
seen = ChannelState(fader_hex="1f14b643", fader_updated_at=now - 3, muted=False, muted_updated_at=now - 3)
old = ChannelState(fader_hex="1f14b643", fader_updated_at=session - 5, muted=False, muted_updated_at=session - 5)
empty = ChannelState()

# default mode "latest": any present valid value is usable (any age, no session)
cfg = Config()
check("latest: old value usable (no session)", check_channel_send(2, old, cfg).ok)
check("latest: unknown fader blocked", check_channel_send(2, empty, cfg).reason == "no fader value known")
check("latest: invalid channel blocked", check_channel_send(99, seen, cfg).reason == "invalid channel")
check("latest: malformed fader blocked",
      check_channel_send(2, ChannelState(fader_hex="zz", fader_updated_at=now), cfg).reason == "invalid fader value")
try:
    Config(desktop_display={"profiles": {"bad": {
        "title": "Bad target", "kind": "image", "path": "C:/image.png",
        "target_ip": "999.999.999.999",
    }}})
    invalid_desktop_ip_rejected = False
except ValueError:
    invalid_desktop_ip_rejected = True
check("desktop profile rejects invalid IPv4", invalid_desktop_ip_rejected)

# "session" mode
cfg_s = Config(safety={"freshness_mode": "session"})
check("session: value seen this session safe", check_channel_send(2, seen, cfg_s, **EP).ok)
check("session: value predating session blocked",
      check_channel_send(2, old, cfg_s, **EP).reason == "fader value predates current session")
check("session: no live session blocked", check_channel_send(2, seen, cfg_s).reason == "no live mixer session")

# "age" mode (default cap 1s)
cfg_a = Config(safety={"freshness_mode": "age"})
check("age: fresh value safe",
      check_channel_send(2, ChannelState(fader_hex="1f14b643", fader_updated_at=now - 0.1), cfg_a).ok)
check("age: stale value blocked", check_channel_send(2, old, cfg_a).reason == "fader value stale")

# toggle needs a known mute state in any mode
check("toggle needs mute state",
      check_channel_send(2, ChannelState(fader_hex="1f14b643", fader_updated_at=now), cfg,
                         require_mute_state=True).reason == "mute state unknown")

print("== HTTP smoke test (isolated config, no mixer connected) ==")
# Use a throwaway config dir so the test is independent of the deployed
# config.yaml / groups.json (which the operator may have changed).
import os, tempfile, pathlib
_tmp = tempfile.mkdtemp(prefix="mixer_selftest_")
pathlib.Path(_tmp, "config.yaml").write_text(
    "mixer:\n  max_channels: 8\n"
    "groups:\n"
    "  drums:\n    channels: [1, 2, 3, 4]\n"
    "  vocals:\n    channels: [5, 6]\n"
    "  all:\n    channels: [1, 2, 3, 4, 5, 6, 7, 8]\n"
    "desktop_display:\n"
    "  enabled: false\n"
    "  profiles:\n"
    "    sample:\n"
    "      title: Sample\n"
    "      kind: image\n"
    "      path: C:/sample.png\n"
    "      target_ip: 192.168.188.42\n"
)
os.environ["MIXER_CONFIG"] = str(pathlib.Path(_tmp, "config.yaml"))

from fastapi.testclient import TestClient
from app.main import app

with TestClient(app) as client:
    h = client.get("/health").json()
    check("/health ok", h["ok"] is True and h["mixer_connected"] is False)
    desktop = client.get("/desktop/profiles").json()
    check("desktop profile target IP", desktop["profiles"][0]["target_ip"] == "192.168.188.42")
    check("desktop action disabled by default",
          client.post("/desktop/sample/open").status_code == 503)
    check("unknown desktop profile -> 404",
          client.post("/desktop/not-configured/open").status_code == 404)
    import os
    from unittest.mock import patch
    old_token = os.environ.get("DESKTOP_AGENT_TOKEN")
    os.environ["DESKTOP_AGENT_TOKEN"] = "selftest-token"
    app.state.cfg.desktop_display.enabled = True
    with patch("app.routes._forward_display_request", return_value={"ok": True}) as forward:
        check("desktop profile open", client.post("/desktop/sample/open").status_code == 200)
        check("desktop profile routed to configured IP",
              forward.call_args.args[0] == "http://192.168.188.42:8766/display")
    app.state.cfg.desktop_display.enabled = False
    if old_token is None:
        os.environ.pop("DESKTOP_AGENT_TOKEN", None)
    else:
        os.environ["DESKTOP_AGENT_TOKEN"] = old_token
    s = client.get("/state").json()
    check("/state has 8 channels", len(s["channels"]) == 8)
    check("/state channels unsafe (no data)", all(not c["safe"] for c in s["channels"].values()))
    m = client.post("/channels/2/mute")
    check("mute blocked w/ 409 no connection", m.status_code == 409)
    check("mute reason = no mixer connection", m.json()["reason"] == "no mixer connection")
    g = client.post("/groups/unknown/mute")
    check("unknown group -> 404", g.status_code == 404)
    bad = client.post("/channels/99/mute")
    check("invalid channel -> 400", bad.status_code == 400)
    fd = client.post("/channels/2/fader", json={"value": 750})
    check("fader blocked w/ 409 no connection", fd.status_code == 409 and fd.json()["reason"] == "no mixer connection")
    fbad = client.post("/channels/2/fader", json={"value": 5000})
    check("fader out of range -> 400", fbad.status_code == 400 and fbad.json()["reason"] == "invalid fader value")
    rbad = client.post("/channels/2/recall-fader", json={"fader_hex": "ffffffff"})
    check("recall NaN hex -> 400", rbad.status_code == 400)
    p = client.post("/panic/disable").json()
    check("panic disable", p["enabled"] is False)
    client.post("/panic/enable")

    print("== group CRUD ==")
    seeded = client.get("/groups").json()
    check("seeded groups present", {"drums", "vocals", "all"} <= set(seeded))
    client.delete("/groups/selftest")  # clean any leftover from a prior run
    cr = client.post("/groups", json={"name": "selftest", "channels": [1, 2]})
    check("create group 200", cr.status_code == 200 and cr.json()["channels"] == [1, 2])
    dup = client.post("/groups", json={"name": "selftest", "channels": [1]})
    check("duplicate create -> 400", dup.status_code == 400)
    badch = client.post("/groups", json={"name": "bad", "channels": [99]})
    check("out-of-range channel -> 400", badch.status_code == 400)
    badname = client.post("/groups", json={"name": "/etc", "channels": [1]})
    check("invalid name -> 400", badname.status_code == 400)
    upd_post = client.post("/groups/selftest/update", json={"channels": [2, 3]})
    check("POST update group", upd_post.status_code == 200 and upd_post.json()["channels"] == [2, 3])
    upd = client.put("/groups/selftest", json={"channels": [3, 3, 4]})
    check("update dedups channels", upd.status_code == 200 and upd.json()["channels"] == [3, 4])
    got = client.get("/groups/selftest").json()
    check("get reflects update", got["channels"] == [3, 4])
    missing_post = client.post("/groups/nope/update", json={"channels": [1]})
    check("POST update missing -> 404", missing_post.status_code == 404)
    missing = client.put("/groups/nope", json={"channels": [1]})
    check("update missing -> 404", missing.status_code == 404)
    dele_post = client.post("/groups/selftest/delete")
    check("POST delete group", dele_post.status_code == 200 and dele_post.json()["deleted"] is True)
    gone_post = client.get("/groups/selftest")
    check("POST deleted group -> 404", gone_post.status_code == 404)
    cr_again = client.post("/groups", json={"name": "selftest", "channels": [1, 2]})
    check("recreate group for legacy delete", cr_again.status_code == 200)
    dele = client.delete("/groups/selftest")
    check("delete 200", dele.status_code == 200)
    gone = client.get("/groups/selftest")
    check("deleted group -> 404", gone.status_code == 404)

    print("== UI served ==")
    ui = client.get("/")
    check("/ serves html with desktop profile controls",
          ui.status_code == 200 and "Mixer Mute Service" in ui.text and "desktop-profiles" in ui.text)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
