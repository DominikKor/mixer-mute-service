"""Update button of the web UI: start the GitHub updater and report progress.

The service cannot update itself: update-static.ps1 stops and replaces it. So
POST /update/start only records the request in update-status.json and starts
the scheduled task "MixerMuteServiceUpdate" (registered by install-service.ps1,
runs update-from-github.ps1 -NonInteractive as SYSTEM). The script keeps that
file and logs/update.log current; GET /update/status serves both, so the page
can show progress before the service restarts and the result after it is back.
"""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Tuple

from fastapi import APIRouter
from fastapi.responses import JSONResponse

from .config import default_config_path

router = APIRouter()
log = logging.getLogger("mixer.updater")

UPDATE_TASK = "MixerMuteServiceUpdate"
# A run that has not touched its status for this long is considered dead.
STALE_SECONDS = 15 * 60
LOG_TAIL_LINES = 40
ACTIVE_STATES = ("requested", "running")


def _data_dir() -> Path:
    return default_config_path().parent


def _status_path() -> Path:
    return _data_dir() / "update-status.json"


def _log_path() -> Path:
    return _data_dir() / "logs" / "update.log"


def _install_dir() -> Path:
    return Path(sys.executable).parent


def _support_check() -> Tuple[bool, Optional[str]]:
    """Updates work only in an installed Windows release built by GitHub."""
    if sys.platform != "win32" or not getattr(sys, "frozen", False):
        return False, "updates are only available on the installed Windows service"
    if not (_install_dir() / "github-repo.txt").exists():
        return False, "this installation was not built by GitHub and has no updater"
    return True, None


def _installed_release() -> Optional[str]:
    tag_file = _install_dir() / "release-tag.txt"
    try:
        return tag_file.read_text(encoding="utf-8-sig").strip() or None
    except OSError:
        return None


def _read_status() -> Optional[dict]:
    try:
        data = json.loads(_status_path().read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _status_age() -> Optional[float]:
    try:
        return time.time() - _status_path().stat().st_mtime
    except OSError:
        return None


def _write_status(state: str, run_id: str, message: str) -> None:
    path = _status_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "state": state,
        "run_id": run_id,
        "message": message,
        "from": _installed_release() or "",
        "to": "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2), encoding="utf-8")


def _log_tail() -> list:
    try:
        raw = _log_path().read_bytes()
    except OSError:
        return []
    # Windows PowerShell may write its transcript as UTF-16.
    text = raw.decode("utf-16") if raw.startswith(b"\xff\xfe") else raw.decode("utf-8-sig", errors="replace")
    lines = [line.rstrip() for line in text.splitlines()]
    return [line for line in lines if line and not line.startswith("*****")][-LOG_TAIL_LINES:]


def _oem_encoding() -> str:
    """Code page of console programs such as schtasks (cp850 on German Windows)."""
    if sys.platform == "win32":
        import ctypes
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    return "utf-8"


def _decode(raw: bytes, encoding: Optional[str] = None) -> str:
    # Never fail on the text of a message: umlauts in schtasks' output
    # ("ausführen") once made the whole request fail with HTTP 500.
    try:
        return raw.decode(encoding or _oem_encoding())
    except (LookupError, UnicodeDecodeError):
        return raw.decode("utf-8", errors="replace")


def _run_task() -> Tuple[bool, str]:
    proc = subprocess.run(
        ["schtasks", "/Run", "/TN", UPDATE_TASK],
        capture_output=True, timeout=15,
    )
    return proc.returncode == 0, _decode(proc.stdout + proc.stderr).strip()


def _is_active(status: Optional[dict]) -> bool:
    if not status or status.get("state") not in ACTIVE_STATES:
        return False
    age = _status_age()
    return age is not None and age < STALE_SECONDS


@router.get("/update/status")
async def update_status():
    available, reason = _support_check()
    status = _read_status()
    return {
        "available": available,
        "reason": reason,
        "installed": _installed_release(),
        "active": _is_active(status),
        "status": status,
        "log": _log_tail(),
    }


@router.post("/update/start")
async def update_start():
    try:
        return await _start()
    except Exception as exc:  # noqa: BLE001 - the dialog must show a reason
        log.exception("update start failed")
        return JSONResponse({"ok": False, "reason": f"{type(exc).__name__}: {exc}"}, 500)


async def _start():
    available, reason = _support_check()
    if not available:
        return JSONResponse({"ok": False, "reason": reason}, 409)
    if _is_active(_read_status()):
        return JSONResponse({"ok": False, "reason": "an update is already running"}, 409)

    run_id = uuid.uuid4().hex
    _write_status("requested", run_id, "Update requested")
    try:
        started, output = await asyncio.to_thread(_run_task)
    except Exception as exc:  # noqa: BLE001 - always answer with a reason
        started, output = False, f"{type(exc).__name__}: {exc}"
    if not started:
        _write_status("failed", run_id, f"could not start the update task: {output}")
        log.error("update task %s could not be started: %s", UPDATE_TASK, output)
        return JSONResponse({"ok": False, "reason": f"could not start the update task: {output}"}, 502)

    log.info("update task %s started (run %s)", UPDATE_TASK, run_id)
    return {"ok": True, "run_id": run_id}
