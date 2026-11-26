"""When the program moves on, what desktop profiles showed or played before
stops (a video window closes, background music fades out).

The service notes every image, file and audio profile it starts, from a
program row, a shortcut or a Desktop key (programs such as OBS are never
noted: they are never closed). When a program row starts (Play or "Weiter"),
everything noted that the new row does not start itself is stopped, in the
background: the new row's own steps never wait for it.

- audio: the agent's normal stop (POST /audio stop), with the profile's
  fade-out; not on a PC where the new row starts audio (its start replaces
  the old track, and a stop sent alongside could end the new one);
- image and file: POST /close (desktop_agent): the windows a file opened get
  closed, an image is hidden if it is still the one shown. Agents from
  before /close answer 404: logged, nothing else happens.

Every stop has a short timeout. An agent that does not answer is marked
down for DOWN_SECONDS and skipped meanwhile, so a PC that is off never holds
anything up. Song windows are not profiles and stay as they are.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from . import display_agent
from .display_agent import AgentError

log = logging.getLogger("mixer.profile_stops")

DESKTOP_PREFIX = "desktop:"
CLOSE_TIMEOUT_SECONDS = 1.5
AUDIO_STOP_TIMEOUT_SECONDS = 3.0  # the agent answers at once and fades in the background
DOWN_SECONDS = 30.0
# Kinds whose output a new one of the same kind replaces on the same PC.
_ONE_PER_PC = ("audio", "image")


class ShownProfiles:
    """What desktop profiles the service started and may still be showing."""

    def __init__(self) -> None:
        self._shown: dict[str, tuple[str, str]] = {}  # profile id -> (target ip, kind)
        self._down_until: dict[str, float] = {}
        self._tasks: set = set()

    def started(self, profile_id: str, target_ip: str, kind: str) -> None:
        if kind == "program":
            return
        if kind in _ONE_PER_PC:
            for other, (ip, other_kind) in list(self._shown.items()):
                if ip == target_ip and other_kind == kind:
                    del self._shown[other]
        self._shown[profile_id] = (target_ip, kind)

    def audio_stopped(self, target_ip: str) -> None:
        """The music on that PC was stopped (the agent plays one track at a time)."""
        for other, (ip, kind) in list(self._shown.items()):
            if ip == target_ip and kind == "audio":
                del self._shown[other]

    def shown(self) -> dict:
        return dict(self._shown)

    def forget(self, profile_id: str) -> None:
        self._shown.pop(profile_id, None)

    def is_down(self, target_ip: str) -> bool:
        return self._down_until.get(target_ip, 0.0) > time.monotonic()

    def mark_down(self, target_ip: str) -> None:
        self._down_until[target_ip] = time.monotonic() + DOWN_SECONDS


def row_profiles(row: dict) -> set:
    """The desktop profiles a program row starts (its desktop list and
    action fields naming a profile)."""
    ids = set(row.get("desktop") or [])
    for field in ("playback", "translation", "lyrics"):
        value = row.get(field)
        if isinstance(value, str) and value.startswith(DESKTOP_PREFIX):
            ids.add(value.removeprefix(DESKTOP_PREFIX))
    return ids


def stop_previous(state, row: dict) -> Optional[asyncio.Task]:
    """Stop, in the background, what was shown before a program row starts.
    Returns the task (tests wait for it), or None when nothing is to stop."""
    tracker: Optional[ShownProfiles] = getattr(state, "shown_profiles", None)
    if tracker is None:
        return None
    profiles = state.cfg.desktop_display.profiles
    keep = row_profiles(row)
    new_audio_pcs = {profiles[p].target_ip for p in keep if p in profiles and profiles[p].kind == "audio"}
    stops = [(profile_id, ip, kind) for profile_id, (ip, kind) in tracker.shown().items()
             if profile_id not in keep and not (kind == "audio" and ip in new_audio_pcs)]
    if not stops:
        return None
    for profile_id, _ip, _kind in stops:
        tracker.forget(profile_id)
    task = asyncio.create_task(_stop_all(state, tracker, stops))
    tracker._tasks.add(task)  # keep a reference until it is done
    task.add_done_callback(tracker._tasks.discard)
    return task


async def _stop_all(state, tracker: ShownProfiles, stops: list) -> None:
    await asyncio.gather(*(_stop_one(state, tracker, *stop) for stop in stops))


async def _stop_one(state, tracker: ShownProfiles, profile_id: str, target_ip: str, kind: str) -> None:
    if tracker.is_down(target_ip):
        log.info("Not stopping %s on %s: the agent did not answer a moment ago", profile_id, target_ip)
        return
    cfg = state.cfg.desktop_display
    try:
        if kind == "audio":
            profile = cfg.profiles.get(profile_id)
            payload = {"id": profile_id, "action": "stop", "path": None}
            if profile is not None and profile.fade:
                # Its normal fade-out (app/actions.py control_audio).
                payload["fade"] = {"db": 0.0, "seconds": profile.fade_seconds}
            answer = await display_agent.call(cfg, target_ip, "/audio", payload, timeout=AUDIO_STOP_TIMEOUT_SECONDS)
        else:
            answer = await display_agent.call(cfg, target_ip, "/close", {"id": profile_id},
                                              timeout=CLOSE_TIMEOUT_SECONDS)
        log.info("Program moved on: %s %s on %s stopped (%s)", kind, profile_id, target_ip, answer)
    except AgentError as exc:
        if exc.agent_status is None and exc.status == 502:
            tracker.mark_down(target_ip)
            log.warning("Program moved on: %s on %s not stopped, agent unavailable (skipped for %.0f s)",
                        profile_id, target_ip, DOWN_SECONDS)
        elif exc.agent_status == 404:
            log.warning("Program moved on: %s on %s not closed, the agent is older than POST /close "
                        "(update the desktop agent)", profile_id, target_ip)
        else:
            log.warning("Program moved on: %s on %s not stopped: %s", profile_id, target_ip, exc.reason)
