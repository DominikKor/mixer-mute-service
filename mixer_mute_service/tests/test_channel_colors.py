"""Strip colours (GET /channel-colors, POST /channels/{n}/color).

Verifies: empty at first, a colour is stored and listed, it survives a reload
from the file, null clears it, unknown colours and invalid channels are
refused with 400 without touching the file, and a damaged file reads as empty.
"""

import json
import os
import pathlib
import tempfile

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="mixer_colors_"))
(_tmp / "config.yaml").write_text("mixer:\n  max_channels: 8\n")
os.environ["MIXER_CONFIG"] = str(_tmp / "config.yaml")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def main():
    store = _tmp / "channel_colors.json"
    with TestClient(app) as client:
        print("== empty ==")
        r = client.get("/channel-colors").json()
        check("no colours yet", r["colors"] == {})
        check("palette offered", "green" in r["palette"] and len(r["palette"]) == 8)

        print("== set / list / clear ==")
        r = client.post("/channels/2/color", json={"color": "green"})
        check("set green on ch 2", r.status_code == 200 and r.json() == {"ok": True, "channel": 2, "color": "green"})
        client.post("/channels/5/color", json={"color": "red"})
        check("listed", client.get("/channel-colors").json()["colors"] == {"2": "green", "5": "red"})
        check("stored in channel_colors.json", json.loads(store.read_text()) == {"2": "green", "5": "red"})
        r = client.post("/channels/2/color", json={"color": None})
        check("null clears", r.status_code == 200 and client.get("/channel-colors").json()["colors"] == {"5": "red"})

        print("== refused ==")
        before = store.read_text()
        r = client.post("/channels/5/color", json={"color": "#00ff00"})
        check("unknown colour -> 400", r.status_code == 400 and r.json()["reason"] == "unknown color")
        r = client.post("/channels/99/color", json={"color": "green"})
        check("invalid channel -> 400", r.status_code == 400 and r.json()["reason"] == "invalid channel")
        check("file untouched", store.read_text() == before)

        print("== damaged file ==")
        store.write_text("{not json")
        check("reads as empty", client.get("/channel-colors").json()["colors"] == {})
        store.write_text(json.dumps({"3": "green", "4": "chartreuse", "x": "red"}))
        check("only valid entries kept", client.get("/channel-colors").json()["colors"] == {"3": "green"})

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
