"""When the program moves on, what the rows before showed or played stops
(a video window closes, background music fades out).

Only what a program row started counts (Play, "Weiter", the dock, Companion:
app/api/program.py runs the row inside program_starts(row id)). The service
notes each image, file and audio profile such a row started once the agent
accepted it (programs such as OBS are never noted: they are never closed;
a start without an answer is noted too, without its stamp: see below).
Whatever the operator starts by
hand (a Desktop key, a shortcut key) is not noted and keeps running; a
profile the program started becomes the operator's once its Desktop key is
used by hand. Notes live in memory only: after a restart nothing is noted,
so nothing is stopped.

When a program row starts, everything noted that the new row does not start
itself is stopped, in the background: the new row never waits for it. What
the new row starts again it sends again; the agent leaves music that still
plays ("keep_if_playing") and a video window still open ("keep_if_open") as
they are, so nothing restarts; an image is simply shown again. A window is
only kept for a profile an earlier program row started: one the operator
left open by hand is opened again.

- audio: the agent's normal stop with the profile's fade-out, naming the
  profile ("only_if"); never on a PC where the new row starts audio itself
  (a desktop profile or a file in its Playback / Übersetzung / Liedtext
  fields): its start replaces the old track anyway;
- image and file: POST /close (desktop_agent): the windows a file opened get
  closed, an image is hidden if it is still the one shown, a document the
  agent shows page by page is hidden.

A document in a file field (file:<ip>|<path>, app/documents.py) counts like
a file profile under its PC's document id; the next row keeps it (on its
page) only when it shows the same file on that PC.

Ordering without waiting: every start carries a stamp (a random id) that the
agent keeps with the track, window or image. A stop names the stamp of the
start it means, and the agent ignores it once a newer start (or a kept one)
of that profile came in. A start the agent did not answer in time (it may
have run all the same) is stopped by profile alone, so it cannot be left
running. A stop that did not reach the agent is noted again for the next
row, unless the profile was started since.

Every stop has a short timeout. An agent that does not answer is marked
down for DOWN_SECONDS and skipped meanwhile, so a PC that is off never holds
anything up. Song windows are not profiles and stay as they are.
"""

from __future__ import annotations

import asyncio
import contextvars
import logging
import time
import uuid
from contextlib import contextmanager
from typing import Optional

from . import display_agent
from .display_agent import AgentError
from .documents import document_id, is_document
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
        # profile id -> (target ip, kind, stamp, confirmed: the agent answered)
        self._shown: dict[str, tuple[str, str, str, bool]] = {}
        self._latest: dict[str, str] = {}  # profile id -> stamp of its latest start
        # Document ids of file fields -> the field's value ("file:<ip>|<path>") shown.
        self._sources: dict[str, str] = {}
        self._down_until: dict[str, float] = {}
        self._tasks: set = set()

    # -- starts (app/actions.py) ---------------------------------------------
    def starting(self, profile_id: str) -> str:
        """A start is about to be sent: its stamp. One by hand takes the
        profile over at once: it is no longer the program's."""
        stamp = uuid.uuid4().hex
        self._latest[profile_id] = stamp
        if program_row() is None:
            self._shown.pop(profile_id, None)
        return stamp

    def latest(self, profile_id: str) -> Optional[str]:
        return self._latest.get(profile_id)

    def accepted(self, profile_id: str, target_ip: str, kind: str, stamp: str) -> None:
        """The agent took the start. A program row's is noted now; on that PC
        it replaced the audio (or the image) before, which is then no longer
        the program's to stop."""
        if kind in _ONE_PER_PC:
            for other, (ip, other_kind, *_rest) in list(self._shown.items()):
                if other != profile_id and ip == target_ip and other_kind == kind:
                    del self._shown[other]
        if program_row() is not None and kind != "program" and self._latest.get(profile_id) == stamp:
            self._shown[profile_id] = (target_ip, kind, stamp, True)

    def timed_out(self, profile_id: str, target_ip: str, kind: str, stamp: str) -> None:
        """A program row's start the agent did not answer: it may have run.
        Noted all the same; its stop names the profile only."""
        if program_row() is not None and kind != "program" and self._latest.get(profile_id) == stamp:
            self._shown[profile_id] = (target_ip, kind, stamp, False)

    def note_source(self, profile_id: str, value: str) -> None:
        """What a file field's document id shows now (app/documents.py)."""
        self._sources[profile_id] = value

    def source(self, profile_id: str) -> Optional[str]:
        """The file field value the program showed under this id, if still noted."""
        return self._sources.get(profile_id) if profile_id in self._shown else None

    def forget(self, profile_id: str) -> None:
        """Not the program's to keep or stop any more (another file replaces it)."""
        self._shown.pop(profile_id, None)
        self._sources.pop(profile_id, None)

    def program_has(self, profile_id: str) -> bool:
        """An earlier program row started it and it was not stopped since."""
        return profile_id in self._shown

    def audio_replaced(self, target_ip: str) -> None:
        """The music on that PC was stopped by hand or replaced by a file's
        (one track per PC): no program audio to stop there any more."""
        for other, (ip, kind, *_rest) in list(self._shown.items()):
            if ip == target_ip and kind == "audio":
                del self._shown[other]

    audio_stopped = audio_replaced

    # -- stops -------------------------------------------------------------
    def shown(self) -> dict:
        """Profile id -> (target ip, kind) of what program rows started."""
        return {profile_id: (ip, kind) for profile_id, (ip, kind, *_rest) in self._shown.items()}

    def take(self, profile_id: str) -> tuple:
        """Forget it (a stop goes out); returns (ip, kind, stamp, confirmed)."""
        return self._shown.pop(profile_id)

    def restore(self, profile_id: str, noted: tuple) -> None:
        """A stop that did not reach the agent: try again on the next row,
        unless the profile was started (or taken over by hand) since."""
        if self._latest.get(profile_id) == noted[2] and profile_id not in self._shown:
            self._shown[profile_id] = noted

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


def row_documents(row: dict) -> dict:
    """Document id -> file field value of the documents a row's file fields show."""
    shown = {}
    for field in ("playback", "translation", "lyrics"):
        value = row.get(field)
        if is_file_action(value):
            try:
                target, path = parse_file_action(value)
            except ValueError:
                continue
            if file_kind(path) == "file" and is_document(path):
                shown[document_id(target)] = value
    return shown


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
    # A document of a file field stays only if the row shows the same file there.
    keep |= {doc_id for doc_id, value in row_documents(row).items() if tracker.source(doc_id) == value}
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
    target_ip, kind, stamp, confirmed = noted
    if tracker.latest(profile_id) != stamp:
        log.info("Not stopping %s: started again meanwhile", profile_id)
        return
    if tracker.is_down(target_ip):
        log.info("Not stopping %s on %s: the agent did not answer a moment ago", profile_id, target_ip)
        tracker.restore(profile_id, noted)
        return
    try:
        # A start without an answer: stopped by profile alone (the agent may
        # not know its stamp).
        answer = await _send_stop(state, profile_id, target_ip, kind, stamp if confirmed else None)
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


async def _send_stop(state, profile_id: str, target_ip: str, kind: str, stamp: Optional[str]) -> dict:
    from .actions import AUDIO_FADE_FULL_DB  # actions imports this module

    cfg = state.cfg.desktop_display
    if kind != "audio":
        body = {"id": profile_id} if stamp is None else {"id": profile_id, "stamp": stamp}
        return await display_agent.call(cfg, target_ip, "/close", body, timeout=CLOSE_TIMEOUT_SECONDS)
    profile = cfg.profiles.get(profile_id)
    payload = {"id": profile_id, "action": "stop", "path": None}
    if profile is not None and profile.fade:
        # Its normal fade-out (app/actions.py control_audio).
        payload["fade"] = {"db": AUDIO_FADE_FULL_DB, "seconds": profile.fade_seconds}
    try:
        # Only this profile's track, and only the start noted: never one started since.
        targeted = {**payload, "only_if": True, **({} if stamp is None else {"stamp": stamp})}
        return await display_agent.call(cfg, target_ip, "/audio", targeted, timeout=AUDIO_STOP_TIMEOUT_SECONDS)
    except AgentError as exc:
        if exc.agent_status != 400:
            raise
    # An agent from before "only_if": its plain stop (it ends whatever plays,
    # but no audio of the new row is on this PC, see stop_previous).
    log.warning("Agent %s is older than the targeted audio stop (update the desktop agent)", target_ip)
    return await display_agent.call(cfg, target_ip, "/audio", payload, timeout=AUDIO_STOP_TIMEOUT_SECONDS)
