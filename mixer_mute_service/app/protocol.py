"""Protocol layer: build outgoing commands and parse incoming mixer frames.

All knowledge here is reverse-engineered from captured TCP payloads and is
deliberately conservative. The most important rule of the whole project lives
here implicitly: a mute/unmute command *always* rewrites the fader value, so we
only ever build a command from a fader value that was actually observed.

The parser is byte based. Frames look like a length-delimited protobuf message
behind a fixed header (inferred from captures, not officially documented):

    5a5a5a5a5a5a | type u32 BE | length u32 BE | varint msg_len | message

Status frames (6d/71) are zero padded to their length. Only messages whose
fields are all understood update state. A message that is recognisably about
a channel's fader/mute/solo but carries a field we do not know does NOT keep
the old state: it makes that channel unknown (sends blocked) until a clean
report arrives, because the old values may no longer be true.
"""

from __future__ import annotations

import logging
import math
import re
import struct
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("mixer.protocol")

# Header that the mixer accepts as an incoming control command (App -> Mixer).
# IMPORTANT: do not append a trailing 00. The prefix is exactly this.
COMMAND_PREFIX = "5a5a5a5a5a5a0000006e000000"

# Buses/auxes (Echo, Monitor, ...) are a different object class. Commands use a
# 72 type (not 6e), the muted flag is 18 01 (field 3, not 20 01), and the fader
# follows a 3d marker (not 2d). Status comes back as type 71. The bus is named by
# an object-id varint (e.g. Echo = 8001 = 128) carried as "08 <varint>".
BUS_COMMAND_PREFIX = "5a5a5a5a5a5a00000072000000"

# Channels are addressed by index: field 1 "08 XX" with XX = channel-1 (so
# channel 2 = 08 01 .. channel 8 = 08 07). For channel 1 the index is 0, which
# protobuf omits; instead every channel-1 message (app, console status and the
# state dump) carries an extra fixed32 field 2 ("15" + 00abc93d) whose meaning
# is unknown. We send it back exactly as observed and refuse to trust a frame
# whose extra field differs from this table.
# Map: channel number -> the hex fragment that replaces "08 XX".
CHANNEL_ID_FIELDS = {
    1: "1500abc93d",  # observed on every channel-1 fader/mute report
}

# Subscribe/keepalive frame the iPad sends right after connecting. Carries no
# channel/mute/fader payload. Verified: sending this on our own connection makes
# the mixer stream the full state dump plus live incremental updates -- the basis
# for "direct" (iPad-free) mode.
SUBSCRIBE_HANDSHAKE_HEX = "5a5a5a5a5a5a0000000100000000"

MAGIC = "5a5a5a5a5a5a"

# The 4 fader bytes are a little-endian float32 fader position: 0.0 = -inf
# (bottom), 1000.0 = top. Verified against captures of the app and the console.
# A value of 0.0 is never transmitted: like protobuf, the field is simply omitted.
FADER_MAX = 1000.0


_FADER_HEX_RE = re.compile(r"[0-9a-fA-F]{8}")


def is_valid_fader_hex(value: object) -> bool:
    """A valid fader value is exactly 8 hex digits (4 bytes), nothing else.
    (bytes.fromhex would also accept spaces: "1f 14 b6" is 8 chars, 3 bytes.)"""
    return isinstance(value, str) and _FADER_HEX_RE.fullmatch(value) is not None


def fader_hex_to_value(fader_hex: object) -> Optional[float]:
    """Decode a fader hex to its position (0..1000), or None if it is not one.

    NaN, infinities, negatives and values above FADER_MAX are rejected: such
    bytes are never a real fader position and sending them can crash the mixer.
    """
    if not is_valid_fader_hex(fader_hex):
        return None
    value = struct.unpack("<f", bytes.fromhex(fader_hex))[0]
    if not math.isfinite(value) or not 0.0 <= value <= FADER_MAX:
        return None
    return value + 0.0  # normalise -0.0


def fader_value_to_hex(value: float) -> str:
    """Encode a fader position (0..1000) as the 8-hex little-endian float32."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ValueError(f"Invalid fader value: {value!r}")
    value = float(value)
    if not math.isfinite(value) or not 0.0 <= value <= FADER_MAX:
        raise ValueError(f"Invalid fader value: {value!r} (0..{FADER_MAX:g})")
    return struct.pack("<f", value + 0.0).hex()


def is_safe_fader_hex(value: object) -> bool:
    """Valid hex AND a real fader position (finite, 0..1000)."""
    return fader_hex_to_value(value) is not None


def _fader_field(marker: str, fader_hex: str) -> str:
    """The fader field (marker + 4 bytes), or nothing for position 0 (-inf),
    exactly as the app and the console encode it."""
    value = fader_hex_to_value(fader_hex)
    if value is None:
        raise ValueError(f"Invalid fader_hex: {fader_hex!r}")
    return "" if value == 0.0 else f"{marker}{fader_hex.lower()}"


def build_mute_command(
    channel: int, muted: bool, fader_hex: str, max_channels: int = 16, *, solo: bool = False
) -> bytes:
    """Build a full App -> Mixer frame.

    Mute:   PREFIX + 090908 XX 20012d FFFFFFFF
    Unmute: PREFIX + 070708 XX     2d FFFFFFFF
    Soloed: PREFIX + LL LL 08 XX 1801 [2001] 2d FFFFFFFF

    XX = channel - 1 (zero based), FFFFFFFF = current observed fader value.
    A fader at position 0 (-inf) omits the 2d field, as the app does.
    Like mute and fader, the solo flag (field 3) is part of every command: a
    command without it switches the console's solo off, so the observed solo
    state must be sent back unchanged.
    """
    if not 1 <= channel <= max_channels:
        raise ValueError(f"Invalid channel: {channel} (1..{max_channels})")
    fader = _fader_field("2d", fader_hex)

    # Channel-identifying field: either the special object id or "08 XX".
    if channel in CHANNEL_ID_FIELDS:
        ident = CHANNEL_ID_FIELDS[channel]
    else:
        ident = f"08{channel - 1:02x}"

    # Body without the leading length marker, fields in the console's order.
    body = f"{ident}{'1801' if solo else ''}{'2001' if muted else ''}{fader}"
    # The frame is prefixed by the payload length, repeated twice (LL LL),
    # e.g. channel 2 mute = 09 09 .., channel 1 unmute = 0a 0a ..
    length = len(body) // 2
    payload = f"{length:02x}{length:02x}{body}"

    return bytes.fromhex(COMMAND_PREFIX + payload)


def build_bus_command(bus_id: str, muted: bool, fader_hex: str, *, solo: bool = False) -> bytes:
    """Build a bus mute/unmute command (type 72).

    Mute:   72.. <LL LL> 08<bus_id> 18 01 3d <fader>
    Unmute: 72.. <LL LL> 08<bus_id> 3d <fader>
    Soloed: 72.. <LL LL> 08<bus_id> 10 01 [18 01] 3d <fader>  (solo = field 2)
    """
    fader = _fader_field("3d", fader_hex)
    ident = f"08{bus_id}"
    body = f"{ident}{'1001' if solo else ''}{'1801' if muted else ''}{fader}"
    length = len(body) // 2
    payload = f"{length:02x}{length:02x}{body}"
    return bytes.fromhex(BUS_COMMAND_PREFIX + payload)


@dataclass
class FrameEvent:
    """A single mute/fader state observation extracted from the stream.

    ``muted`` and ``fader_hex`` are None when a frame for the channel was seen
    but cannot be trusted (e.g. an unexpected extra field or a fader value that
    is not a real position). Applying such an event makes the channel unknown,
    so every send to it is blocked until a clean frame arrives.
    """

    channel: int
    muted: Optional[bool]
    fader_hex: Optional[str]
    # Hex of the frame this came from. For a full state dump (several KB, one
    # event per channel) only the 14-byte frame header is kept.
    raw_match: str = ""
    end_hex_index: int = 0  # index into the hex string just past this frame
    # True if this came from a full state dump (see StateStore.apply_event).
    from_dump: bool = False
    # Console solo (headphone monitoring). False when the frame has no solo
    # field (= not soloed), None with muted/fader None for an untrusted section.
    # No default of False: an event built without saying it must not claim
    # "not soloed" (the next command would switch the console's solo off).
    solo: Optional[bool] = None


_MAGIC = bytes.fromhex(MAGIC)
_HEADER_LEN = 14  # magic(6) + type u32 + length u32

# Message types that carry exactly one fader section.
_INPUT_TYPES = (0x6E, 0x6D)  # command (app -> mixer) / status (mixer -> app)
_BUS_TYPES = (0x72, 0x71)
# Full state dump the mixer sends after the subscribe handshake.
_DUMP_TYPE = 0x01

# Protobuf wire types.
WIRE_VARINT, WIRE_FIXED32, WIRE_BYTES = 0, 5, 2
_VARINT, _FIXED32, _BYTES = WIRE_VARINT, WIRE_FIXED32, WIRE_BYTES

# Input fader section (also field 7 of every channel object in the dump):
#   1 = channel index (omitted for index 0), 2 = extra fixed32 only seen on
#   channel 1, 3 = solo, 4 = muted, 5 = fader. Omitted fields are 0 (protobuf
#   defaults). Solo verified live on 2026-11-09: solo on a muted channel 2 is
#   08 01 18 01 20 01 2d <fader>.
_INPUT_FIELDS = {1: _VARINT, 2: _FIXED32, 3: _VARINT, 4: _VARINT, 5: _FIXED32}
# Bus fader section: 1 = bus object id, 2 = solo, 3 = muted, 7 = fader
# (verified live: "PC Input" soloed = 08 8401 10 01 3d <fader>).
_BUS_FIELDS = {1: _VARINT, 2: _VARINT, 3: _VARINT, 7: _FIXED32}
# Dump: top-level fields 1 and 2 hold the input channel objects (8 each); each
# object has its index in field 1 and its fader section in field 7.
_DUMP_INPUT_GROUPS = (1, 2)
_DUMP_SECTION_FIELD = 7
# Dump: top-level field 3 holds the bus objects (Hall 1 = 128 .. Monitor = 131 ..
# main L/R = 255); each has its object id in field 1 and its fader section in
# field 6, byte-identical to the message of a type-71 bus status frame
# (e.g. 08 8001 18 01 3d <fader>). It is the only sub-message of a bus object
# with that shape.
_DUMP_BUS_GROUP = 3
_DUMP_BUS_SECTION_FIELD = 6

_ZERO_FADER = "00000000"


def _read_varint(data: bytes, pos: int) -> Tuple[Optional[int], int]:
    """Decode a varint at pos -> (value, next_pos).

    Returns (None, pos) if the data ends mid-varint and (None, -1) if it is
    malformed (longer than 64 bits).
    """
    value = shift = 0
    while pos < len(data):
        b = data[pos]
        pos += 1
        value |= (b & 0x7F) << shift
        if not b & 0x80:
            return value, pos
        shift += 7
        if shift > 63:
            return None, -1
    return None, pos


def decode_fields(msg: bytes) -> Optional[List[Tuple[int, int, object]]]:
    """Decode a flat protobuf message into [(field, wire_type, value)].

    varint -> int, fixed32 -> 4 raw bytes, length-delimited -> bytes.
    Returns None if the message is malformed or uses another wire type.
    """
    fields: List[Tuple[int, int, object]] = []
    pos = 0
    while pos < len(msg):
        key, pos = _read_varint(msg, pos)
        if key is None:
            return None
        fld, wire = key >> 3, key & 7
        if wire == _VARINT:
            value, pos = _read_varint(msg, pos)
            if value is None:
                return None
        elif wire == _FIXED32:
            if pos + 4 > len(msg):
                return None
            value, pos = msg[pos:pos + 4], pos + 4
        elif wire == _BYTES:
            n, pos = _read_varint(msg, pos)
            if n is None or pos + n > len(msg):
                return None
            value, pos = msg[pos:pos + n], pos + n
        else:
            return None
        fields.append((fld, wire, value))
    return fields


def _strict_fields(msg: bytes, schema: Dict[int, int]) -> Optional[Dict[int, object]]:
    """Fields of msg as {field: value}, only if every field is in schema with
    the expected wire type and appears once. Otherwise None (not this shape)."""
    fields = decode_fields(msg)
    if fields is None:
        return None
    out: Dict[int, object] = {}
    for fld, wire, value in fields:
        if schema.get(fld) != wire or fld in out:
            return None
        out[fld] = value
    return out


Section = Tuple[int, Optional[bool], Optional[str], Optional[bool]]


def _unknown(channel: int, what: str, msg: bytes) -> Section:
    """A report about ``channel`` that cannot be trusted: the channel becomes
    unknown (muted/fader/solo None), so every send to it is blocked until a
    clean report arrives. Logged with its bytes, so a new field can be mapped."""
    log.warning("untrusted %s for channel %s, channel now unknown: %s", what, channel, msg.hex())
    return channel, None, None, None


def _input_channel_of(fields: list) -> Optional[int]:
    """Channel an input fader section (possibly with unknown fields) is about,
    or None if it does not look like one. A fader section names its channel in
    field 1 (index; omitted for channel 1) and carries solo/mute/fader (3/4/5)."""
    index = [(w, v) for f, w, v in fields if f == 1]
    if len(index) > 1 or (index and index[0][0] != _VARINT):
        return None
    if not any((f, w) in ((3, _VARINT), (4, _VARINT), (5, _FIXED32)) for f, w, _ in fields):
        return None
    value = index[0][1] if index else 0
    return value + 1 if value <= 63 else None


def _input_section(msg: bytes) -> Optional[Section]:
    """Interpret an input fader section -> (channel, muted, fader_hex, solo).

    Returns None if the message is not a fader section. Returns muted/fader/
    solo as None (= make the channel unknown) if it is one but cannot be
    trusted: an unknown or repeated field, a bad value, or an extra field 2
    other than the one build_mute_command re-sends.
    """
    f = _strict_fields(msg, _INPUT_FIELDS)
    if f is None:
        fields = decode_fields(msg)
        channel = _input_channel_of(fields) if fields is not None else None
        return None if channel is None else _unknown(channel, "input section", msg)
    index = f.get(1, 0)
    if index > 63:
        return None
    channel = index + 1
    mute = f.get(4, 0)
    solo = f.get(3, 0)
    fader_hex = f[5].hex() if 5 in f else _ZERO_FADER
    # The extra field must be exactly what build_mute_command re-sends for this
    # channel, otherwise a command from us would change it.
    extra = ("15" + f[2].hex()) if 2 in f else None
    if (mute not in (0, 1) or solo not in (0, 1) or extra != CHANNEL_ID_FIELDS.get(channel)
            or not is_safe_fader_hex(fader_hex)):
        return _unknown(channel, "input section", msg)
    return channel, bool(mute), fader_hex, bool(solo)


def _bus_section(msg: bytes, buses: Dict[int, int]) -> Optional[Section]:
    """Interpret a bus fader section -> (channel, muted, fader_hex, solo) for
    a configured bus, or None. An untrusted section of a known bus makes that
    bus unknown (see _input_section)."""
    f = _strict_fields(msg, _BUS_FIELDS)
    if f is None:
        fields = decode_fields(msg) or []
        ids = {v for fld, w, v in fields if fld == 1 and w == _VARINT}
        looks_like_bus = any((fld, w) in ((2, _VARINT), (3, _VARINT), (7, _FIXED32))
                             for fld, w, _ in fields)
        if len(ids) == 1 and looks_like_bus:
            ident = next(iter(ids))
            if ident in buses:
                return _unknown(buses[ident], "bus section", msg)
        return None
    if 1 not in f or f[1] not in buses:
        return None
    channel = buses[f[1]]
    mute = f.get(3, 0)
    solo = f.get(2, 0)
    fader_hex = f[7].hex() if 7 in f else _ZERO_FADER
    if mute not in (0, 1) or solo not in (0, 1) or not is_safe_fader_hex(fader_hex):
        return _unknown(channel, "bus section", msg)
    return channel, bool(mute), fader_hex, bool(solo)


def _object_section(obj_fields: list, section_field: int) -> Optional[bytes]:
    """The object's fader section, or None unless it has exactly one."""
    sections = [v for f, w, v in obj_fields if f == section_field and w == _BYTES]
    return sections[0] if len(sections) == 1 else None


def _object_id(obj_fields: list) -> Optional[int]:
    return next((v for f, w, v in obj_fields if f == 1 and w == _VARINT), None)


# Input channel object in the dump: 3 = input (gain, HPF), 4 = EQ,
# 5 = dynamics (gate, compressor). Mapped live on 2026-11-09, see
# app/channel_settings.py.
_SETTINGS_FIELDS = {3: "input", 4: "eq", 5: "dynamics"}

BusObjects = List[Tuple[int, str]]


@dataclass
class Dump:
    """Everything the service reads from one full state dump, decoded once."""

    sections: List[Section] = field(default_factory=list)
    # (object id, console name) of every bus with a well-formed fader section.
    bus_objects: BusObjects = field(default_factory=list)
    # {channel: {"input"|"eq"|"dynamics": raw bytes}} of every input channel.
    settings: Dict[int, Dict[str, bytes]] = field(default_factory=dict)
    input_objects: int = 0  # 0 = nothing could be read from this dump

    @property
    def ok(self) -> bool:
        return self.input_objects > 0


def _group_objects(top: list, groups) -> List[list]:
    out = []
    for fld, wire, group in top:
        if fld not in groups or wire != _BYTES:
            continue
        for _, cwire, obj in decode_fields(group) or ():
            if cwire == _BYTES:
                out.append(decode_fields(obj) or [])
    return out


def parse_dump(
    msg: bytes,
    buses: "BusMap",
    max_input: Optional[int] = None,
    on_bus_objects: Optional[Callable[[BusObjects], None]] = None,
) -> Dump:
    """Decode a full state dump once.

    ``on_bus_objects`` (bus discovery) runs before the sections are read, so a
    bus it adds to ``buses`` gets its state from this same dump. Every input
    object (up to ``max_input``) and every known bus yields a section; one
    whose section is missing, repeated or untrusted yields an unknown section,
    so a channel never keeps values the dump does not confirm.
    """
    dump = Dump()
    top = decode_fields(msg)
    if top is None:
        log.warning("state dump could not be decoded (%d bytes); no state taken from it", len(msg))
        return dump
    input_objs = _group_objects(top, _DUMP_INPUT_GROUPS)
    bus_objs = _group_objects(top, (_DUMP_BUS_GROUP,))
    dump.input_objects = len(input_objs)
    if not input_objs:
        log.warning("state dump without input channels (%d bytes); link liveness not refreshed", len(msg))

    bus_sections: Dict[int, Optional[bytes]] = {}
    for obj in bus_objs:
        ident = _object_id(obj)
        if ident is None:
            continue
        section = _object_section(obj, _DUMP_BUS_SECTION_FIELD)
        if section is not None and (_strict_fields(section, _BUS_FIELDS) or {}).get(1) == ident:
            name = next((v for f, w, v in obj if f == 2 and w == _BYTES), b"")
            dump.bus_objects.append((ident, name.decode("utf-8", errors="replace").strip()))
            bus_sections[ident] = section
        else:
            bus_sections.setdefault(ident, section)
    if on_bus_objects is not None:
        try:
            on_bus_objects(list(dump.bus_objects))
        except Exception:  # noqa: BLE001 - never let a callback kill the link
            log.exception("bus discovery failed; dump parsed with known buses")

    for obj in input_objs:
        channel = (_object_id(obj) or 0) + 1  # channel 1's index 0 is omitted
        if max_input is not None and channel > max_input:
            continue
        section = _object_section(obj, _DUMP_SECTION_FIELD)
        parsed = _input_section(section) if section is not None else None
        # The section must name the same channel as the object holding it.
        if parsed is None or parsed[0] != channel:
            parsed = _unknown(channel, "dump input object", section or b"")
        dump.sections.append(parsed)
        sections = {}
        for fld, wire, value in obj:
            name = _SETTINGS_FIELDS.get(fld)
            if name and wire == _BYTES and name not in sections:
                sections[name] = value
        if sections:
            dump.settings[channel] = sections

    for ident, section in bus_sections.items():
        if ident not in buses.ids:
            continue
        parsed = _bus_section(section, buses.ids) if section is not None else None
        if parsed is None or parsed[0] != buses.ids[ident]:
            parsed = _unknown(buses.ids[ident], "dump bus object", section or b"")
        dump.sections.append(parsed)
    return dump


def dump_channel_settings(
    msg: bytes, max_input: Optional[int] = None
) -> Dict[int, Dict[str, bytes]]:
    """Raw processing sections of every input channel in a full state dump:
    {channel: {"input": bytes, "eq": bytes, "dynamics": bytes}}. Read-only
    information; nothing here is ever sent back to the mixer."""
    return parse_dump(msg, BusMap(), max_input).settings


def dump_bus_objects(msg: bytes) -> BusObjects:
    """(object id, console name) of every bus in a full state dump that has a
    well-formed fader section naming the same id. Used to discover buses."""
    return parse_dump(msg, BusMap()).bus_objects


def varint_hex(value: int) -> str:
    """Encode a bus object id as the varint hex used in config (128 -> "8001")."""
    out = bytearray()
    while True:
        b = value & 0x7F
        value >>= 7
        out.append(b | (0x80 if value else 0))
        if not value:
            return out.hex()


def _bus_map(buses: Iterable) -> Dict[int, int]:
    """(channel, id_hex) pairs -> {object id: channel}. The id is configured as
    the varint hex used on the wire (e.g. "8001" = 128)."""
    out = {}
    for channel, bus_id in buses:
        value, end = _read_varint(bytes.fromhex(bus_id), 0)
        if value is not None and end == len(bus_id) // 2:
            out[value] = channel
    return out


class BusMap:
    """Bus object id -> channel, shared by every parser of a link (a bus
    discovered at runtime is added with add())."""

    def __init__(self, pairs: Iterable = ()) -> None:
        self.ids: Dict[int, int] = _bus_map(pairs)

    def add(self, channel: int, object_id: int) -> None:
        self.ids[object_id] = channel


DumpListener = Callable[[Dump], None]


def _scan(
    buf: bytes,
    buses: BusMap,
    max_msg: int,
    max_input: Optional[int] = None,
    on_bus_objects: Optional[Callable[[BusObjects], None]] = None,
    dump_listeners: Iterable[DumpListener] = (),
) -> Tuple[List[FrameEvent], int]:
    """Parse every complete frame in buf.

    Returns (events, consumed): consumed is how many leading bytes are fully
    handled and can be dropped (garbage, parsed frames, padding).

    ``max_input`` (the number of input channels) drops input events above it:
    bus channel numbers start right after the inputs, so an input index beyond
    the configured inputs must never be taken for (and overwrite) a bus.
    """
    events: List[FrameEvent] = []
    pos = consumed = 0
    while True:
        start = buf.find(_MAGIC, pos)
        if start == -1:
            # keep a short tail: a magic may be split across reads
            consumed = max(pos, len(buf) - (len(_MAGIC) - 1), 0)
            break
        consumed = start
        if len(buf) - start < _HEADER_LEN:
            break
        ftype, flen = struct.unpack_from(">II", buf, start + len(_MAGIC))
        if flen == 0:
            pos = consumed = start + _HEADER_LEN
            continue
        mlen, mpos = _read_varint(buf, start + _HEADER_LEN)
        if mlen is None and mpos != -1:
            break  # length not complete yet
        if mlen is None or mlen > flen or mlen > max_msg:
            pos = start + 1  # not a frame we understand: resync on next magic
            continue
        end = mpos + mlen
        if len(buf) < end:
            break  # wait for the rest of the message
        msg = buf[mpos:end]
        if ftype in _INPUT_TYPES:
            parsed = [_input_section(msg)]
            if max_input is not None and parsed[0] is not None and parsed[0][0] > max_input:
                parsed = []
        elif ftype in _BUS_TYPES:
            parsed = [_bus_section(msg, buses.ids)]
        elif ftype == _DUMP_TYPE:
            dump = parse_dump(msg, buses, max_input, on_bus_objects)
            for listener in dump_listeners:
                try:
                    listener(dump)
                except Exception:  # noqa: BLE001 - a listener must not kill the link
                    log.exception("state dump listener failed")
            parsed = dump.sections
        else:
            parsed = []
        is_dump = ftype == _DUMP_TYPE
        raw = buf[start:start + _HEADER_LEN].hex() if is_dump else buf[start:end].hex()
        for p in parsed:
            if p is not None:
                channel, muted, fader_hex, solo = p
                events.append(FrameEvent(channel, muted, fader_hex, raw, end * 2,
                                         from_dump=is_dump, solo=solo))
        # Trailing zero padding is skipped by the next magic search.
        pos = consumed = end
    return events, consumed


class StreamBuffer:
    """Accumulates a byte stream and yields complete frames as they arrive.

    TCP is a stream: a read may contain half a frame or several frames. This
    buffers bytes, parses every complete frame, drops what is handled, and caps
    total size so garbage cannot grow it without bound.

    ``buses`` is a BusMap (shared, may grow at runtime) or (channel, id) pairs
    so bus frames are also parsed. ``on_bus_objects`` discovers buses from each
    dump before its sections are read; ``dump_listeners`` get every decoded
    Dump (channel settings, link liveness).
    """

    def __init__(
        self,
        buses=(),
        max_bytes: int = 1 << 20,
        max_input: Optional[int] = None,
        on_bus_objects: Optional[Callable[[BusObjects], None]] = None,
        dump_listeners: Iterable[DumpListener] = (),
    ) -> None:
        self._buf = bytearray()
        self._buses = buses if isinstance(buses, BusMap) else BusMap(buses)
        self._max = max_bytes
        self._max_input = max_input
        self._on_bus_objects = on_bus_objects
        self._dump_listeners = list(dump_listeners)

    @property
    def at_frame_boundary(self) -> bool:
        """True when no partial frame is pending: everything fed so far ends on
        a frame boundary (leftover bytes, if any, contain no frame start).

        The proxy uses this to never inject a command into the middle of a
        frame it is relaying.
        """
        return _MAGIC[:1] not in self._buf

    def feed(self, data: bytes) -> List["FrameEvent"]:
        self._buf.extend(data)
        events, consumed = _scan(bytes(self._buf), self._buses, self._max, self._max_input,
                                 self._on_bus_objects, self._dump_listeners)
        del self._buf[:consumed]
        if len(self._buf) > self._max:
            # a frame claims more than we buffer: drop it, resync on next magic
            del self._buf[: self._buf.find(_MAGIC) + 1]
        return events


def parse_hex_stream(hex_data: str, buses=(), max_input: Optional[int] = None) -> List[FrameEvent]:
    """Find every known mute/fader frame in a hex string.

    Returns events in ascending position order. ``buses`` is an iterable of
    (channel, id) for bus (type 71/72) frames.
    """
    bus_map = buses if isinstance(buses, BusMap) else BusMap(buses)
    events, _ = _scan(bytes.fromhex(hex_data.lower()), bus_map, 1 << 24, max_input)
    return events
