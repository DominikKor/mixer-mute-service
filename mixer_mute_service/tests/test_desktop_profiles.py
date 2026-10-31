"""Desktop profiles added/edited from the UI are spliced into config.yaml.

Verifies: adding a profile only inserts lines (every other byte of the real
config.yaml stays), editing changes only the edited values and keeps comments
and unknown keys, missing/empty sections are created, CRLF and BOM survive,
invalid or unsafe changes are refused without touching the file, and the HTTP
endpoints update the buttons without a restart.

Run: python -m tests.test_desktop_profiles
"""

import difflib
import os
import pathlib
import re
import shutil
import sys
import tempfile

import yaml

from app.config import DesktopProfileConfig, load_config
from app.desktop_profiles import ProfileEditError, add_profile, delete_profile, update_profile

PASS = 0
FAIL = 0
ROOT = pathlib.Path(__file__).resolve().parent.parent
_tmp = pathlib.Path(tempfile.mkdtemp(prefix="mixer_profiles_"))


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def cfg_file(name, text=None, raw=None):
    path = _tmp / name / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        path.write_bytes(raw)
    elif text is not None:
        path.write_text(text)
    return path


def refused(fn, *args):
    """(status, reason) of a refused change, or None if it went through."""
    try:
        fn(*args)
    except ProfileEditError as exc:
        return exc.status, str(exc)
    return None


OBS = DesktopProfileConfig(
    title="OBS Studio", kind="program",
    path=r"C:\Program Files\obs-studio\bin\64bit\obs64.exe",
    args=["--startstreaming", "--scene", "Begrüßung"],
    target_ip="192.168.188.12", fullscreen=False, position="bottom-right",
)
QR = DesktopProfileConfig(
    title="Gemeindegaben-QR", kind="image",
    path=r"I:\Googledrive\Streamingbilder\GemeindegabenQR.png",
    target_ip="192.168.188.11", fullscreen=True, monitor=2,
)


print("== add to the real config.yaml ==")
real = cfg_file("real")
shutil.copy(ROOT / "config.yaml", real)
before = real.read_bytes()
add_profile(real, "obs", OBS)
after = real.read_bytes()
# Find the inserted block: all original bytes must be kept, in order.
prefix = 0
while prefix < len(before) and before[prefix] == after[prefix]:
    prefix += 1
inserted = len(after) - len(before)
check("only lines were inserted", after[:prefix] + after[prefix + inserted:] == before)
check("inserted block is the new profile", after[prefix:prefix + inserted].decode().lstrip().startswith("obs:"))
check(".bak holds the previous version", (real.parent / "config.yaml.bak").read_bytes() == before)
loaded = load_config(real)
check("new profile loads as written", loaded.desktop_display.profiles["obs"] == OBS)
check("existing profile untouched", loaded.desktop_display.profiles["gemeindegaben"] == QR)
check("non-ASCII written as escapes (file stays ASCII)", after.isascii())
old_cfg = yaml.safe_load(before)
new_cfg = yaml.safe_load(after)
new_cfg["desktop_display"]["profiles"].pop("obs")
check("everything else in the config is unchanged", new_cfg == old_cfg)

print("== edit in place ==")
text = real.read_text()
changed = QR.model_copy(update={"title": "QR-Code", "fullscreen": False, "position": "center"})
update_profile(real, "gemeindegaben", changed)
edited = real.read_text()
diff = list(difflib.ndiff(text.splitlines(), edited.splitlines()))
removed = [l[2:] for l in diff if l.startswith("- ")]
added = [l[2:] for l in diff if l.startswith("+ ")]
check("only title and fullscreen lines replaced",
      [l.strip() for l in removed] == ['title: "Gemeindegaben-QR"', "fullscreen: true"])
check("changed values + new position line",
      sorted(l.strip() for l in added) == ['fullscreen: false', 'position: "center"', 'title: "QR-Code"'])
check("edited profile loads as saved", load_config(real).desktop_display.profiles["gemeindegaben"] == changed)
mtime = real.stat().st_mtime_ns
update_profile(real, "gemeindegaben", changed)
check("saving without changes leaves the file alone", real.stat().st_mtime_ns == mtime and real.read_text() == edited)

print("== edit keeps comments, unknown keys and layouts ==")
commented = cfg_file("commented", (
    "desktop_display:\n"
    "  profiles:\n"
    "    # the QR code for the offering\n"
    "    qr:\n"
    "      title: QR   # shown on the button\n"
    "      kind: image\n"
    "      path: 'C:\\qr.png'\n"
    "      note: hand-written key\n"
    "      args:\n"
    "        - a\n"
    "        - b\n"
    "      # end of qr\n"
    "groups:\n"
    "  all:\n"
    "    channels: [1, 2]\n"
))
update_profile(commented, "qr", DesktopProfileConfig(
    title="QR neu", kind="program", path=r"C:\qr.exe", args=["x"], monitor=3))
t = commented.read_text()
check("inline comment kept", "title: \"QR neu\"   # shown on the button" in t)
check("comment lines kept", "# the QR code for the offering" in t and "# end of qr" in t)
check("unknown key kept", yaml.safe_load(t)["desktop_display"]["profiles"]["qr"]["note"] == "hand-written key")
check("block list replaced", yaml.safe_load(t)["desktop_display"]["profiles"]["qr"]["args"] == ["x"])
check("groups still follow", t.rstrip().endswith("channels: [1, 2]"))
check("monitor added inside the profile", load_config(commented).desktop_display.profiles["qr"].monitor == 3)

print("== missing or empty sections ==")
for name, text in [
    ("no-section", "mixer:\n  max_channels: 8\n"),
    ("no-newline", "mixer:\n  max_channels: 8"),
    ("no-profiles", "desktop_display:\n  enabled: false  # off\ngroups: {}\n"),
    ("empty-profiles", "desktop_display:\n  enabled: true\n  profiles: {}   # none yet\n"),
    ("null-profiles", "desktop_display:\n  profiles:\nserver:\n  port: 8765\n"),
    ("empty-section", "desktop_display:\nserver:\n  port: 8765\n"),
    ("empty-file", ""),
]:
    path = cfg_file(name, text)
    add_profile(path, "obs", OBS)
    cfg = load_config(path)
    check(f"{name}: profile added", cfg.desktop_display.profiles.get("obs") == OBS)
    old = yaml.safe_load(text) or {}
    new = yaml.safe_load(path.read_text())
    new["desktop_display"]["profiles"].pop("obs")
    old_dd = old.get("desktop_display") or {}
    check(f"{name}: rest unchanged",
          {k: v for k, v in new.items() if k != "desktop_display"} == {k: v for k, v in old.items() if k != "desktop_display"}
          and {k: v for k, v in new["desktop_display"].items() if k != "profiles"} == {k: v for k, v in old_dd.items() if k != "profiles"})
missing = cfg_file("missing")
missing.unlink(missing_ok=True)
add_profile(missing, "obs", OBS)
check("missing file: created with the profile", load_config(missing).desktop_display.profiles["obs"] == OBS)
second = cfg_file("second", "desktop_display:\n  profiles:\n    a:\n        title: A\n        kind: file\n        path: 'C:\\a.txt'\n")
add_profile(second, "b", DesktopProfileConfig(title="B", kind="file", path=r"C:\b.txt"))
check("indentation follows the existing profiles", "    b:\n        title: \"B\"\n" in second.read_text())
multiple = cfg_file("delete-multiple", "desktop_display:\n  enabled: true\n  profiles:\n    first:\n      title: First\n      kind: file\n      path: 'C:\\first.txt'\n    second:\n      title: Second\n      kind: file\n      path: 'C:\\second.txt'\nserver:\n  host: '127.0.0.1'\n  port: 8765\n")
delete_profile(multiple, "first")
remaining = load_config(multiple)
check("delete keeps other profile", set(remaining.desktop_display.profiles) == {"second"})
check("delete keeps unrelated config", remaining.server.host == "127.0.0.1" and remaining.server.port == 8765)
last = cfg_file("delete-last", "desktop_display:\n  enabled: true\n  profiles:\n    only:\n      title: Only\n      kind: file\n      path: 'C:\\only.txt'\nserver:\n  host: '127.0.0.1'\n  port: 8765\n")
delete_profile(last, "only")
check("delete last profile leaves valid empty map", load_config(last).desktop_display.profiles == {})
original = multiple.read_bytes()
refused_delete = refused(delete_profile, multiple, "unknown")
check("unknown profile delete -> 404", refused_delete is not None and refused_delete[0] == 404)
check("unknown profile delete leaves file unchanged", multiple.read_bytes() == original)

print("== CRLF and BOM ==")
crlf = cfg_file("crlf", raw=b"\xef\xbb\xbfdesktop_display:\r\n  profiles:\r\n    a:\r\n      title: A\r\n      kind: file\r\n      path: 'C:\\a.txt'\r\n")
add_profile(crlf, "b", DesktopProfileConfig(title="B", kind="file", path=r"C:\b.txt"))
update_profile(crlf, "a", DesktopProfileConfig(title="A2", kind="file", path=r"C:\a.txt", monitor=2))
raw = crlf.read_bytes()
check("BOM kept", raw.startswith(b"\xef\xbb\xbf"))
check("only CRLF line breaks", b"\n" not in raw.replace(b"\r\n", b""))
check("both profiles load", set(load_config(crlf).desktop_display.profiles) == {"a", "b"})

print("== refused changes leave the file alone ==")
guard = cfg_file("guard", (ROOT / "config.yaml").read_text())
original = guard.read_bytes()
bad = [
    ("duplicate id", add_profile, "gemeindegaben", QR, 409),
    ("unknown id", update_profile, "nope", QR, 404),
    ("bad id", add_profile, "../x", QR, 400),
    ("relative path", add_profile, "x", QR.model_copy(update={"path": "bild.png"}), 400),
    ("program without .exe", add_profile, "x", OBS.model_copy(update={"path": r"C:\run.bat"}), 400),
    ("args on an image", add_profile, "x", QR.model_copy(update={"args": ["-x"]}), 400),
    ("line break in title", add_profile, "x", QR.model_copy(update={"title": "a\nb: c"}), 400),
    ("empty title", add_profile, "x", QR.model_copy(update={"title": "  "}), 400),
]
for name, fn, pid, profile, status in bad:
    r = refused(fn, guard, pid, profile)
    check(f"{name} -> {status}", r is not None and r[0] == status)
check("file unchanged after refusals", guard.read_bytes() == original)
flow = cfg_file("flow", "desktop_display:\n  profiles:\n    a: {title: A, kind: file, path: 'C:\\a.txt'}\n")
r = refused(update_profile, flow, "a", DesktopProfileConfig(title="A2", kind="file", path=r"C:\a.txt"))
check("flow-style profile is not edited", r is not None and r[0] == 409)
r = refused(add_profile, cfg_file("broken", "desktop_display: [\n"), "x", QR)
check("unparseable config refused", r is not None and r[0] == 409)
numeric = cfg_file("numeric", "mixer:\n  max_channels: 8\n")
add_profile(numeric, "123", QR)
check("numeric id is quoted", "123" in load_config(numeric).desktop_display.profiles)

print("== HTTP ==")
# Never reach the real mixer: point the copied config at a closed local port.
http_text = (ROOT / "config.yaml").read_text()
http_text = re.sub(r'(?m)^  ip: .*$', '  ip: "127.0.0.1"', http_text, count=1)
http_text = re.sub(r'(?m)^  port: 6400$', '  port: 9', http_text, count=1)
http_cfg = cfg_file("http", http_text)
os.environ["MIXER_CONFIG"] = str(http_cfg)
from fastapi.testclient import TestClient  # noqa: E402
from app.main import app  # noqa: E402

with TestClient(app) as client:
    body = {"id": "obs", **OBS.model_dump()}
    r = client.post("/desktop/profiles", json=body)
    check("create 200", r.status_code == 200 and r.json()["ok"] is True)
    listed = {p["id"]: p for p in client.get("/desktop/profiles").json()["profiles"]}
    check("button listed without restart", listed["obs"]["title"] == "OBS Studio")
    check("list has path and args for editing",
          listed["obs"]["path"] == OBS.path and listed["obs"]["args"] == OBS.args)
    check("duplicate create -> 409", client.post("/desktop/profiles", json=body).status_code == 409)
    upd = {**OBS.model_dump(), "title": "OBS", "monitor": 2}
    check("PUT update 200", client.put("/desktop/profiles/obs", json=upd).status_code == 200)
    check("POST update 200",
          client.post("/desktop/profiles/obs/update", json={**upd, "position": "center"}).status_code == 200)
    listed = {p["id"]: p for p in client.get("/desktop/profiles").json()["profiles"]}
    check("update visible", listed["obs"]["title"] == "OBS" and listed["obs"]["monitor"] == 2
          and listed["obs"]["position"] == "center")
    r = client.post("/desktop/profiles", json={**body, "id": "ip", "target_ip": "300.1.1.1"})
    check("invalid IP -> 400 with reason", r.status_code == 400 and "target_ip" in r.json()["reason"])
    r = client.post("/desktop/profiles", json={**body, "id": "k", "kind": "script"})
    check("invalid kind -> 400", r.status_code == 400)
    check("missing id -> 400", client.post("/desktop/profiles", json=OBS.model_dump()).status_code == 400)
    check("update unknown -> 404", client.put("/desktop/profiles/nope", json=upd).status_code == 404)
    text = http_cfg.read_text()
    check("manual comments still in the config", "# Must stay false." in text and "freshness_mode" in text)
    delete_response = client.delete("/desktop/profiles/obs")
    check("DELETE profile 200", delete_response.status_code == 200 and delete_response.json()["deleted"] is True)
    check("deleted profile removed without restart",
        all(profile["id"] != "obs" for profile in client.get("/desktop/profiles").json()["profiles"]))
    check("DELETE unknown profile -> 404", client.delete("/desktop/profiles/nope").status_code == 404)

print(f"\n{PASS} passed, {FAIL} failed")
sys.exit(1 if FAIL else 0)
