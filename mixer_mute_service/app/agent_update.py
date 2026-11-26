"""Update key, second step: the display agent on the PC whose browser pressed it.

The Update key first updates the service (app/updater.py). Then the page asks
here for the display agent of its own PC: the one at the address the request
came from (the service's own PC for a browser on it). Its version is compared
with the agent files of this release (the same files the .cmd download
carries); an older agent gets them through its self-update (POST /update on
the agent) and starts again.

GET  /update/agent   {present, version, latest, current, self_update}
POST /update/agent   update the agent of this PC and wait until it is back
GET  /update/agents  the same for every PC the service sends to, with the mpv
                     and yt-dlp each agent finds (null: not found)
POST /update/agents  every agent that answers starts again (all at once): one
                     that is behind through its self-update, a current one
                     through POST /restart on the agent (same restart, so a
                     press of the Update key always gives fresh agents)

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
import json
import logging
import time
from ipaddress import IPv4Address

from fastapi import APIRouter, Request

from . import display_agent
from .agent_download import SCRIPT_NAME, _agent_files, _version, agent_dir
from .songs import note_agent
from .mission_spotlight import profile_path
from .responses import fail

router = APIRouter()
log = logging.getLogger("mixer.agent_update")

RESTART_WAIT_SECONDS = 45

# Every /info call carries the audio sources (files, YouTube links) of the
# PC's audio profiles. Current agents keep a local copy of each and
# play that: the music is on a Google Drive drive, whose files are fetched
# only when opened, so mpv may wait for Drive before the first sound. Older
# agents ignore the field. Every open page asks /update/agents every 30 s
# (and with every Update), so new and changed profiles reach the agents
# soon; the agent looks at each source at most once per 5 minutes and tries
# a failed one again only after a growing pause (5 min doubling to 1 h).
MAX_AUDIO_SOURCES = 20
MAX_AUDIO_SOURCES_BYTES = 6000  # the agent takes at most 8 KiB for /info


def audio_sources(request: Request, target: str) -> list:
    sources: list = []
    profiles = request.app.state.cfg.desktop_display.profiles
    for profile in profiles.values():
        if profile.kind != "audio" or profile.target_ip != target or not profile.path:
            continue
        path = profile_path(profile)
        if path in sources:
            continue
        if len(sources) >= MAX_AUDIO_SOURCES or len(json.dumps(sources + [path])) > MAX_AUDIO_SOURCES_BYTES:
            break
        sources.append(path)
    return sources


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
        answer = await display_agent.call(cfg, target, "/info", {"audio": audio_sources(request, target)},
                                          timeout=2.0)
    except display_agent.AgentError as exc:
        if exc.agent_status == 404:
            # Answers, but does not know /info: an agent from before.
            return {**base, "present": True, "version": None, "current": False, "self_update": False}
        return {**base, "present": False, "reason": "no display agent on this device"}
    songs = getattr(request.app.state, "songs", None)
    if songs is not None:
        # A restarted or updated agent may now take what it refused before (song layouts).
        note_agent(songs.agent_memory, target, answer)
    version = str(answer.get("version") or "unknown")
    info = {**base, "present": True, "version": version, "current": version == latest, "self_update": True}
    # Agents since 1.2.1-16x say which mpv / yt-dlp they find (None: not
    # found) and what of the user's environment they had to fill in at start.
    # Since the local audio copies also where mpv's last start spent its
    # time (audio_start; see mpv_start_report in the agent).
    for key in ("mpv", "ytdlp", "restored_env", "started_at", "audio_start"):
        if key in answer:
            info[key] = answer[key]
    # Agents with POST /restart say so; older ones can only be updated.
    info["restart"] = answer.get("restart") is True
    return info


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
        for target in songs.settings.targets:
            names.setdefault(target.ip, "Liedanzeige")
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


async def _restart_one(request: Request, target: str, info: dict) -> dict:
    """Restart one current agent and wait until a new process answers (its
    start time changed). Returns the agent's info, with "error" when not."""
    cfg = request.app.state.cfg.desktop_display
    try:
        await display_agent.call(cfg, target, "/restart", {}, timeout=10.0)
    except display_agent.AgentError as exc:
        return {**info, "error": f"display agent restart failed: {exc.reason}", "status": exc.status}
    log.info("Display agent on %s restarting (%s)", target, info.get("version"))
    deadline = time.monotonic() + RESTART_WAIT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(1.5)
        after = await _agent_info(request, target)
        if after.get("present") and after.get("started_at") not in (None, info.get("started_at")):
            log.info("Display agent on %s is back", target)
            return {**after, "restarted": True}
    return {**info, "error": "display agent did not come back after the restart", "status": 504}


@router.get("/update/agents")
async def agents_status(request: Request):
    pcs = await agent_pcs(request)
    infos = await asyncio.gather(*(_agent_info(request, pc["pc"]) for pc in pcs))
    return {"ok": True, "latest": _version(request),
            "agents": [{**pc, **info} for pc, info in zip(pcs, infos)]}


@router.post("/update/agents")
async def agents_update(request: Request):
    """Start every agent that answers again, all at once: one that is behind
    gets the new files (self-update, then it restarts), a current one is
    restarted. Per PC the answer says "updated", "restarted", self_update
    false (too old: the .cmd download once) or present false (not
    reachable). "ok" is false when one that could be done was not."""
    pcs = await agent_pcs(request)
    infos = await asyncio.gather(*(_agent_info(request, pc["pc"]) for pc in pcs))
    to_update = [(pc, info) for pc, info in zip(pcs, infos)
                 if info.get("present") and info.get("self_update") and not info.get("current")]
    # A current agent without /restart cannot be restarted from here (only a
    # build whose agent files lack it); it is left alone.
    to_restart = [(pc, info) for pc, info in zip(pcs, infos)
                  if info.get("present") and info.get("self_update") and info.get("current") and info.get("restart")]
    bundle = None
    if to_update:
        try:
            bundle = _bundle(request)
        except FileNotFoundError as exc:
            return fail(str(exc), 404)
    jobs = [_update_one(request, pc["pc"], info, bundle) for pc, info in to_update]
    jobs += [_restart_one(request, pc["pc"], info) for pc, info in to_restart]
    targets = [pc["pc"] for pc, _ in to_update] + [pc["pc"] for pc, _ in to_restart]
    done = dict(zip(targets, await asyncio.gather(*jobs)))
    agents = [{**pc, **done.get(pc["pc"], info)} for pc, info in zip(pcs, infos)]
    failed = [a for a in agents if "error" in a]
    return {"ok": not failed, "latest": _version(request), "agents": agents}
