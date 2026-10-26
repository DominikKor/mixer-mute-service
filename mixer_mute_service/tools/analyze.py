"""Decode mixer->ipad 6d frames from a capture log. Read-only analysis."""
import re, struct, sys, collections

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mixer_capture2.log"
lines = open(path).read().splitlines()

frames = []
for ln in lines:
    m = re.search(r"mixer->ipad \d+ bytes: ([0-9a-f]+)", ln)
    if not m:
        continue
    hx = m.group(1)
    # split into individual 6d frames within the chunk
    for fm in re.finditer(r"5a5a5a5a5a5a0000006d000001([0-9a-f]*?)(?=5a5a5a5a5a5a|$)", hx):
        frames.append(fm.group(1))

print(f"total 6d frames: {len(frames)}")

def f32le(h):
    try:
        return round(struct.unpack("<f", bytes.fromhex(h))[0], 2)
    except Exception:
        return None

# Categorize by the first 8 hex chars (4 bytes) after the prefix == "opcode/channel"
cats = collections.Counter(f[:8] for f in frames)
print("\n=== frame openings (first 4 bytes after 6d prefix) x count ===")
for op, n in cats.most_common():
    print(f"  {op}  x{n}")

# For each opening, show a couple of examples with the trailing zeros stripped,
# and decode a trailing 4-byte float candidate.
print("\n=== per-opening detail ===")
for op in cats:
    examples = [f for f in frames if f.startswith(op)][:3]
    print(f"\nopening {op} ({cats[op]} frames):")
    for ex in examples:
        core = ex.rstrip("0")
        if len(core) % 2:
            core += "0"
        # find a '2d' marker and decode the 4 bytes after it
        decoded = ""
        idx = ex.find("2d")
        if idx != -1 and len(ex) >= idx + 2 + 8:
            fv = ex[idx + 2: idx + 10]
            decoded = f"  fader@2d={fv} -> {f32le(fv)}"
        print(f"    {core[:48]}{decoded}")

# Show the full sweep of decoded fader values for the dominant fader-move opening
print("\n=== decoded value sweep per opening (channel detection) ===")
for op in cats:
    vals = []
    for f in frames:
        if not f.startswith(op):
            continue
        idx = f.find("2d")
        if idx != -1 and len(f) >= idx + 10:
            vals.append(f32le(f[idx + 2: idx + 10]))
    uniq = [v for i, v in enumerate(vals) if i == 0 or v != vals[i - 1]]
    if len(uniq) > 1:
        print(f"  {op}: {uniq}")
