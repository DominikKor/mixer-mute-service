"""Answer shapes shared by all routes.

Every answer is a JSON object with "ok". A refusal carries a short English
"reason" (the page translates the known ones) and a 4xx/5xx status:
400 bad input, 404 unknown thing, 409 refused in the current state
(mixer, safety, program), 502 a device answered badly or not at all,
503 a feature is switched off or not configured.
"""

from __future__ import annotations

import json
from typing import Any, Tuple

from fastapi.responses import JSONResponse

Result = Tuple[dict, int]

# Controller reasons that are the caller's fault or an unknown name.
_NOT_FOUND = {"unknown group"}
_BAD_INPUT = {"invalid channel", "invalid fader value", "invalid fader_hex"}


def fail(reason: str, status: int = 400, **extra: Any) -> JSONResponse:
    return JSONResponse({"ok": False, "reason": reason, **extra}, status_code=status)


def status_for(result: dict) -> int:
    """HTTP status of a controller result: blocked sends are 409 (stale or
    unknown values, panic, no connection, unsafe group)."""
    if result.get("ok"):
        return 200
    reason = result.get("reason", "")
    if reason in _NOT_FOUND:
        return 404
    if reason in _BAD_INPUT:
        return 400
    return 409


def controller_result(result: dict) -> Result:
    return result, status_for(result)


def reply(result: dict, status: int = 200):
    """A route's answer for a (result, status) pair."""
    if status == 200 and result.get("ok") is not False:
        return result
    return JSONResponse(result, status_code=status if status != 200 else 502)


def respond(result: dict) -> JSONResponse:
    """A controller result as an answer."""
    return JSONResponse(result, status_code=status_for(result))


def unwrap(response: Any) -> Result:
    """(body, status) of a route function's return value."""
    if isinstance(response, JSONResponse):
        return json.loads(response.body.decode("utf-8")), response.status_code
    return response, 200


def round3(value):
    return None if value is None else round(value, 3)
