"""Running things by their ID: Steuerung keys, catalog keys (light, cameras,
OBS, beamer, Overall macros), desktop profiles and files on a PC.

Steuerung, the camera section and Programm rows all go through here, so a
key behaves the same wherever it is pressed. Every function returns
(result, status) (see app/responses.py); nothing here builds HTTP answers.

Values of a program action field:
  "<button id>"           a Steuerung key or a catalog camera key
  "desktop:<profile id>"  a desktop profile (audio profiles start playing)
  "file:<ip>|<path>"      a file on a PC (app/file_actions.py)
"""

from __future__ import annotations

import asyncio
import logging
from copy import deepcopy
from typing import Optional

from fastapi import Request

from . import display_agent, documents
from .camera_program import CAMERAS, DEFAULT_WAIT_SECONDS
from .companion_buttons import CATALOG_ONLY_IDS, COMPANION_BUTTONS_BY_ID, resolve_alias
from .display_agent import AgentError
from .file_actions import (PLAYER_FAILURE_PREFIX, is_file_action, no_file_reason, run_file_action,
                           runs_from_network_share, spotlight_search)
from .mission_spotlight import profile_path
from .native_controls import NativeControlError
from .profile_stops import program_row
from .responses import Result, controller_result

log = logging.getLogger("mixer.actions")

DESKTOP_PREFIX = "desktop:"
PROGRAM_ACTION_FIELDS = ("playback", "translation", "lyrics")
# Profile fields the agent's /display endpoint does not take (it refuses
# unknown keys).
_AGENT_EXCLUDED_FIELDS = {"target_ip", "fade", "fade_seconds", "monitors", "mission_spotlight"}
AUDIO_FADE_FULL_DB = 0.0  # audio fades always go up to full player volume
# Catalog actions that change what OBS has on air.
_OBS_PROGRAM_ACTIONS = {"obs-scene", "obs-live", "obs-overlay-toggle"}
# ... and every action that changes what GET /obs/status reports.
_OBS_STATUS_ACTIONS = _OBS_PROGRAM_ACTIONS | {"obs-transition-adjust"}


# -- keys ------------------------------------------------------------------


async def pressable_button(request: Request, button_id: str) -> Optional[dict]:
    """A key of Steuerung, or a catalog camera, light or mixer key: those are
    not in Steuerung any more but the camera section and program rows still
    run them by ID."""
    store = request.app.state.companion_button_store
    # An old duplicate OBS key ID (catalog v10) is its native key: it runs
    # while that key is in Steuerung (the migration put it there).
    button = await store.get_button(resolve_alias(button_id))
    if button is None and button_id.startswith("native-cam"):
        # A catalog camera key or a preset saved in the camera section.
        button = next((item for item in await store.camera_buttons() if item["id"] == button_id), None)
    if button is None and button_id in CATALOG_ONLY_IDS:
        button = deepcopy(COMPANION_BUTTONS_BY_ID[button_id])
    return button


def native_action_of(button: dict) -> Optional[dict]:
    """The catalog action a key runs, or None for channel/group keys."""
    if button["kind"] == "native":
        return button.get("native_action", {})
    if button["kind"] == "companion":
        target = COMPANION_BUTTONS_BY_ID.get(resolve_alias(button.get("target_button_id", button["id"])))
        if target is not None and target["kind"] == "native":
            return target["native_action"]
    return None


def switches_mutes(button: dict) -> bool:
    """Channel and group keys and the Overall macros change mutes."""
    if button["kind"] in ("channel", "group"):
        return True
    action = native_action_of(button)
    return bool(action) and action.get("type") == "group-macro"


async def press_native(request: Request, button_id: str, action: dict) -> Result:
    """Run one catalog action (light, camera, OBS, beamer, Overall macro)."""
    if action.get("type") == "group-macro":
        return await _press_group_macro(request, button_id, action)
    if action.get("type") in _OBS_PROGRAM_ACTIONS:
        # A key that puts something on air wins over a camera cut still
        # waiting from an Overall key or a program row.
        camera_program = getattr(request.app.state, "camera_program", None)
        if camera_program is not None:
            camera_program.cancel()
    try:
        result = await request.app.state.native_controls.press(action)
    except NativeControlError as exc:
        log.warning("Native action failed button=%s reason=%s", button_id, exc)
        return {"ok": False, "button": button_id, "reason": str(exc)}, 503
    finally:
        obs_status = getattr(request.app.state, "obs_status", None)
        if obs_status is not None and action.get("type") in _OBS_STATUS_ACTIONS:
            obs_status.invalidate()  # the next poll shows the cut at once
    return {**result, "button": button_id}, 200


async def _press_group_macro(request: Request, button_id: str, action: dict) -> Result:
    """Overall (see the catalog in app/companion_buttons.py): the group's
    mutes first; a blocked group stops the key before the light, the cameras
    or OBS change (a camera cut still waiting from an earlier key is dropped:
    OBS stays as it is). Then
    the stage light fades while the cameras and keys run: only the camera
    that is off air moves now, the cut follows in the background after the
    wait. Every part is tried even if another one fails (a camera that does
    not answer must not keep OBS or the light from switching); the answer
    lists what failed."""
    state = request.app.state
    camera_program = getattr(state, "camera_program", None)
    presets = {cam: (action.get("cameras") or {}).get(cam) for cam in CAMERAS}
    # Its place in the camera order, taken now (this stops a cut still
    # waiting from an earlier key): a key pressed while the mutes are being
    # set wins over this one's cameras.
    camera_token = camera_program.cancel() if camera_program is not None and any(presets.values()) else None
    result = await state.controller.group_set(action["group"], False)
    if not result.get("ok"):
        return controller_result({**result, "button": button_id})
    failed = []
    stage = action.get("stage")
    light = None
    if stage and (stage.get("on") or stage.get("off")):
        light = asyncio.create_task(state.native_controls.press(
            {"type": "artnet-zones-set", "on": stage.get("on") or [], "off": stage.get("off") or []}))

    camera = None
    if any(presets.values()):
        if camera_program is None:
            camera = {"ok": False, "reason": "camera presets unavailable"}
        else:
            camera = await camera_program.run(
                presets, action.get("camera_wait_seconds") or DEFAULT_WAIT_SECONDS,
                COMPANION_BUTTONS_BY_ID.get(button_id, {}).get("label", button_id),
                switch_if_none_live="cam1", token=camera_token)
        if not camera.get("ok"):
            failed.append(f"cameras: {camera.get('reason')}")

    # Its OBS key (PC2, Ende) also cancels a camera cut still waiting from an
    # earlier key (press_native).
    linked = []
    for linked_id in action.get("buttons", []):
        # Linked steps come from the built-in catalog, so renaming or
        # deleting a key in Steuerung does not break the macro.
        target = COMPANION_BUTTONS_BY_ID.get(linked_id)
        if target is None or target["kind"] != "native" or target["native_action"].get("type") == "group-macro":
            data, status = {"ok": False, "reason": "unknown linked action"}, 500
        else:
            data, status = await press_native(request, linked_id, target["native_action"])
        linked.append({"button": linked_id, **data})
        if status != 200 or data.get("ok") is False:
            failed.append(f"{target['label'] if target else linked_id}: {data.get('reason')}")

    light_result = None
    if light is not None:
        try:
            light_result = await light
        except NativeControlError as exc:
            light_result = {"ok": False, "reason": str(exc)}
            failed.append(f"light: {exc}")
            log.warning("Overall light failed button=%s reason=%s", button_id, exc)

    answer = {"ok": not failed, "button": button_id, "group_result": result,
              "light": light_result, "camera": camera, "linked_actions": linked}
    warnings = [data["warning"] for data in linked if data.get("warning")]
    if warnings:
        answer["warning"] = "; ".join(warnings)
    if failed:
        log.warning("Overall %s: mute group set, but %s", button_id, "; ".join(failed))
        return {**answer, "reason": "group set, but " + "; ".join(failed)}, 502
    return answer, 200


async def press_button(request: Request, button_id: str) -> Result:
    button = await pressable_button(request, button_id)
    if button is None:
        return {"ok": False, "reason": "unknown Companion button"}, 404
    controller = request.app.state.controller
    kind = button["kind"]
    if kind == "channel":
        if button["operation"] == "toggle":
            result = await controller.channel_toggle(button["channel"])
        else:
            result = await controller.channel_set(button["channel"], button["operation"] == "mute")
        return controller_result({**result, "button": button_id})
    if kind == "group":
        if button["operation"] == "toggle":
            result = await controller.group_toggle(button["group"])
        else:
            result = await controller.group_set(button["group"], button["operation"] == "mute")
        return controller_result({**result, "button": button_id})
    action = native_action_of(button)
    if action is None:
        return {"ok": False, "reason": "invalid control button target"}, 400
    result, status = await press_native(request, button_id, action)
    if kind == "companion":
        result["target_button"] = button.get("target_button_id", button["id"])
    return result, status


# -- desktop profiles --------------------------------------------------------


def _profile(request: Request, profile_id: str):
    return request.app.state.cfg.desktop_display.profiles.get(profile_id)


def network_program(kind: str, path: str) -> bool:
    """A program on a network share: never started (anyone who can write
    there could swap it)."""
    # A name ending in "*" may resolve to an .exe on the target PC, too; a
    # "file" profile opening an .exe or .bat would run it just the same.
    return runs_from_network_share(path, may_be_program=kind == "program")


async def _no_file_reason(request: Request, profile, path: str) -> str:
    """Why a profile's file was not found; Mission Spotlight names its week."""
    spotlight = spotlight_search() if profile.mission_spotlight else None
    return await no_file_reason(request, profile.target_ip, path, spotlight)


async def show_profile(request: Request, profile_id: str) -> Result:
    """Show an image, start a program or open a file on the profile's PC."""
    profile = _profile(request, profile_id)
    if profile is None:
        return {"ok": False, "reason": "unknown desktop profile"}, 404
    if profile.kind == "audio":
        return {"ok": False, "reason": "audio profiles use the audio endpoint"}, 400
    if network_program(profile.kind, profile.path):
        return {"ok": False, "reason": "programs must be on the target PC, not on a network share"}, 400
    cfg = request.app.state.cfg.desktop_display
    # Exactly the keys the agent expects (it rejects any other).
    payload = {"id": profile_id, **profile.model_dump(exclude=_AGENT_EXCLUDED_FIELDS),
               "path": profile_path(profile)}
    # Only sent when used, so a single-monitor profile still works with an older agent.
    if profile.monitors:
        if profile.kind != "image":
            return {"ok": False, "reason": "several monitors are only possible for images"}, 400
        payload["monitors"] = profile.monitors
    # A video plays in mpv on the right monitor if the agent finds mpv.exe
    # there (desktop_agent: its own choice, for every video).
    # A document is answered once its first page is on screen (app/documents.py).
    timeout = documents.DISPLAY_TIMEOUT_SECONDS if profile.kind == "file" and documents.is_document(
        profile.path) else 3.0
    try:
        answer = await _tracked_start(request, profile_id, profile, lambda extra: display_agent.call(
            cfg, profile.target_ip, "/display", {**payload, **extra}, timeout=timeout))
    except AgentError as exc:
        if exc.agent_status == 503:
            return {"ok": False, "reason": await _no_file_reason(request, profile, payload["path"])}, 502
        return exc.answer(), exc.status
    if profile.kind == "file" and documents.is_document(profile.path) and answer.get("ok") is not False:
        # Shown page by page by the agent: paging goes there (app/documents.py).
        documents.note_shown(request.app.state, profile.target_ip, profile_id)
    return answer, 200


def _shown_profiles(request: Request):
    """What desktop profiles are shown (app/profile_stops.py), or None."""
    return getattr(request.app.state, "shown_profiles", None)


async def _tracked_start(request: Request, profile_id: str, profile, send) -> dict:
    """Send a profile's start (send(extra): the agent call, extra fields
    added) through the notes of app/profile_stops.py. A program row's start
    is noted once the agent accepted it (it stops when the program moves
    on), or noted without its stamp when the agent did not answer in time;
    one by hand takes the profile over (it keeps running). A program row
    asks the agent to keep music that still plays as it is, and a video
    window still open if an earlier program row opened it (one left open by
    hand is opened again). An agent without these fields (stamp, keep)
    gets the start without them. Never waits for a stop. Raises AgentError
    as send()."""
    shown = _shown_profiles(request)
    if shown is None:
        return await send({})
    by_program_row = program_row() is not None
    keep_window = by_program_row and profile.kind == "file" and shown.program_has(profile_id)
    stamp = shown.starting(profile_id)
    extra = {"stamp": stamp}
    if by_program_row and profile.kind == "audio":
        extra["keep_if_playing"] = True
    if keep_window:
        extra["keep_if_open"] = True
    try:
        answer = await send(extra)
    except AgentError as exc:
        if exc.agent_status is None and exc.status == 502:
            shown.timed_out(profile_id, profile.target_ip, profile.kind, stamp)
        if exc.agent_status != 400:
            raise
        # An agent from before start stamps rejects the extra fields (stamp,
        # keep_if_playing / keep_if_open): the start as it knows it.
        log.warning("Agent %s rejected %s; sent again without (update the desktop agent)",
                    profile.target_ip, ", ".join(sorted(extra)))
        answer = await send({})
    if answer.get("ok") is not False:
        shown.accepted(profile_id, profile.target_ip, profile.kind, stamp)
    return answer


async def control_audio(request: Request, profile_id: str, action: str) -> Result:
    profile = _profile(request, profile_id)
    if profile is None:
        return {"ok": False, "reason": "unknown desktop profile"}, 404
    if profile.kind != "audio":
        return {"ok": False, "reason": "profile is not an audio profile"}, 400
    if action not in ("play", "stop"):
        return {"ok": False, "reason": "unknown audio action"}, 404
    payload = {"id": profile_id, "action": action, "path": profile_path(profile) if action == "play" else None}
    if profile.fade:
        # Play fades in from silence to full volume (0 dB = mpv 100 %), stop
        # fades out again. The agent's protocol still names the level; it is
        # always full volume now, so every agent version accepts it.
        payload["fade"] = {"db": AUDIO_FADE_FULL_DB, "seconds": profile.fade_seconds}
    cfg = request.app.state.cfg.desktop_display

    def send(extra: dict):
        return display_agent.call(cfg, profile.target_ip, "/audio", {**payload, **extra},
                                  timeout=display_agent.AUDIO_TIMEOUT_SECONDS)

    try:
        answer = await (_tracked_start(request, profile_id, profile, send) if action == "play" else send({}))
    except AgentError as exc:
        if exc.agent_status is None:
            return exc.answer(), exc.status
        if exc.agent_status == 503 and action == "play":
            # The agent says why the player did not start; anything else at 503
            # means the file is missing there.
            agent_reason = (exc.body or {}).get("reason")
            if not (isinstance(agent_reason, str) and agent_reason.startswith(PLAYER_FAILURE_PREFIX)):
                return {"ok": False, "reason": await _no_file_reason(request, profile, payload["path"])}, 502
        if exc.body is not None:
            return exc.body, exc.agent_status
        reason = "display agent rejected the audio request"
        if "fade" in payload and exc.agent_status == 400:
            # Agents before fading reject the extra field.
            reason += "; update the desktop agent on this PC for fading"
        return {"ok": False, "reason": reason}, exc.agent_status
    shown = _shown_profiles(request)
    if shown is not None and action == "stop" and answer.get("ok") is not False:
        shown.audio_stopped(profile.target_ip)
    return answer, 200


async def run_profile(request: Request, profile_id: str) -> Result:
    """A profile from a program row: audio profiles start playing."""
    profile = _profile(request, profile_id)
    if profile is not None and profile.kind == "audio":
        return await control_audio(request, profile_id, "play")
    return await show_profile(request, profile_id)


# -- program action fields --------------------------------------------------


async def run_action(request: Request, value: str) -> Result:
    """Run the value of a program action field."""
    if is_file_action(value):
        return await run_file_action(request, value)
    if value.startswith(DESKTOP_PREFIX):
        return await run_profile(request, value.removeprefix(DESKTOP_PREFIX))
    return await press_button(request, value)


async def program_reference_error(request: Request, item: dict) -> Optional[str]:
    """Reason why a program row (or a shortcut) cannot run, or None. Checked
    on save and again before play, since a key or group may have been
    deleted since."""
    state = request.app.state
    if item.get("group") and await state.groups.get(item["group"]) is None:
        return "unknown group"
    for field in PROGRAM_ACTION_FIELDS:
        value = item.get(field)
        if not value or is_file_action(value):
            continue  # file paths are checked when saved; the file itself when it runs
        if value.startswith(DESKTOP_PREFIX):
            profile_id = value.removeprefix(DESKTOP_PREFIX)
            if not profile_id or profile_id not in state.cfg.desktop_display.profiles:
                return f"unknown {field} desktop profile"
        elif await pressable_button(request, value) is None:
            return f"unknown {field} button"
    for profile_id in item.get("desktop") or []:
        if profile_id not in state.cfg.desktop_display.profiles:
            return f"unknown desktop profile {profile_id}"
    # Only checked once the library has been read (it lives on the display PC).
    songs = state.songs.songs
    if item.get("song") and songs and item["song"] not in songs:
        return "unknown song"
    scenes = getattr(state, "light_scenes", None)
    if item.get("light") and (scenes is None or await scenes.get(item["light"]) is None):
        return "unknown light scene"
    return None


async def program_conflict_error(request: Request, item: dict) -> Optional[str]:
    """Rows that would fight over the mutes: a row with a mute group sets
    the mutes itself, so it may not also run a key that switches mutes."""
    if not item.get("group"):
        return None
    for field in PROGRAM_ACTION_FIELDS:
        value = item.get(field)
        if not value or is_file_action(value) or value.startswith(DESKTOP_PREFIX):
            continue
        button = await pressable_button(request, value)
        if button is not None and switches_mutes(button):
            return f"{field}: this key switches mutes; a row with a mute group sets them itself"
    return None


async def program_area_problems(request: Request, item: dict) -> dict:
    """Per area of a program row (or shortcut), why it cannot run: "mixer",
    "playback", "translation", "lyrics", "desktop" (profiles that are gone;
    the others run), "song", "light". A row runs every area without one
    (app/api/program.py), so a deleted key or light scene costs only its
    own part. A key that switches mutes in a row with a mute group is
    skipped too (the group sets the mutes itself)."""
    state = request.app.state
    problems = {}
    if item.get("group") and await state.groups.get(item["group"]) is None:
        problems["mixer"] = "unknown group"
    for field in PROGRAM_ACTION_FIELDS:
        value = item.get(field)
        if not value or is_file_action(value):
            continue
        if value.startswith(DESKTOP_PREFIX):
            profile_id = value.removeprefix(DESKTOP_PREFIX)
            if not profile_id or profile_id not in state.cfg.desktop_display.profiles:
                problems[field] = "unknown desktop profile"
            continue
        button = await pressable_button(request, value)
        if button is None:
            problems[field] = "unknown button"
        elif item.get("group") and switches_mutes(button):
            problems[field] = "this key switches mutes; the row's mute group sets them itself"
    gone = [p for p in item.get("desktop") or [] if p not in state.cfg.desktop_display.profiles]
    if gone:
        problems["desktop"] = "unknown desktop profile " + ", ".join(gone)
    songs = state.songs.songs
    if item.get("song") and songs and item["song"] not in songs:
        problems["song"] = "unknown song"
    scenes = getattr(state, "light_scenes", None)
    if item.get("light") and (scenes is None or await scenes.get(item["light"]) is None):
        problems["light"] = "unknown light scene"
    return problems
