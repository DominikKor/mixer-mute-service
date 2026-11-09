"""Update key, second step: the display agent on the PC whose browser pressed it.

The Update key first updates the service (app/updater.py). Then the page asks
here for the display agent of its own PC: the one at the address the request
came from (the service's own PC for a browser on it). Its version is compared
with the agent files of this release (the same files the .cmd download
carries); an older agent gets them through its self-update (POST /update on
the agent) and starts again.

GET  /update/agent   {present, version, latest, current, self_update}
POST /update/agent   update the agent of this PC and wait until it is back
GET  /update/agents  the same for every PC the service sends to
POST /update/agents  update every agent that is behind (all at once)

"Every PC" is the Ziel-PCs list, the PCs of the desktop profiles, the song
display PC and the PC of the browser asking. The Update key uses the
/agents routes: a PC whose browser never presses it (the Anzeige PC) is
updated too.

Agents from before the self-update answer 404 to /info: they need the .cmd
download once (``self_update`` false); every later update is automatic.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from ipaddress import IPv4Address

from fastapi import APIRouter, Request

from . import display_agent
from .agent_download import SCRIPT_NAME, _agent_files, _version, agent_dir
from .responses import fail

router = APIRouter()
log = logging.getLogger("mixer.agent_update")

RESTART_WAIT_SECONDS = 45


def client_pc(request: Request) -> str:
    """The address of the PC the request came from; this PC is 127.0.0.1."""
    host = (request.client.host if request.client else "") or ""
    if host.startswith("::ffff:"):
        host = host[len("::ffff:"):]
    try:
        address = IPv4Address(host)
    except ValueError:
        return "127.0.0.1" if host in ("::1", "localhost") else ""
    return "127.0.0.1" if address.is_loopback else host


async def _agent_info(request: Request, target: str) -> dict:
    """What the agent at target is: missing, too old for the self-update, or
    a version."""
    latest = _version(request)
    base = {"pc": target, "latest": latest}
    cfg = request.app.state.cfg.desktop_display
    if not display_agent.is_ready(cfg):
        return {**base, "present": False, "reason": "desktop display is not configured"}
    if not target or not display_agent.allowed_target(target):
        return {**base, "present": False, "reason": "no display agent on this device"}
    try:
        answer = await display_agent.call(cfg, target, "/info", {}, timeout=2.0)
    except display_agent.AgentError as exc:
        if exc.agent_status == 404:
            # Answers, but does not know /info: an agent from before.
            return {**base, "present": True, "version": None, "current": False, "self_update": False}
        return {**base, "present": False, "reason": "no display agent on this device"}
    version = str(answer.get("version") or "unknown")
    return {**base, "present": True, "version": version, "current": version == latest, "self_update": True}


def _bundle(request: Request) -> dict:
    directory = agent_dir()
    if not (directory / SCRIPT_NAME).is_file():
        raise FileNotFoundError("display agent files are not part of this installation")
    return {
        "version": _version(request),
        "files": {p.name: base64.b64encode(p.read_bytes()).decode("ascii") for p in _agent_files(directory)},
    }


async def _update_one(request: Request, target: str, info: dict, bundle: dict) -> dict:
    """Send the new files to one agent and wait until it runs them. Returns
    the agent's info, with "error" when it did not work."""
    cfg = request.app.state.cfg.desktop_display
    try:
        await display_agent.call(cfg, target, "/update", bundle, timeout=30.0)
    except display_agent.AgentError as exc:
        return {**info, "error": f"display agent update failed: {exc.reason}", "status": exc.status}
    log.info("Display agent on %s updating %s -> %s", target, info["version"], bundle["version"])
    # The agent starts again; wait until it reports the new version.
    deadline = time.monotonic() + RESTART_WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(1.5)
        after = await _agent_info(request, target)
        if after.get("present") and after.get("current"):
            log.info("Display agent on %s runs %s", target, after["version"])
            return {**after, "updated": True}
    return {**info, "error": "display agent did not come back with the new version", "status": 504}


async def agent_pcs(request: Request) -> list:
    """[{pc, name}] of every PC with a display agent the service knows of."""
    state = request.app.state
    names: dict = {}
    target_pcs = getattr(state, "target_pcs", None)
    if target_pcs is not None:
        for pc in await target_pcs.get():
            names.setdefault(pc["ip"], pc["name"])
    for profile in state.cfg.desktop_display.profiles.values():
        names.setdefault(profile.target_ip, profile.title)
    songs = getattr(state, "songs", None)
    if songs is not None:
        names.setdefault(songs.settings.target_ip, "Liedanzeige")
    caller = client_pc(request)
    if caller and caller != "127.0.0.1":
        names.setdefault(caller, "dieser PC")
    return [{"pc": ip, "name": name} for ip, name in sorted(names.items())
            if display_agent.allowed_target(ip)]


def _refuse(reason: str, status: int, info: dict):
    return fail(reason, status, **{k: v for k, v in info.items() if k != "reason"})


@router.get("/update/agent")
async def agent_status(request: Request):
    return {"ok": True, **await _agent_info(request, client_pc(request))}


@router.post("/update/agent")
async def agent_update(request: Request):
    target = client_pc(request)
    info = await _agent_info(request, target)
    if not info["present"]:
        return _refuse(info.get("reason", "no display agent on this device"), 404, info)
    if not info["self_update"]:
        return _refuse("display agent too old for the automatic update", 409, info)
    if info["current"]:
        return {"ok": True, "updated": False, **info}
    try:
        bundle = _bundle(request)
    except FileNotFoundError as exc:
        return _refuse(str(exc), 404, info)
    result = await _update_one(request, target, info, bundle)
    if "error" in result:
        return _refuse(result["error"], result["status"], info)
    return {"ok": True, **result}


@router.get("/update/agents")
async def agents_status(request: Request):
    pcs = await agent_pcs(request)
    infos = await asyncio.gather(*(_agent_info(request, pc["pc"]) for pc in pcs))
    return {"ok": True, "latest": _version(request),
            "agents": [{**pc, **info} for pc, info in zip(pcs, infos)]}


@router.post("/update/agents")
async def agents_update(request: Request):
    """Update every agent that is behind, all at once. "ok" is false when one
    that could be updated was not; agents too old for the self-update are
    listed with self_update false (they need the .cmd download once)."""
    pcs = await agent_pcs(request)
    infos = await asyncio.gather(*(_agent_info(request, pc["pc"]) for pc in pcs))
    due = [(pc, info) for pc, info in zip(pcs, infos)
           if info.get("present") and info.get("self_update") and not info.get("current")]
    bundle = None
    if due:
        try:
            bundle = _bundle(request)
        except FileNotFoundError as exc:
            return fail(str(exc), 404)
    updated = dict(zip((pc["pc"] for pc, _ in due),
                       await asyncio.gather(*(_update_one(request, pc["pc"], info, bundle) for pc, info in due))))
    agents = [{**pc, **updated.get(pc["pc"], info)} for pc, info in zip(pcs, infos)]
    failed = [a for a in agents if "error" in a]
    return {"ok": not failed, "latest": _version(request), "agents": agents}
