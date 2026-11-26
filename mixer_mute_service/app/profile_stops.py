"""When the program moves on, what the rows before showed or played stops
(a video window closes, background music fades out).

Only what a program row started counts (Play, "Weiter", the dock, Companion:
app/api/program.py runs the row inside program_starts(row id)). The service
notes each image, file and audio profile such a row starts (programs such
as OBS are never noted: they are never closed). Whatever the operator starts
by hand (a Desktop key, a shortcut key) is not noted and keeps running; a
profile the program started becomes the operator's once its Desktop key is
used by hand. Notes live in memory only: after a restart nothing is noted,
so nothing is stopped.

When a program row starts, everything noted that the new row does not start
itself is stopped, in the background: the new row's own steps never wait
for it. A profile the new row starts again that an earlier row already has
running is left as it is (no new fade-in, no reopened video).

- audio: the agent's normal stop with the profile's fade-out, naming the
  profile ("only_if": the agent stops only that one, never a newer track);
  never on a PC where the new row starts audio itself (a desktop profile or
  a file in its Playback / Übersetzung / Liedtext fields): its start
  replaces the old track anyway;
- image and file: POST /close (desktop_agent): the windows a file opened get
  closed, an image is hidden if it is still the one shown.

Ordering: each start of a profile counts up its generation. A stop sent for
a profile holds that profile's lock while it is on its way and is dropped if
the profile was started again meanwhile; a start of the same profile waits
for it (at most the short stop timeout), so a stop never lands after a newer
start of the same profile. A stop that did not reach the agent is noted
again for the next row, unless the profile was started since.

Every stop has a short timeout. An agent that does not answer is marked
down for DOWN_SECONDS and skipped meanwhile, so a PC that is off never holds
anything up. Song windows are not profiles and stay as they are.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
from contextlib import contextmanager
from typing import Optional

from . import display_agent
from .display_agent import AgentError
from .file_actions import file_kind, is_file_action, parse_file_action

log = logging.getLogger("mixer.profile_stops")

DESKTOP_PREFIX = "desktop:"
CLOSE_TIMEOUT_SECONDS = 1.5
AUDIO_STOP_TIMEOUT_SECONDS = 3.0  # the agent answers at once and fades in the background
DOWN_SECONDS = 30.0
# Kinds whose output a new one of the same kind replaces on the same PC.
_ONE_PER_PC = ("audio", "image")
# The id of the program row running its steps (program_starts()), else None.
_PROGRAM_ROW = contextvars.ContextVar("program_row", default=None)


@contextmanager
def program_starts(row_id: str):
    """Profiles started inside count as the program's (this row's)."""
    token = _PROGRAM_ROW.set(row_id)
    try:
        yield
    finally:
        _PROGRAM_ROW.reset(token)


def program_row() -> Optional[str]:
    return _PROGRAM_ROW.get()


class ShownProfiles:
    """The desktop profiles program rows started that may still be showing."""

    def __init__(self) -> None:
        self._shown: dict[str, tuple[str, str, str]] = {}  # profile id -> (target ip, kind, row id)
        self._generation: dict[str, int] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._down_until: dict[str, float] = {}
        self._tasks: set = set()

    # -- starts (app/actions.py) ---------------------------------------------
    def lock(self, profile_id: str) -> asyncio.Lock:
        """Orders a profile's stop and its next start (see above)."""
        return self._locks.setdefault(profile_id, asyncio.Lock())

    def generation(self, profile_id: str) -> int:
        return self._generation.get(profile_id, 0)

    def running_from_earlier_row(self, profile_id: str, row_id: str) -> bool:
        """An earlier program row started it and it was not stopped since:
        the new row leaves it running (and it is now this row's)."""
        noted = self._shown.get(profile_id)
        if noted is None or noted[2] == row_id:
            return False
        self._shown[profile_id] = (noted[0], noted[1], row_id)
        return True

    def starting(self, profile_id: str, target_ip: str, kind: str) -> int:
        """A start is about to be sent. A program row's start is noted now,
        before the agent answers (a slow answer cannot lose it); one by hand
        takes the profile over: it is no longer the program's. Returns the
        generation to pass to accepted() / refused()."""
        generation = self.generation(profile_id) + 1
        self._generation[profile_id] = generation
        row_id = program_row()
        if row_id is not None and kind != "program":
            self._shown[profile_id] = (target_ip, kind, row_id)
        else:
            self._shown.pop(profile_id, None)
        return generation

    def accepted(self, profile_id: str, target_ip: str, kind: str) -> None:
        """The agent took the start: on that PC it replaced the audio (or
        the image) before, which is then no longer the program's to stop."""
        if kind in _ONE_PER_PC:
            for other, (ip, other_kind, _row) in list(self._shown.items()):
                if other != profile_id and ip == target_ip and other_kind == kind:
                    del self._shown[other]

    def refused(self, profile_id: str, generation: int) -> None:
        """The agent refused the start: nothing new runs."""
        if self.generation(profile_id) == generation:
            self._shown.pop(profile_id, None)

    def audio_stopped(self, target_ip: str) -> None:
        """The music on that PC was stopped by hand (one track per PC)."""
        for other, (ip, kind, _row) in list(self._shown.items()):
            if ip == target_ip and kind == "audio":
                del self._shown[other]

    # -- stops -------------------------------------------------------------
    def shown(self) -> dict:
        """Profile id -> (target ip, kind) of what program rows started."""
        return {profile_id: (ip, kind) for profile_id, (ip, kind, _row) in self._shown.items()}

    def take(self, profile_id: str) -> tuple:
        """Forget it (a stop goes out); returns (ip, kind, row id, generation)."""
        ip, kind, row_id = self._shown.pop(profile_id)
        return ip, kind, row_id, self.generation(profile_id)

    def restore(self, profile_id: str, noted: tuple) -> None:
        """A stop that did not reach the agent: try again on the next row,
        unless the profile was started (or taken over by hand) since."""
        ip, kind, row_id, generation = noted
        if self.generation(profile_id) == generation and profile_id not in self._shown:
            self._shown[profile_id] = (ip, kind, row_id)

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


def row_audio_pcs(row: dict, profiles: dict) -> set:
    """The PCs on which a program row starts audio: audio desktop profiles
    and audio files in its file fields."""
    pcs = {profiles[p].target_ip for p in row_profiles(row) if p in profiles and profiles[p].kind == "audio"}
    for field in ("playback", "translation", "lyrics"):
        value = row.get(field)
        if is_file_action(value):
            try:
                target, path = parse_file_action(value)
            except ValueError:
                continue
            if file_kind(path) == "audio":
                pcs.add(target)
    return pcs


def stop_previous(state, row: dict) -> Optional[asyncio.Task]:
    """Stop, in the background, what earlier program rows started that this
    row does not start itself. Returns the task (tests wait for it), or None
    when nothing is to stop."""
    tracker: Optional[ShownProfiles] = getattr(state, "shown_profiles", None)
    if tracker is None:
        return None
    keep = row_profiles(row)
    audio_pcs = row_audio_pcs(row, state.cfg.desktop_display.profiles)
    stops = [(profile_id, tracker.take(profile_id)) for profile_id, (ip, kind) in tracker.shown().items()
             if profile_id not in keep and not (kind == "audio" and ip in audio_pcs)]
    if not stops:
        return None
    task = asyncio.create_task(_stop_all(state, tracker, stops))
    tracker._tasks.add(task)  # keep a reference until it is done
    task.add_done_callback(tracker._tasks.discard)
    return task


async def _stop_all(state, tracker: ShownProfiles, stops: list) -> None:
    await asyncio.gather(*(_stop_one(state, tracker, profile_id, noted) for profile_id, noted in stops))


async def _stop_one(state, tracker: ShownProfiles, profile_id: str, noted: tuple) -> None:
    target_ip, kind, _row, generation = noted
    if tracker.is_down(target_ip):
        log.info("Not stopping %s on %s: the agent did not answer a moment ago", profile_id, target_ip)
        tracker.restore(profile_id, noted)
        return
    async with tracker.lock(profile_id):
        if tracker.generation(profile_id) != generation:
            log.info("Not stopping %s: started again meanwhile", profile_id)
            return
        try:
            answer = await _send_stop(state, profile_id, target_ip, kind)
            log.info("Program moved on: %s %s on %s stopped (%s)", kind, profile_id, target_ip, answer)
        except AgentError as exc:
            if exc.agent_status is None and exc.status == 502:
                tracker.mark_down(target_ip)
                tracker.restore(profile_id, noted)
                log.warning("Program moved on: %s on %s not stopped, agent unavailable (skipped for %.0f s)",
                            profile_id, target_ip, DOWN_SECONDS)
            elif exc.agent_status == 404:
                log.warning("Program moved on: %s on %s not closed, the agent is older than POST /close "
                            "(update the desktop agent)", profile_id, target_ip)
            else:
                log.warning("Program moved on: %s on %s not stopped: %s", profile_id, target_ip, exc.reason)


async def _send_stop(state, profile_id: str, target_ip: str, kind: str) -> dict:
    from .actions import AUDIO_FADE_FULL_DB  # actions imports this module

    cfg = state.cfg.desktop_display
    if kind != "audio":
        return await display_agent.call(cfg, target_ip, "/close", {"id": profile_id}, timeout=CLOSE_TIMEOUT_SECONDS)
    profile = cfg.profiles.get(profile_id)
    payload = {"id": profile_id, "action": "stop", "path": None}
    if profile is not None and profile.fade:
        # Its normal fade-out (app/actions.py control_audio).
        payload["fade"] = {"db": AUDIO_FADE_FULL_DB, "seconds": profile.fade_seconds}
    try:
        # Only this profile's track: never one started since.
        return await display_agent.call(cfg, target_ip, "/audio", {**payload, "only_if": True},
                                        timeout=AUDIO_STOP_TIMEOUT_SECONDS)
    except AgentError as exc:
        if exc.agent_status != 400:
            raise
    # An agent from before "only_if": its plain stop (it ends whatever plays,
    # but no audio of the new row is on this PC, see stop_previous).
    log.warning("Agent %s is older than the targeted audio stop (update the desktop agent)", target_ip)
    return await display_agent.call(cfg, target_ip, "/audio", payload, timeout=AUDIO_STOP_TIMEOUT_SECONDS)
