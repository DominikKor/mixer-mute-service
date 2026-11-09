"""Summarise mixer->app 6d frames from a capture log. Read-only analysis.

Reads the RAW lines that tools/capture_proxy.py writes
("<time> S<n> mixer->app RAW <len>: <hex>") and, for older logs, the former
"mixer->ipad <len> bytes: <hex>" lines. The chunks of each session are joined
before splitting, so frames cut across TCP reads are found whole.

Usage:
    python -m tools.analyze captures/<file>.log
"""

import collections
import re
import struct
import sys

_RAW = re.compile(r"(?:(S\d+) mixer->app RAW \d+|mixer->ipad \d+ bytes): ([0-9a-f]+)")
_FRAME_6D = re.compile(r"5a5a5a5a5a5a0000006d000001([0-9a-f]*?)(?=5a5a5a5a5a5a|$)")


def f32le(h):
    try:
        return round(struct.unpack("<f", bytes.fromhex(h))[0], 2)
    except (ValueError, struct.error):
        return None


def read_frames(path):
    streams = collections.defaultdict(list)
    with open(path) as fh:
        for line in fh:
            m = _RAW.search(line)
            if m:
                streams[m.group(1) or "legacy"].append(m.group(2))
    frames = []
    for chunks in streams.values():
        frames.extend(fm.group(1) for fm in _FRAME_6D.finditer("".join(chunks)))
    return frames


def fader_after_2d(frame):
    idx = frame.find("2d")
    if idx != -1 and len(frame) >= idx + 10:
        return frame[idx + 2: idx + 10]
    return None


def main():
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    frames = read_frames(sys.argv[1])
    print(f"total 6d frames: {len(frames)}")

    # The first 4 bytes after the prefix are the "opcode/channel" opening.
    cats = collections.Counter(f[:8] for f in frames)
    print("\n=== frame openings (first 4 bytes after 6d prefix) x count ===")
    for op, n in cats.most_common():
        print(f"  {op}  x{n}")

    print("\n=== per-opening detail (trailing zeros stripped) ===")
    for op in cats:
        print(f"\nopening {op} ({cats[op]} frames):")
        for ex in [f for f in frames if f.startswith(op)][:3]:
            core = ex.rstrip("0")
            if len(core) % 2:
                core += "0"
            fv = fader_after_2d(ex)
            decoded = f"  fader@2d={fv} -> {f32le(fv)}" if fv else ""
            print(f"    {core[:48]}{decoded}")

    print("\n=== decoded value sweep per opening (channel detection) ===")
    for op in cats:
        vals = [f32le(fv) for f in frames if f.startswith(op) for fv in [fader_after_2d(f)] if fv]
        uniq = [v for i, v in enumerate(vals) if i == 0 or v != vals[i - 1]]
        if len(uniq) > 1:
            print(f"  {op}: {uniq}")


if __name__ == "__main__":
    main()
