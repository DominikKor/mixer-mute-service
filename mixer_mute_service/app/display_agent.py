"""The one client for the display agents (desktop_agent/show_image_agent.py,
port 8766 on each PC): desktop profiles, audio, songs, program files and
shutdown all go through ``call``.

Every request carries the shared agent token, so it is only ever sent to a
LAN address: a private or loopback IPv4 address. A target typed into a
setting or a program row cannot send the token anywhere else.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import urllib.error
import urllib.request
from ipaddress import IPv4Address
from typing import Any, Optional

log = logging.getLogger("mixer.display_agent")

AGENT_PORT = 8766
# Starting music: the agent waits up to 10 s for mpv on a cold start
# (desktop_agent IPC_CONNECT_SECONDS), so its answer may take that long.
AUDIO_TIMEOUT_SECONDS = 15.0


class AgentError(Exception):
    """A call that did not succeed.

    ``status`` is the status code for our own answer; ``agent_status`` and
    ``body`` are the agent's HTTP status and JSON answer when it refused.
    """

    def __init__(self, reason: str, status: int, agent_status: Optional[int] = None,
                 body: Optional[dict] = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.agent_status = agent_status
        self.body = body

    def answer(self) -> dict:
        return {"ok": False, "reason": self.reason}


def agent_token(display_cfg) -> str:
    """The token, or "" when desktop display is switched off or has none."""
    if not display_cfg.enabled:
        return ""
    return os.environ.get(display_cfg.token_env, "")


def is_ready(display_cfg) -> bool:
    return bool(agent_token(display_cfg))


def allowed_target(ip: str) -> bool:
    """A private or loopback IPv4 address (never the internet)."""
    try:
        address = IPv4Address(ip)
    except ValueError:
        return False
    if address.is_unspecified or address.is_multicast or address.is_reserved:
        return False
    return address.is_private or address.is_loopback


def _post(url: str, token: str, payload: dict, timeout: float) -> dict:
    """One blocking request (tests replace this function)."""
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json", "X-Display-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        data = json.loads(response.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("agent answer is not a JSON object")
    return data


def _error_body(exc: urllib.error.HTTPError) -> Optional[dict]:
    try:
        data: Any = json.loads(exc.read().decode("utf-8"))
    except (ValueError, UnicodeDecodeError, OSError, AttributeError, TypeError):
        return None
    return data if isinstance(data, dict) else None


async def call(display_cfg, target_ip: str, path: str, payload: dict, timeout: float = 3.0) -> dict:
    """POST ``payload`` to ``path`` on the agent at ``target_ip``.

    Returns the agent's JSON answer, or raises AgentError:
    503 not configured, 400 address not allowed, 502 refused or unreachable.
    """
    token = agent_token(display_cfg)
    if not token:
        raise AgentError("desktop display is not configured", 503)
    if not allowed_target(target_ip):
        log.warning("Display agent call to %s%s refused: not a LAN address", target_ip, path)
        raise AgentError("display agent address must be a LAN address", 400)
    url = f"http://{target_ip}:{AGENT_PORT}{path}"
    try:
        return await asyncio.to_thread(_post, url, token, payload, timeout)
    except urllib.error.HTTPError as exc:
        log.warning("Display agent %s refused %s: HTTP %s", target_ip, path, exc.code)
        raise AgentError("display agent rejected the request", 502,
                         agent_status=exc.code, body=_error_body(exc)) from exc
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("Display agent %s unavailable for %s: %s", target_ip, path, exc)
        raise AgentError("display agent unavailable", 502) from exc
