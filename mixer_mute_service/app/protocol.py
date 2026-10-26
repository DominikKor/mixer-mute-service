"""Protocol layer: build outgoing commands and parse incoming mixer frames.

All knowledge here is reverse-engineered from captured TCP payloads and is
deliberately conservative. The most important rule of the whole project lives
here implicitly: a mute/unmute command *always* rewrites the fader value, so we
only ever build a command from a fader value that was actually observed.

The parser is byte based. Frames look like a length-delimited protobuf message
behind a fixed header (inferred from captures, not officially documented):

    5a5a5a5a5a5a | type u32 BE | length u32 BE | varint msg_len | message

Status frames (6d/71) are zero padded to their length. Only messages whose
fields are all understood update state; anything else is ignored.
"""

from __future__ import annotations

import math
import struct
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Tuple

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

# Header(s) for status/broadcast frames (Mixer -> iPad). The mixer does NOT
# accept these as control commands, but they carry the fader/mute state we need.
STATUS_PREFIX_6D = "5a5a5a5a5a5a0000006d00000100"

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


def is_valid_fader_hex(value: object) -> bool:
    """A valid fader value is exactly 8 lowercase-or-uppercase hex chars (4 bytes)."""
    if not isinstance(value, str):
        return False
    if len(value) != 8:
        return False
    try:
        bytes.fromhex(value)
    except ValueError:
        return False
    return True


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


def build_mute_command(channel: int, muted: bool, fader_hex: str, max_channels: int = 8) -> bytes:
    """Build a full App -> Mixer frame.

    Mute:   PREFIX + 090908 XX 20012d FFFFFFFF
    Unmute: PREFIX + 070708 XX     2d FFFFFFFF

    XX = channel - 1 (zero based), FFFFFFFF = current observed fader value.
    A fader at position 0 (-inf) omits the 2d field, as the app does.
    """
    if not 1 <= channel <= max_channels:
        raise ValueError(f"Invalid channel: {channel} (1..{max_channels})")
    fader = _fader_field("2d", fader_hex)

    # Channel-identifying field: either the special object id or "08 XX".
    if channel in CHANNEL_ID_FIELDS:
        ident = CHANNEL_ID_FIELDS[channel]
    else:
        ident = f"08{channel - 1:02x}"

    # Body without the leading length marker.
    body = f"{ident}2001{fader}" if muted else f"{ident}{fader}"
    # The frame is prefixed by the payload length, repeated twice (LL LL),
    # e.g. channel 2 mute = 09 09 .., channel 1 unmute = 0a 0a ..
    length = len(body) // 2
    payload = f"{length:02x}{length:02x}{body}"

    return bytes.fromhex(COMMAND_PREFIX + payload)


def build_bus_command(bus_id: str, muted: bool, fader_hex: str) -> bytes:
    """Build a bus mute/unmute command (type 72).

    Mute:   72.. <LL LL> 08<bus_id> 18 01 3d <fader>
    Unmute: 72.. <LL LL> 08<bus_id> 3d <fader>
    """
    fader = _fader_field("3d", fader_hex)
    ident = f"08{bus_id}"
    body = f"{ident}1801{fader}" if muted else f"{ident}{fader}"
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
    raw_match: str
    end_hex_index: int  # index into the hex string just past this frame
    # True if this came from a full state dump (see StateStore.apply_event).
    from_dump: bool = False


_MAGIC = bytes.fromhex(MAGIC)
_HEADER_LEN = 14  # magic(6) + type u32 + length u32

# Message types that carry exactly one fader section.
_INPUT_TYPES = (0x6E, 0x6D)  # command (app -> mixer) / status (mixer -> app)
_BUS_TYPES = (0x72, 0x71)
# Full state dump the mixer sends after the subscribe handshake.
_DUMP_TYPE = 0x01

# Protobuf wire types.
_VARINT, _FIXED32, _BYTES = 0, 5, 2

# Input fader section (also field 7 of every channel object in the dump):
#   1 = channel index (omitted for index 0), 2 = extra fixed32 only seen on
#   channel 1, 4 = muted, 5 = fader. Omitted fields are 0 (protobuf defaults).
_INPUT_FIELDS = {1: _VARINT, 2: _FIXED32, 4: _VARINT, 5: _FIXED32}
# Bus fader section: 1 = bus object id, 3 = muted, 7 = fader.
_BUS_FIELDS = {1: _VARINT, 3: _VARINT, 7: _FIXED32}
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


def _decode_fields(msg: bytes) -> Optional[List[Tuple[int, int, object]]]:
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
        field, wire = key >> 3, key & 7
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
        fields.append((field, wire, value))
    return fields


def _strict_fields(msg: bytes, schema: Dict[int, int]) -> Optional[Dict[int, object]]:
    """Fields of msg as {field: value}, only if every field is in schema with
    the expected wire type and appears once. Otherwise None (not this shape)."""
    fields = _decode_fields(msg)
    if fields is None:
        return None
    out: Dict[int, object] = {}
    for field, wire, value in fields:
        if schema.get(field) != wire or field in out:
            return None
        out[field] = value
    return out


def _input_section(msg: bytes) -> Optional[Tuple[int, Optional[bool], Optional[str]]]:
    """Interpret an input fader section -> (channel, muted, fader_hex).

    Returns None if the message is not a fader section. Returns muted/fader as
    None (= make the channel unknown) if it is one but cannot be trusted.
    """
    f = _strict_fields(msg, _INPUT_FIELDS)
    if f is None:
        return None
    index = f.get(1, 0)
    mute = f.get(4, 0)
    if index > 63 or mute not in (0, 1):
        return None
    channel = index + 1
    fader_hex = f[5].hex() if 5 in f else _ZERO_FADER
    # The extra field must be exactly what build_mute_command re-sends for this
    # channel, otherwise a command from us would change it.
    extra = ("15" + f[2].hex()) if 2 in f else None
    if extra != CHANNEL_ID_FIELDS.get(channel) or not is_safe_fader_hex(fader_hex):
        return channel, None, None
    return channel, bool(mute), fader_hex


def _bus_section(msg: bytes, buses: Dict[int, int]) -> Optional[Tuple[int, Optional[bool], Optional[str]]]:
    """Interpret a bus fader section -> (channel, muted, fader_hex) for a
    configured bus, or None."""
    f = _strict_fields(msg, _BUS_FIELDS)
    if f is None or 1 not in f or f[1] not in buses:
        return None
    channel = buses[f[1]]
    mute = f.get(3, 0)
    fader_hex = f[7].hex() if 7 in f else _ZERO_FADER
    if mute not in (0, 1) or not is_safe_fader_hex(fader_hex):
        return channel, None, None
    return channel, bool(mute), fader_hex


def _dump_objects(msg: bytes, groups) -> Iterable[Tuple[int, Optional[bytes]]]:
    """(object id, fader section) for every object in the given top-level dump
    groups. The section is None unless the object has exactly one."""
    for field, wire, group in _decode_fields(msg) or ():
        if field not in groups or wire != _BYTES:
            continue
        section_field = _DUMP_BUS_SECTION_FIELD if field == _DUMP_BUS_GROUP else _DUMP_SECTION_FIELD
        for _, cwire, obj in _decode_fields(group) or ():
            if cwire != _BYTES:
                continue
            obj_fields = _decode_fields(obj) or []
            ident = next((v for f, w, v in obj_fields if f == 1 and w == _VARINT), 0)
            sections = [v for f, w, v in obj_fields if f == section_field and w == _BYTES]
            yield ident, sections[0] if len(sections) == 1 else None


def _dump_sections(msg: bytes, buses: Optional[Dict[int, int]] = None) -> List[Tuple[int, Optional[bool], Optional[str]]]:
    """Fader sections of all input channels and configured buses in a full
    state dump. ``buses`` maps bus object id -> channel (see _bus_map)."""
    out = []
    for index, section in _dump_objects(msg, _DUMP_INPUT_GROUPS):
        parsed = _input_section(section) if section is not None else None
        # The section must name the same channel as the object holding it.
        if parsed is not None and parsed[0] == index + 1:
            out.append(parsed)
    for bus_id, section in _dump_objects(msg, (_DUMP_BUS_GROUP,)):
        if section is None or bus_id not in (buses or {}):
            continue
        # The section must name the same bus as the object holding it.
        if (_strict_fields(section, _BUS_FIELDS) or {}).get(1) != bus_id:
            continue
        parsed = _bus_section(section, buses)
        if parsed is not None:
            out.append(parsed)
    return out


def dump_bus_objects(msg: bytes) -> List[Tuple[int, str]]:
    """(object id, console name) of every bus in a full state dump that has a
    well-formed fader section naming the same id. Used to discover buses."""
    out = []
    for field, wire, group in _decode_fields(msg) or ():
        if field != _DUMP_BUS_GROUP or wire != _BYTES:
            continue
        for _, cwire, obj in _decode_fields(group) or ():
            if cwire != _BYTES:
                continue
            obj_fields = _decode_fields(obj) or []
            ident = next((v for f, w, v in obj_fields if f == 1 and w == _VARINT), None)
            name = next((v for f, w, v in obj_fields if f == 2 and w == _BYTES), b"")
            sections = [v for f, w, v in obj_fields if f == _DUMP_BUS_SECTION_FIELD and w == _BYTES]
            if ident is None or len(sections) != 1:
                continue
            if (_strict_fields(sections[0], _BUS_FIELDS) or {}).get(1) != ident:
                continue
            out.append((ident, name.decode("utf-8", errors="replace").strip()))
    return out


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
    """Bus object id -> channel, shared by every parser of a link.

    ``on_dump`` (optional) is called with dump_bus_objects() of every full state
    dump *before* its sections are interpreted, so a bus it adds via add() gets
    its state from that same dump.
    """

    def __init__(self, pairs: Iterable = (), on_dump=None) -> None:
        self.ids: Dict[int, int] = _bus_map(pairs)
        self.on_dump = on_dump

    def add(self, channel: int, object_id: int) -> None:
        self.ids[object_id] = channel


def _as_bus_map(buses) -> BusMap:
    return buses if isinstance(buses, BusMap) else BusMap(buses)


def _scan(buf: bytes, buses: BusMap, max_msg: int) -> Tuple[List[FrameEvent], int]:
    """Parse every complete frame in buf.

    Returns (events, consumed): consumed is how many leading bytes are fully
    handled and can be dropped (garbage, parsed frames, padding).
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
        elif ftype in _BUS_TYPES:
            parsed = [_bus_section(msg, buses.ids)]
        elif ftype == _DUMP_TYPE:
            if buses.on_dump is not None:
                buses.on_dump(dump_bus_objects(msg))
            parsed = _dump_sections(msg, buses.ids)
        else:
            parsed = []
        raw = buf[start:end].hex()
        for p in parsed:
            if p is not None:
                channel, muted, fader_hex = p
                events.append(FrameEvent(channel, muted, fader_hex, raw, end * 2,
                                         from_dump=ftype == _DUMP_TYPE))
        # Trailing zero padding is skipped by the next magic search.
        pos = consumed = end
    return events, consumed


class StreamBuffer:
    """Accumulates a byte stream and yields complete frames as they arrive.

    TCP is a stream: a read may contain half a frame or several frames. This
    buffers bytes, parses every complete frame, drops what is handled, and caps
    total size so garbage cannot grow it without bound.

    ``buses`` is a BusMap (shared, may grow at runtime) or (channel, id) pairs
    so bus frames are also parsed.
    """

    def __init__(self, buses=(), max_bytes: int = 1 << 20) -> None:
        self._buf = bytearray()
        self._buses = _as_bus_map(buses)
        self._max = max_bytes

    def feed(self, data: bytes) -> List["FrameEvent"]:
        self._buf.extend(data)
        events, consumed = _scan(bytes(self._buf), self._buses, self._max)
        del self._buf[:consumed]
        if len(self._buf) > self._max:
            # a frame claims more than we buffer: drop it, resync on next magic
            del self._buf[: self._buf.find(_MAGIC) + 1]
        return events


def parse_hex_stream(hex_data: str, buses=()) -> List[FrameEvent]:
    """Find every known mute/fader frame in a hex string.

    Returns events in ascending position order. ``buses`` is an iterable of
    (channel, id) for bus (type 71/72) frames.
    """
    events, _ = _scan(bytes.fromhex(hex_data.lower()), _as_bus_map(buses), 1 << 24)
    return events
