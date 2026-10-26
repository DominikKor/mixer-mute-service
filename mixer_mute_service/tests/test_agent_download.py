"""Agent update download (GET /desktop/agent/update.cmd).

Verifies: served as an attachment, a valid batch file (no BOM, CRLF, starts
with @echo off), cmd.exe stops before the PowerShell part and the marker occurs
only once, every agent file is embedded byte-identical, the version is set, the
update script follows, and a missing agent folder gives a clear 404.
"""

import base64
import os
import pathlib
import re
import tempfile

_tmp = pathlib.Path(tempfile.mkdtemp(prefix="mixer_agent_dl_"))
(_tmp / "config.yaml").write_text("mixer:\n  max_channels: 8\n")
os.environ["MIXER_CONFIG"] = str(_tmp / "config.yaml")

from fastapi.testclient import TestClient  # noqa: E402

import app.agent_download as agent_download  # noqa: E402
from app.main import app  # noqa: E402

PASS = 0
FAIL = 0


def check(name, cond):
    global PASS, FAIL
    PASS, FAIL = (PASS + 1, FAIL) if cond else (PASS, FAIL + 1)
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")


def main():
    agent_dir = agent_download.agent_dir()
    with TestClient(app) as client:
        print("== download ==")
        r = client.get("/desktop/agent/update.cmd")
        check("200", r.status_code == 200)
        check("attachment with .cmd name",
              'attachment; filename="MixerDisplayAgent-Update.cmd"' in r.headers.get("content-disposition", ""))
        raw = r.content
        check("no BOM, starts with @echo off", raw.startswith(b"@echo off\r\n"))
        check("CRLF line endings only", b"\n" not in raw.replace(b"\r\n", b""))
        text = raw.decode("utf-8")

        print("== batch / PowerShell split ==")
        marker = text.find("##POWERSHELL")
        check("marker occurs exactly once", text.count("##POWERSHELL") == 1)
        check("cmd exits before the marker", 0 < text.find("exit /b") < marker)
        ps = text[marker:]
        check("version set", re.search(r'^\$AgentVersion = "[A-Za-z0-9._+-]+"\r$', ps, re.M) is not None)
        check("update script follows", "Get-ScheduledTask -TaskName $TaskName" in ps)

        print("== embedded files ==")
        embedded = dict(re.findall(r'^    "([^"]+)" = "([A-Za-z0-9+/=]+)"\r$', ps, re.M))
        expected = sorted(p.name for p in agent_dir.iterdir()
                          if p.is_file() and p.suffix in (".py", ".txt", ".ps1") and not p.name.startswith("."))
        check("all agent files embedded", sorted(embedded) == expected and "show_image_agent.py" in expected)
        check("contents byte-identical",
              all(base64.b64decode(embedded[n]) == (agent_dir / n).read_bytes() for n in expected))

        print("== agent folder missing ==")
        original = agent_download.agent_dir
        agent_download.agent_dir = lambda: _tmp / "no-such-dir"
        try:
            r = client.get("/desktop/agent/update.cmd")
            check("404 with reason", r.status_code == 404 and r.json()["ok"] is False and r.json()["reason"])
        finally:
            agent_download.agent_dir = original

    print(f"\n{PASS} passed, {FAIL} failed")
    raise SystemExit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
