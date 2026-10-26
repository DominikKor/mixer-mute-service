"""Update button endpoints (no Windows needed: the task start is stubbed).

Verifies: unavailable outside an installed Windows release, a start records the
request with a run id and launches the task once, a second start while it runs
is refused (unless the run went stale), a failing task start is reported, and
status + log tail are served for the dialog.
"""

import json
import os
import pathlib
import sys
import tempfile
import time

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="mixer_updater_"))
(_tmp / "config.yaml").write_text("mixer:\n  max_channels: 8\n")
os.environ["MIXER_CONFIG"] = str(_tmp / "config.yaml")

from fastapi.testclient import TestClient  # noqa: E402

import app.updater as updater  # noqa: E402
from app.main import app  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def main():
    status_file = _tmp / "update-status.json"
    with TestClient(app) as client:
        print("== not available on a dev machine ==")
        s = client.get("/update/status").json()
        check("status says unavailable", s["available"] is False and s["reason"])
        r = client.post("/update/start")
        check("start refused with 409", r.status_code == 409)
        check("nothing recorded", not status_file.exists())

        print("== installed Windows release (stubbed) ==")
        runs = []
        updater._support_check = lambda: (True, None)
        updater._installed_release = lambda: "v1.1.0"
        updater._run_task = lambda: (runs.append(1) or (True, "SUCCESS"))
        r = client.post("/update/start")
        check("start accepted", r.status_code == 200 and r.json()["ok"] is True)
        run_id = r.json()["run_id"]
        st = json.loads(status_file.read_text())
        check("request recorded with run id", st["state"] == "requested" and st["run_id"] == run_id)
        check("task started once", runs == [1])
        s = client.get("/update/status").json()
        check("status: available, active, installed tag",
              s["available"] and s["active"] and s["installed"] == "v1.1.0" and s["status"]["run_id"] == run_id)

        r = client.post("/update/start")
        check("second start while running -> 409", r.status_code == 409 and runs == [1])

        old = time.time() - updater.STALE_SECONDS - 5
        os.utime(status_file, (old, old))
        check("stale run no longer active", client.get("/update/status").json()["active"] is False)
        r = client.post("/update/start")
        check("start allowed after a stale run", r.status_code == 200 and runs == [1, 1])

        # The script reports its result in the same file (here: written by hand).
        st = json.loads(status_file.read_text())
        st.update(state="updated", message="Updated from v1.1.0 to v1.2.0.", to="v1.2.0")
        status_file.write_text(json.dumps(st))
        (_tmp / "logs").mkdir(exist_ok=True)
        (_tmp / "logs" / "update.log").write_bytes(
            "**********************\nChecking the latest release...\nUpdated to v1.2.0.\n".encode("utf-16"))
        s = client.get("/update/status").json()
        check("finished run not active", s["active"] is False and s["status"]["state"] == "updated")
        check("UTF-16 transcript tail served without banner",
              s["log"] == ["Checking the latest release...", "Updated to v1.2.0."])

        print("== task cannot be started ==")
        updater._run_task = lambda: (False, "ERROR: The system cannot find the file specified.")
        r = client.post("/update/start")
        check("502 with reason", r.status_code == 502 and "cannot find" in r.json()["reason"])
        check("recorded as failed, not left running",
              json.loads(status_file.read_text())["state"] == "failed"
              and client.get("/update/status").json()["active"] is False)

        print("== UI ==")
        ui = client.get("/").text
        check("update button and dialog served", 'id="update-btn"' in ui and 'id="update-dialog"' in ui)

    print(f"\n{PASS} passed, {FAIL} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
