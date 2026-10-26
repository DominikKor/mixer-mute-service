"""Bucket 6d '15'-type (muted fader-move) frames by time gaps, to attribute each
burst to a channel the user moved in order. Read-only."""
import re, struct, sys

path = sys.argv[1] if len(sys.argv) > 1 else "/tmp/mixer_capture2.log"
GAP = 2.5  # seconds; a gap larger than this starts a new channel bucket

def f32le(h):
    try: return round(struct.unpack("<f", bytes.fromhex(h))[0], 1)
    except Exception: return None

rows = []  # (epoch, opcode, fader)
for ln in open(path):
    m = re.search(r"^(\d\d:\d\d:\d\d),(\d+).*mixer->ipad \d+ bytes: ([0-9a-f]+)", ln)
    if not m:
        m2 = re.search(r"(\d\d):(\d\d):(\d\d),(\d+).*mixer->ipad \d+ bytes: ([0-9a-f]+)", ln)
        if not m2: continue
        hh,mm,ss,ms,hx = m2.groups()
        t = int(hh)*3600+int(mm)*60+int(ss)+int(ms)/1000
    else:
        hms, ms, hx = m.groups()
        continue
    for fm in re.finditer(r"5a5a5a5a5a5a0000006d000001(0[0-9a-f])1500abc93d2d([0-9a-f]{8})", hx):
        rows.append((t, fm.group(1), f32le(fm.group(2))))

rows.sort()
print(f"{len(rows)} '15'-type frames")
buckets = []
for t, op, fv in rows:
    if not buckets or t - buckets[-1][-1][0] > GAP:
        buckets.append([])
    buckets[-1].append((t, op, fv))

print(f"\n{len(buckets)} time-separated bursts (in order):")
for i, b in enumerate(buckets, 1):
    ops = sorted({x[1] for x in b})
    faders = [x[2] for x in b]
    print(f"  burst #{i}: opcode(s)={ops}  frames={len(b)}  fader {faders[0]}..{faders[-1]}")
