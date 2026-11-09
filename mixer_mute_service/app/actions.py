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

import logging
from copy import deepcopy
from typing import Optional

from fastapi import Request

from . import display_agent
from .companion_buttons import CAMERA_BUTTON_IDS, CATALOG_ONLY_IDS, COMPANION_BUTTONS_BY_ID
from .display_agent import AgentError
from .file_actions import is_file_action, not_found_reason, run_file_action, runs_from_network_share
from .native_controls import NativeControlError
from .responses import Result, controller_result

log = logging.getLogger("mixer.actions")

DESKTOP_PREFIX = "desktop:"
PROGRAM_ACTION_FIELDS = ("playback", "translation", "lyrics")
# Profile fields the agent's /display endpoint does not take (it refuses
# unknown keys).
_AGENT_EXCLUDED_FIELDS = {"target_ip", "fade", "fade_seconds"}
AUDIO_FADE_FULL_DB = 0.0  # audio fades always go up to full player volume


# -- keys ------------------------------------------------------------------


async def pressable_button(request: Request, button_id: str) -> Optional[dict]:
    """A key of Steuerung, or a catalog camera, light or mixer key: those are
    not in Steuerung any more but the camera section and program rows still
    run them by ID."""
    store = request.app.state.companion_button_store
    button = await store.get_button(button_id)
    if button is None and button_id in CAMERA_BUTTON_IDS:
        button = next(item for item in await store.camera_buttons() if item["id"] == button_id)
    if button is None and button_id in CATALOG_ONLY_IDS:
        button = deepcopy(COMPANION_BUTTONS_BY_ID[button_id])
    return button


def native_action_of(button: dict) -> Optional[dict]:
    """The catalog action a key runs, or None for channel/group keys."""
    if button["kind"] == "native":
        return button.get("native_action", {})
    if button["kind"] == "companion":
        target = COMPANION_BUTTONS_BY_ID.get(button.get("target_button_id", button["id"]))
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
    try:
        result = await request.app.state.native_controls.press(action)
    except NativeControlError as exc:
        log.warning("Native action failed button=%s reason=%s", button_id, exc)
        return {"ok": False, "button": button_id, "reason": str(exc)}, 503
    return {**result, "button": button_id}, 200


async def _press_group_macro(request: Request, button_id: str, action: dict) -> Result:
    """Overall: the group's mutes, then the light, then the linked keys.
    Stops at the first step that fails."""
    result = await request.app.state.controller.group_set(action["group"], False)
    if not result.get("ok"):
        return controller_result({**result, "button": button_id})
    steps = []
    if action.get("light_zones_on"):
        steps.append(("light-on", {"type": "artnet-zones-on", "zones": action["light_zones_on"]}))
    for linked_id in action.get("buttons", []):
        # Linked steps come from the built-in catalog, so renaming or
        # deleting a key in Steuerung does not break the macro.
        target = COMPANION_BUTTONS_BY_ID.get(linked_id)
        if target is None or target["kind"] != "native" or target["native_action"].get("type") == "group-macro":
            steps.append((linked_id, None))
        else:
            steps.append((linked_id, target["native_action"]))
    linked = []
    for linked_id, linked_action in steps:
        if linked_action is None:
            data, status = {"ok": False, "reason": "unknown linked action"}, 500
        else:
            data, status = await press_native(request, linked_id, linked_action)
        linked.append({"button": linked_id, **data})
        if status != 200 or data.get("ok") is False:
            return {
                "ok": False,
                "button": button_id,
                "reason": "linked native action failed",
                "group_result": result,
                "linked_actions": linked,
            }, status
    return {"ok": True, "button": button_id, "group_result": result, "linked_actions": linked}, 200


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
    payload = {"id": profile_id, **profile.model_dump(exclude=_AGENT_EXCLUDED_FIELDS)}
    try:
        return await display_agent.call(cfg, profile.target_ip, "/display", payload), 200
    except AgentError as exc:
        if exc.agent_status == 503:
            return {"ok": False, "reason": not_found_reason(profile.path)}, 502
        return exc.answer(), exc.status


async def control_audio(request: Request, profile_id: str, action: str) -> Result:
    profile = _profile(request, profile_id)
    if profile is None:
        return {"ok": False, "reason": "unknown desktop profile"}, 404
    if profile.kind != "audio":
        return {"ok": False, "reason": "profile is not an audio profile"}, 400
    if action not in ("play", "stop"):
        return {"ok": False, "reason": "unknown audio action"}, 404
    payload = {"id": profile_id, "action": action, "path": profile.path if action == "play" else None}
    if profile.fade:
        # Play fades in from silence to full volume (0 dB = mpv 100 %), stop
        # fades out again. The agent's protocol still names the level; it is
        # always full volume now, so every agent version accepts it.
        payload["fade"] = {"db": AUDIO_FADE_FULL_DB, "seconds": profile.fade_seconds}
    cfg = request.app.state.cfg.desktop_display
    try:
        return await display_agent.call(cfg, profile.target_ip, "/audio", payload,
                                        timeout=display_agent.AUDIO_TIMEOUT_SECONDS), 200
    except AgentError as exc:
        if exc.agent_status is None:
            return exc.answer(), exc.status
        if exc.body is not None:
            return exc.body, exc.agent_status
        reason = "display agent rejected the audio request"
        if "fade" in payload and exc.agent_status == 400:
            # Agents before fading reject the extra field.
            reason += "; update the desktop agent on this PC for fading"
        return {"ok": False, "reason": reason}, exc.agent_status


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
    """Reason why a program row cannot run, or None. Checked on save and
    again before play, since a key or group may have been deleted since."""
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
