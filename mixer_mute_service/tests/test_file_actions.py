"""Files opened straight from a program row (app/file_actions.py), agent stubbed.

Verifies: "file:<ip>|<path>" values are checked when a row is saved, starting
the row plays music on the target PC (/audio), starts .exe files and opens
other files (/display), a missing file is reported, and the folder browser
refuses requests that do not come from the streaming PC itself.
"""

import io
import os
import urllib.error

from tests.helpers import use_app_config  # noqa: E402

_tmp = use_app_config("mixer_files_",
    "desktop_display:\n  enabled: true\n  token_env: FILE_TEST_TOKEN\n  profiles: {}\n")
os.environ["FILE_TEST_TOKEN"] = "test-token"

from fastapi.testclient import TestClient  # noqa: E402

import app.file_actions as file_actions  # noqa: E402
import app.display_agent as display_agent  # noqa: E402
from app.main import app  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


class FakeAgent:
    def __init__(self):
        self.calls = []
        self.missing = False

    def __call__(self, url, token, payload, timeout):
        self.calls.append((url, payload))
        if self.missing:
            raise urllib.error.HTTPError(url, 503, "Configured target is not available", {}, io.BytesIO(b""))
        return {"ok": True}


def main():
    agent = FakeAgent()
    display_agent._post = agent
    row = {"time": "10:00", "title": "Einspieler", "group": ""}
    with TestClient(app) as client:
        print("== saving ==")
        for name, value in [
            ("missing separator", "file:127.0.0.1"),
            ("bad PC address", "file:pc11|C:\\Musik\\a.mp3"),
            ("relative path", "file:127.0.0.1|Musik\\a.mp3"),
            ("path too long", "file:127.0.0.1|C:\\" + "a" * 500),
            ("internet address", "file:8.8.8.8|C:\\Musik\\a.mp3"),
            ("program on a network share", "file:192.168.188.11|\\\\server\\share\\Start.exe"),
        ]:
            r = client.post("/program/items", json={**row, "playback": value})
            check(f"{name} refused", r.status_code == 400 and r.json()["reason"].startswith("playback:"))
        long_path = "I:\\GoogleDrive\\Musikbeiträge\\" + "Sehr langer Ordnername\\" * 4 + "Hintergrundmusik.mp3"
        r = client.post("/program/items", json={
            **row, "playback": "file:127.0.0.1|" + long_path,
            "translation": "file:192.168.188.11|C:\\Programme\\Uhr\\Start.exe",
            "lyrics": "file:192.168.188.11|C:\\Users\\Beamer\\Desktop\\Uhr.url",
        })
        check("row with three file actions saved (paths longer than key IDs)",
              r.status_code == 200 and r.json()["item"]["playback"].endswith("Hintergrundmusik.mp3"))
        item_id = r.json()["item"]["id"]

        print("== starting ==")
        r = client.post(f"/program/items/{item_id}/play")
        check("row started", r.status_code == 200 and r.json()["ok"] is True)
        (u1, p1), (u2, p2), (u3, p3) = agent.calls
        check("music plays on this PC via /audio",
              u1 == "http://127.0.0.1:8766/audio" and p1 == {"id": "program-file", "action": "play", "path": long_path})
        check(".exe starts on the target PC",
              u2 == "http://192.168.188.11:8766/display" and p2["kind"] == "program" and p2["path"] == "C:\\Programme\\Uhr\\Start.exe")
        check("other files open with the default app",
              u3 == "http://192.168.188.11:8766/display" and p3["kind"] == "file" and p3["title"] == "Uhr.url"
              and set(p3) == {"id", "title", "kind", "path", "args", "fullscreen", "monitor", "position"})
        agent.missing = True
        r = client.post(f"/program/items/{item_id}/play")
        actions = r.json().get("linked_actions", [])
        check("missing file reported", r.status_code == 502 and actions and actions[0]["reason"] == "file not found on the target PC")
        agent.missing = False

        print("== folder browser ==")
        check("not offered to other devices", client.get("/files/browse-available").json()["available"] is False)
        r = client.get("/files/browse", params={"path": str(_tmp)})
        check("other devices refused (403)", r.status_code == 403)
        file_actions._from_this_pc = lambda request: True
        (_tmp / "Musik").mkdir()
        (_tmp / "Musik" / "Intro.MP3").write_bytes(b"")
        (_tmp / "Musik" / "Tool.exe").write_bytes(b"")
        (_tmp / "Musik" / ".versteckt").write_bytes(b"")
        r = client.get("/files/browse", params={"path": str(_tmp / "Musik")})
        data = r.json()
        check("folder listed with kinds, hidden files skipped",
              r.status_code == 200 and [(f["name"], f["kind"]) for f in data["files"]] == [("Intro.MP3", "audio"), ("Tool.exe", "program")])
        check("parent folder given", data["parent"] == str(_tmp))
        r = client.get("/files/browse", params={"path": str(_tmp)})
        check("sub folders listed", "Musik" in [d["name"] for d in r.json()["dirs"]])
        r = client.get("/files/browse")
        check("no path: drives / root", r.status_code == 200 and r.json()["dirs"] and r.json()["parent"] is None)
        check("missing folder -> 404", client.get("/files/browse", params={"path": str(_tmp / "fehlt")}).status_code == 404)

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
