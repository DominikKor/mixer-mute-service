"""FastAPI routes. Handlers stay thin: they call the controller and translate
its JSON-compatible result into an HTTP status code. On any block the service
returns 4xx with a clear reason (never a silent success)."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import Any, Tuple

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .camera_streams import CameraStreamError
from .config import DesktopProfileConfig, default_config_path
from .companion_buttons import COMPANION_BUTTONS, COMPANION_BUTTONS_BY_ID
from .desktop_profiles import ProfileEditError, add_profile, delete_profile, update_profile
from .groups import GroupError
from .native_controls import NativeControlError
from .program_items import ProgramItemError
from .songs import show_slide
from .models import (
    AlwaysOnRequest,
    BeamerStateRequest,
    CameraStreamRequest,
    GroupCaptureRequest,
    CompanionButtonCreate,
    CompanionButtonUpdate,
    CompanionCategoryCreate,
    CompanionCategoryVisibility,
    FaderRequest,
    GroupCreate,
    GroupRename,
    GroupUpdate,
    ProgramItemRequest,
    RecallFaderRequest,
)
from .protocol import fader_hex_to_value
from .safety import is_channel_safe
from .state import ChannelState

router = APIRouter()
log = logging.getLogger("mixer.routes")
# One config.yaml edit at a time.
_profile_lock = asyncio.Lock()


def _ctx(request: Request):
    app = request.app
    return app.state.cfg, app.state.store, app.state.client, app.state.controller


def _status_for(result: dict) -> int:
    """Map a blocked result to an HTTP status code."""
    if result.get("ok"):
        return 200
    reason = result.get("reason", "")
    if reason in ("unknown group",):
        return 404
    if reason in ("invalid channel", "invalid fader value", "invalid fader_hex"):
        return 400
    # stale / unknown fader / disabled / no connection / unsafe group -> conflict
    return 409


def _respond(result: dict) -> JSONResponse:
    return JSONResponse(result, status_code=_status_for(result))


def _forward_display_request(url: str, token: str, profile: dict) -> dict:
    payload = json.dumps(profile).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json", "X-Display-Token": token},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=3) as response:
        return json.loads(response.read().decode("utf-8"))


def _unwrap(response: Any) -> Tuple[dict, int]:
    """Body and status of a handler result (a dict or a JSONResponse)."""
    if isinstance(response, JSONResponse):
        return json.loads(response.body.decode("utf-8")), response.status_code
    return response, 200


@router.get("/companion/buttons")
async def companion_buttons(request: Request):
    data = await request.app.state.companion_button_store.snapshot()
    present = {button["id"] for button in data["buttons"]}
    data["companion_targets"] = [
        button for button in COMPANION_BUTTONS
        if button["kind"] != "channel" and button["id"] in present
    ]
    return data


@router.post("/companion/categories")
async def companion_category_create(body: CompanionCategoryCreate, request: Request):
    try:
        category = await request.app.state.companion_button_store.create_category(body.name)
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, 400)
    return {"ok": True, "category": category}


@router.post("/companion/categories/{category_id}/visibility")
async def companion_category_visibility(
    category_id: str, body: CompanionCategoryVisibility, request: Request
):
    try:
        category = await request.app.state.companion_button_store.set_category_visible(
            category_id, body.visible
        )
    except KeyError:
        return JSONResponse({"ok": False, "reason": "unknown category"}, 404)
    return {"ok": True, "category": category}


@router.post("/companion/buttons")
async def companion_button_create(body: CompanionButtonCreate, request: Request):
    cfg = request.app.state.cfg
    if body.kind == "companion":
        target = COMPANION_BUTTONS_BY_ID.get(body.target_button_id or "")
        if target is None or target["kind"] != "native":
            return JSONResponse({"ok": False, "reason": "invalid control button target"}, 400)
        action = {"kind": "companion", "target_button_id": target["id"]}
    elif body.kind == "channel":
        if body.channel not in cfg.valid_channels():
            return JSONResponse({"ok": False, "reason": "invalid channel"}, 400)
        if body.operation is None:
            return JSONResponse({"ok": False, "reason": "operation is required"}, 400)
        action = {"kind": "channel", "channel": body.channel, "operation": body.operation}
    else:
        if not body.group or await request.app.state.groups.get(body.group) is None:
            return JSONResponse({"ok": False, "reason": "unknown group"}, 404)
        if body.operation is None:
            return JSONResponse({"ok": False, "reason": "operation is required"}, 400)
        action = {"kind": "group", "group": body.group, "operation": body.operation}

    try:
        button = await request.app.state.companion_button_store.create_button(
            body.label, body.category_id, action
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, 400)
    except KeyError:
        return JSONResponse({"ok": False, "reason": "unknown category"}, 404)
    return {"ok": True, "button": button}


@router.post("/companion/buttons/{button_id}/update")
async def companion_button_update(
    button_id: str, body: CompanionButtonUpdate, request: Request
):
    try:
        button = await request.app.state.companion_button_store.update_button(
            button_id, body.label, body.category_id
        )
    except ValueError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, 400)
    except KeyError:
        return JSONResponse({"ok": False, "reason": "unknown button or category"}, 404)
    return {"ok": True, "button": button}


@router.post("/companion/buttons/{button_id}/delete")
async def companion_button_delete(button_id: str, request: Request):
    try:
        await request.app.state.companion_button_store.delete_button(button_id)
    except KeyError:
        return JSONResponse({"ok": False, "reason": "unknown button"}, 404)
    return {"ok": True, "button_id": button_id, "deleted": True}


async def _press_native(button_id: str, action: dict, request: Request):
    """Run one native catalog action (light, camera, OBS, group macro)."""
    if action.get("type") == "group-macro":
        _, _, _, controller = _ctx(request)
        result = await controller.group_set(action["group"], False)
        if not result.get("ok"):
            return _respond({**result, "button": button_id})
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
                linked_data, linked_status = {"ok": False, "reason": "unknown linked action"}, 500
            else:
                linked_data, linked_status = _unwrap(await _press_native(linked_id, linked_action, request))
            linked.append({"button": linked_id, **linked_data})
            if linked_status != 200 or linked_data.get("ok") is False:
                return JSONResponse({
                    "ok": False,
                    "button": button_id,
                    "reason": "linked native action failed",
                    "group_result": result,
                    "linked_actions": linked,
                }, linked_status)
        return {"ok": True, "button": button_id, "group_result": result, "linked_actions": linked}
    try:
        result = await request.app.state.native_controls.press(action)
    except NativeControlError as exc:
        log.warning("Native action failed button=%s reason=%s", button_id, exc)
        return JSONResponse({"ok": False, "button": button_id, "reason": str(exc)}, 503)
    return {**result, "button": button_id}


@router.post("/companion/buttons/{button_id}/press")
async def companion_button_press(button_id: str, request: Request):
    button = await request.app.state.companion_button_store.get_button(button_id)
    if button is None:
        return JSONResponse({"ok": False, "reason": "unknown Companion button"}, 404)

    _, _, _, controller = _ctx(request)
    kind = button["kind"]
    if kind == "native":
        return await _press_native(button_id, button.get("native_action", {}), request)
    if kind == "channel":
        if button["operation"] == "toggle":
            result = await controller.channel_toggle(button["channel"])
        else:
            result = await controller.channel_set(
                button["channel"], button["operation"] == "mute"
            )
        return _respond({**result, "button": button_id})
    if kind == "group":
        if button["operation"] == "toggle":
            result = await controller.group_toggle(button["group"])
        else:
            result = await controller.group_set(
                button["group"], button["operation"] == "mute"
            )
        return _respond({**result, "button": button_id})
    if kind == "companion":
        # A user key that points at a native catalog action.
        target_id = button.get("target_button_id", button["id"])
        target = COMPANION_BUTTONS_BY_ID.get(target_id)
        if target is None or target["kind"] != "native":
            return JSONResponse({"ok": False, "reason": "invalid control button target"}, 400)
        result = await _press_native(button_id, target["native_action"], request)
        if isinstance(result, dict):
            result["target_button"] = target_id
        return result
    return JSONResponse({"ok": False, "reason": "invalid button action"}, 400)


@router.get("/health")
async def health(request: Request):
    cfg, store, client, controller = _ctx(request)
    groups = await request.app.state.groups.list()
    return {
        "ok": True,
        "enabled": controller.enabled,
        "mixer_connected": client.connected,
        "last_update_age_seconds": _round(await store.last_update_age()),
        "build_id": getattr(request.app.state, "build_id", "dev"),
        "group_count": len(groups),
        "desktop_display_enabled": cfg.desktop_display.enabled,
        "desktop_display_ready": _display_ready(cfg.desktop_display),
    }


def _display_ready(display_cfg) -> bool:
    return display_cfg.enabled and bool(os.environ.get(display_cfg.token_env, ""))


@router.get("/beamer/state")
async def beamer_state(request: Request):
    hidden = await request.app.state.native_controls.get_beamer_state()
    return {
        "ok": True,
        "hidden": hidden,
        "mode": None if hidden is None else ("hide" if hidden else "unhide"),
    }


@router.post("/beamer/state")
async def set_beamer_state(body: BeamerStateRequest, request: Request):
    return await request.app.state.native_controls.set_beamer_state(body.hidden)


@router.get("/camera-streams")
async def camera_streams(request: Request):
    store = request.app.state.camera_streams
    relay = request.app.state.camera_relay
    return {
        "ok": True,
        "relay_ready": relay.ready,
        "reason": relay.reason,
        "cameras": await store.public_snapshot(),
    }


@router.post("/camera-streams")
async def camera_stream_create(body: CameraStreamRequest, request: Request):
    store = request.app.state.camera_streams
    try:
        camera = await store.add(body.name, body.rtsp_url, body.username, body.password)
    except CameraStreamError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, 400)
    relay = request.app.state.camera_relay
    status = await relay.configure(await store.relay_snapshot())
    return {"ok": True, "camera": camera, "relay_ready": status["ready"], "reason": status["reason"]}


@router.put("/camera-streams/{camera_id}")
async def camera_stream_update(camera_id: str, body: CameraStreamRequest, request: Request):
    store = request.app.state.camera_streams
    try:
        camera = await store.update(camera_id, body.name, body.rtsp_url, body.username, body.password)
    except KeyError:
        return JSONResponse({"ok": False, "reason": "unknown camera"}, 404)
    except CameraStreamError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, 400)
    relay = request.app.state.camera_relay
    status = await relay.configure(await store.relay_snapshot())
    return {"ok": True, "camera": camera, "relay_ready": status["ready"], "reason": status["reason"]}


@router.delete("/camera-streams/{camera_id}")
async def camera_stream_delete(camera_id: str, request: Request):
    store = request.app.state.camera_streams
    try:
        await store.delete(camera_id)
    except KeyError:
        return JSONResponse({"ok": False, "reason": "unknown camera"}, 404)
    relay = request.app.state.camera_relay
    status = await relay.configure(await store.relay_snapshot())
    return {"ok": True, "deleted": True, "relay_ready": status["ready"], "reason": status["reason"]}


@router.get("/desktop/profiles")
async def desktop_profiles(request: Request):
    cfg = request.app.state.cfg.desktop_display
    return {
        "ready": _display_ready(cfg),
        "profiles": [
            {
                "id": profile_id,
                "title": profile.title,
                "kind": profile.kind,
                "path": profile.path,
                "args": profile.args,
                "target_ip": profile.target_ip,
                "fullscreen": profile.fullscreen,
                "monitor": profile.monitor,
                "position": profile.position,
            }
            for profile_id, profile in cfg.profiles.items()
        ],
    }


def _validation_reason(exc: ValidationError) -> str:
    err = exc.errors()[0]
    field = ".".join(str(part) for part in err.get("loc", ())) or "profile"
    return f"{field}: {err.get('msg', 'invalid value')}"


async def _save_profile(request: Request, profile_id: str, body: dict, create: bool):
    if not isinstance(body, dict):
        return JSONResponse({"ok": False, "reason": "profile must be a JSON object"}, 400)
    fields = {k: v for k, v in body.items() if k != "id"}
    try:
        profile = DesktopProfileConfig(**fields)
    except ValidationError as exc:
        return JSONResponse({"ok": False, "reason": _validation_reason(exc)}, 400)
    edit = add_profile if create else update_profile
    path = default_config_path()
    async with _profile_lock:
        try:
            cfg = await asyncio.to_thread(edit, path, profile_id, profile)
        except ProfileEditError as exc:
            log.warning("Desktop profile %s not saved: %s", profile_id, exc)
            return JSONResponse({"ok": False, "id": profile_id, "reason": str(exc)}, exc.status)
        except OSError as exc:
            log.warning("Desktop profile %s not saved: %s", profile_id, exc)
            return JSONResponse({"ok": False, "id": profile_id, "reason": f"config.yaml not writable: {exc}"}, 500)
        # Buttons appear/change without a restart; other settings still need one.
        request.app.state.cfg.desktop_display.profiles = cfg.desktop_display.profiles
    log.info("Desktop profile %s %s in %s", profile_id, "added" if create else "updated", path)
    return {"ok": True, "id": profile_id, "created": create}


@router.post("/desktop/profiles")
async def create_desktop_profile(request: Request, body: dict = Body(...)):
    profile_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(profile_id, str):
        return JSONResponse({"ok": False, "reason": "id is required"}, 400)
    return await _save_profile(request, profile_id, body, create=True)


@router.put("/desktop/profiles/{profile_id}")
async def update_desktop_profile(profile_id: str, request: Request, body: dict = Body(...)):
    return await _save_profile(request, profile_id, body, create=False)


@router.post("/desktop/profiles/{profile_id}/update")
async def update_desktop_profile_post(profile_id: str, request: Request, body: dict = Body(...)):
    return await _save_profile(request, profile_id, body, create=False)


@router.delete("/desktop/profiles/{profile_id}")
async def delete_desktop_profile(profile_id: str, request: Request):
    path = default_config_path()
    async with _profile_lock:
        try:
            cfg = await asyncio.to_thread(delete_profile, path, profile_id)
        except ProfileEditError as exc:
            return JSONResponse({"ok": False, "id": profile_id, "reason": str(exc)}, exc.status)
        except OSError as exc:
            return JSONResponse({"ok": False, "id": profile_id, "reason": f"config.yaml not writable: {exc}"}, 500)
        request.app.state.cfg.desktop_display.profiles = cfg.desktop_display.profiles
    log.info("Desktop profile %s deleted from %s", profile_id, path)
    return {"ok": True, "id": profile_id, "deleted": True}


@router.post("/desktop/{profile_id}/open")
async def open_desktop_profile(profile_id: str, request: Request):
    cfg = request.app.state.cfg.desktop_display
    profile = cfg.profiles.get(profile_id)
    if profile is None:
        return JSONResponse({"ok": False, "reason": "unknown desktop profile"}, 404)
    if profile.kind == "audio":
        return JSONResponse({"ok": False, "reason": "audio profiles use the audio endpoint"}, 400)
    token = os.environ.get(cfg.token_env, "")
    if not cfg.enabled or not token:
        return JSONResponse(
            {"ok": False, "reason": "desktop display is not configured"}, 503
        )
    try:
        return await asyncio.to_thread(
            _forward_display_request,
            f"http://{profile.target_ip}:8766/display",
            token,
            {"id": profile_id, **profile.model_dump(exclude={"target_ip"})},
        )
    except urllib.error.HTTPError as exc:
        log.warning("Display agent rejected request: HTTP %s", exc.code)
        return JSONResponse(
            {"ok": False, "reason": "display agent rejected the request"}, 502
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("Display agent unavailable: %s", exc)
        return JSONResponse(
            {"ok": False, "reason": "display agent unavailable"}, 502
        )


@router.post("/desktop/{profile_id}/audio/{action}")
async def control_desktop_audio(profile_id: str, action: str, request: Request):
    cfg = request.app.state.cfg.desktop_display
    profile = cfg.profiles.get(profile_id)
    if profile is None:
        return JSONResponse({"ok": False, "reason": "unknown desktop profile"}, 404)
    if profile.kind != "audio":
        return JSONResponse({"ok": False, "reason": "profile is not an audio profile"}, 400)
    if action not in ("play", "stop"):
        return JSONResponse({"ok": False, "reason": "unknown audio action"}, 404)
    token = os.environ.get(cfg.token_env, "")
    if not cfg.enabled or not token:
        return JSONResponse(
            {"ok": False, "reason": "desktop display is not configured"}, 503
        )
    payload = {
        "id": profile_id,
        "action": action,
        "path": profile.path if action == "play" else None,
    }
    try:
        return await asyncio.to_thread(
            _forward_display_request,
            f"http://{profile.target_ip}:8766/audio",
            token,
            payload,
        )
    except urllib.error.HTTPError as exc:
        try:
            details = json.loads(exc.read().decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            details = {"ok": False, "reason": "display agent rejected the audio request"}
        log.warning("Display agent audio request failed: HTTP %s", exc.code)
        return JSONResponse(details, exc.code)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        log.warning("Display agent unavailable for audio: %s", exc)
        return JSONResponse(
            {"ok": False, "reason": "display agent unavailable"}, 502
        )


def _channel_view(
    request: Request, channel: int, cs: "ChannelState | None", always_on: set, now: float
) -> dict:
    cfg, _, client, controller = _ctx(request)
    fader_age = None if (cs is None or cs.fader_updated_at is None) else now - cs.fader_updated_at
    mute_age = None if (cs is None or cs.muted_updated_at is None) else now - cs.muted_updated_at
    return {
        "name": cfg.channel_label(channel),
        "fader_hex": None if cs is None else cs.fader_hex,
        "fader_value": None if cs is None else fader_hex_to_value(cs.fader_hex),
        "fader_age_seconds": _round(fader_age),
        "muted": None if cs is None else cs.muted,
        "solo": None if cs is None else cs.solo,
        "mute_age_seconds": _round(mute_age),
        "safe": (
            client.connected and controller.enabled
            and is_channel_safe(channel, cs, cfg, client.session_epoch)
        ),
        "always_on": channel in always_on,
    }


@router.get("/state")
async def state(request: Request):
    cfg, store, client, controller = _ctx(request)
    now = time.monotonic()
    snap = await store.snapshot()
    always_on = await controller.always_on_channels()
    return {
        "enabled": controller.enabled,
        "mixer_connected": client.connected,
        "channels": {
            str(ch): _channel_view(request, ch, snap.get(ch), always_on, now)
            for ch in cfg.all_channels()
        },
    }


@router.get("/channels/{channel}")
async def get_channel(channel: int, request: Request):
    cfg, store, _, controller = _ctx(request)
    if channel not in cfg.valid_channels():
        return JSONResponse({"ok": False, "channel": channel, "reason": "invalid channel"}, 400)
    cs = await store.get_channel(channel)
    always_on = await controller.always_on_channels()
    return {"channel": channel, **_channel_view(request, channel, cs, always_on, time.monotonic())}


@router.post("/channels/{channel}/mute")
async def channel_mute(channel: int, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.channel_set(channel, True))


@router.post("/channels/{channel}/unmute")
async def channel_unmute(channel: int, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.channel_set(channel, False))


@router.post("/channels/{channel}/always-on")
async def channel_always_on(channel: int, body: AlwaysOnRequest, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.set_channel_always_on(channel, body.enabled))


@router.post("/channels/{channel}/toggle")
async def channel_toggle(channel: int, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.channel_toggle(channel))


@router.post("/channels/{channel}/fader")
async def channel_fader(channel: int, body: FaderRequest, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.channel_set_fader(channel, body.value))


@router.post("/channels/{channel}/recall-fader")
async def channel_recall_fader(channel: int, body: RecallFaderRequest, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.channel_recall_fader(channel, body.fader_hex))


@router.post("/groups/{group}/mute")
async def group_mute(group: str, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.group_set(group, True))


@router.post("/groups/{group}/unmute")
async def group_unmute(group: str, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.group_set(group, False))


@router.post("/groups/{group}/toggle")
async def group_toggle(group: str, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.group_toggle(group))


@router.post("/groups/from-current-status")
async def create_group_from_current_status(body: GroupCaptureRequest, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.create_group_from_current_status(body.name))


_PROGRAM_ACTION_FIELDS = ("playback", "translation", "lyrics")


async def _program_reference_error(item: dict, request: Request):
    """Reason why a program row cannot run, or None. Checked on save and
    again before play, since a key or group may have been deleted since."""
    if item.get("group") and await request.app.state.groups.get(item["group"]) is None:
        return "unknown group"
    for field in _PROGRAM_ACTION_FIELDS:
        button_id = item.get(field)
        if not button_id:
            continue
        if button_id.startswith("desktop:"):
            profile_id = button_id.removeprefix("desktop:")
            if not profile_id or profile_id not in request.app.state.cfg.desktop_display.profiles:
                return f"unknown {field} desktop profile"
        elif await request.app.state.companion_button_store.get_button(button_id) is None:
            return f"unknown {field} button"
    # Only checked once the library has been read (it lives on the display PC).
    songs = request.app.state.songs.songs
    if item.get("song") and songs and item["song"] not in songs:
        return "unknown song"
    return None


async def _validate_program_item(body: ProgramItemRequest, request: Request):
    reason = await _program_reference_error(body.model_dump(), request)
    if reason is None:
        return None
    return JSONResponse({"ok": False, "reason": reason}, 404 if reason == "unknown group" else 400)


@router.get("/program/items")
async def list_program_items(request: Request):
    return await request.app.state.program_items.list()


@router.post("/program/items")
async def create_program_item(body: ProgramItemRequest, request: Request):
    invalid = await _validate_program_item(body, request)
    if invalid:
        return invalid
    try:
        item = await request.app.state.program_items.create(body.model_dump())
    except ProgramItemError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, 400)
    return {"ok": True, "item": item}


@router.put("/program/items/{item_id}")
async def update_program_item(item_id: str, body: ProgramItemRequest, request: Request):
    invalid = await _validate_program_item(body, request)
    if invalid:
        return invalid
    try:
        item = await request.app.state.program_items.update(item_id, body.model_dump())
    except ProgramItemError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, 400)
    if item is None:
        return JSONResponse({"ok": False, "reason": "unknown program item"}, 404)
    return {"ok": True, "item": item}


@router.delete("/program/items/{item_id}")
async def delete_program_item(item_id: str, request: Request):
    deleted = await request.app.state.program_items.delete(item_id)
    if not deleted:
        return JSONResponse({"ok": False, "reason": "unknown program item"}, 404)
    return {"ok": True, "item_id": item_id, "deleted": True}


@router.get("/program/status")
async def program_status(request: Request):
    """The row that ran last today and the one "Weiter" would start."""
    return await request.app.state.program_items.status()


@router.post("/program/items/{item_id}/duplicate")
async def duplicate_program_item(item_id: str, request: Request):
    item = await request.app.state.program_items.duplicate(item_id)
    if item is None:
        return JSONResponse({"ok": False, "reason": "unknown program item"}, 404)
    return {"ok": True, "item": item}


@router.post("/program/reset")
async def reset_program(request: Request):
    count = await request.app.state.program_items.reset_played()
    return {"ok": True, "reset": count}


# One program start at a time: two quick "Weiter" presses must not both start
# the same row, and a row must not run twice in parallel.
_program_lock = asyncio.Lock()


@router.post("/program/next")
async def play_next_program_item(request: Request):
    async with _program_lock:
        status = await request.app.state.program_items.status()
        if status["next_id"] is None:
            return JSONResponse({"ok": False, "reason": "program finished"}, 409)
        return await _play_program_item(status["next_id"], request)


@router.post("/program/items/{item_id}/play")
async def play_program_item(item_id: str, request: Request):
    async with _program_lock:
        return await _play_program_item(item_id, request)


async def _play_program_item(item_id: str, request: Request):
    program_store = request.app.state.program_items
    item = await program_store.get(item_id)
    if item is None:
        return JSONResponse({"ok": False, "reason": "unknown program item"}, 404)
    # Refuse before anything happens instead of muting and then failing on a
    # deleted key halfway through.
    broken = await _program_reference_error(item, request)
    if broken:
        return JSONResponse({"ok": False, "reason": broken, "program_item_id": item_id}, 409)
    _, _, _, controller = _ctx(request)
    group_result = None
    if item.get("group"):
        group_result = await controller.group_set(item["group"], False)
        if not group_result.get("ok"):
            # Not marked as started: "Weiter" still offers this row.
            return _respond({
                "ok": False,
                "reason": group_result.get("reason", "mute group could not be activated"),
                "details": group_result.get("details"),
                "program_item_id": item_id,
                "group_result": group_result,
            })
    played = await program_store.mark_played(item_id)

    linked_actions = []
    for field in _PROGRAM_ACTION_FIELDS:
        button_id = item.get(field)
        if not button_id:
            continue
        if button_id.startswith("desktop:"):
            response = await open_desktop_profile(button_id.removeprefix("desktop:"), request)
        else:
            response = await companion_button_press(button_id, request)
        result, status_code = _unwrap(response)
        linked_actions.append({"field": field, **result})
        if status_code != 200 or result.get("ok") is False:
            return JSONResponse({
                "ok": False,
                "reason": f"{field} action failed" + (" after group activation" if group_result else ""),
                "program_item_id": item_id,
                "title": item["title"],
                "delay_seconds": played["last_delay_seconds"],
                "group_result": group_result,
                "linked_actions": linked_actions,
            }, status_code)

    song_result = None
    if item.get("song"):
        song_result, status_code = _unwrap(await show_slide(request, item["song"], 0))
        if status_code != 200 or song_result.get("ok") is False:
            return JSONResponse({
                "ok": False,
                "reason": f"song could not be shown: {song_result.get('reason', 'HTTP ' + str(status_code))}",
                "program_item_id": item_id,
                "title": item["title"],
                "delay_seconds": played["last_delay_seconds"],
                "group_result": group_result,
                "linked_actions": linked_actions,
            }, status_code if status_code != 200 else 502)

    return {
        "ok": True,
        "song": song_result,
        "program_item_id": item_id,
        "title": item["title"],
        "delay_seconds": played["last_delay_seconds"],
        "group_result": group_result,
        "linked_actions": linked_actions,
    }


# -- group management (CRUD) ----------------------------------------------


@router.get("/groups")
async def list_groups(request: Request):
    return await request.app.state.groups.list()


@router.post("/groups")
async def create_group(body: GroupCreate, request: Request):
    try:
        channels = await request.app.state.groups.create(body.name, body.channels)
    except GroupError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, 400)
    return {"ok": True, "group": body.name.strip(), "channels": channels}


@router.get("/groups/{group}")
async def get_group(group: str, request: Request):
    channels = await request.app.state.groups.get(group)
    if channels is None:
        return JSONResponse({"ok": False, "group": group, "reason": "unknown group"}, 404)
    return {"group": group, "channels": channels}


async def _update_group(group: str, body: GroupUpdate, request: Request):
    try:
        channels = await request.app.state.groups.update(group, body.channels)
    except GroupError as exc:
        status = 404 if str(exc).startswith("unknown group") else 400
        return JSONResponse({"ok": False, "group": group, "reason": str(exc)}, status)
    return {"ok": True, "group": group, "channels": channels}


@router.post("/groups/{group}/update")
async def update_group_post(group: str, body: GroupUpdate, request: Request):
    return await _update_group(group, body, request)


@router.put("/groups/{group}")
async def update_group(group: str, body: GroupUpdate, request: Request):
    return await _update_group(group, body, request)


async def _delete_group(group: str, request: Request):
    try:
        await request.app.state.groups.delete(group)
    except GroupError as exc:
        return JSONResponse({"ok": False, "group": group, "reason": str(exc)}, 404)
    return {"ok": True, "group": group, "deleted": True}


@router.post("/groups/{group}/delete")
async def delete_group_post(group: str, request: Request):
    return await _delete_group(group, request)


@router.delete("/groups/{group}")
async def delete_group(group: str, request: Request):
    return await _delete_group(group, request)


@router.post("/groups/{group}/rename")
async def rename_group(group: str, body: GroupRename, request: Request):
    """Rename a group and every reference to it (Programm rows, Steuerung keys,
    Overall macros), so nothing that used the old name breaks."""
    state = request.app.state
    try:
        channels = await state.groups.rename(group, body.name)
    except GroupError as exc:
        status = 404 if str(exc).startswith("unknown group") else 400
        return JSONResponse({"ok": False, "group": group, "reason": str(exc)}, status)
    new_name = body.name.strip()
    program_rows = await state.program_items.rename_group(group, new_name)
    keys = await state.companion_button_store.rename_group(group, new_name)
    log.info("Group %r renamed to %r (%d program rows, %d keys)", group, new_name, program_rows, keys)
    return {
        "ok": True, "group": new_name, "old_name": group, "channels": channels,
        "program_items": program_rows, "buttons": keys,
    }


@router.post("/panic/disable")
async def panic_disable(request: Request):
    _, _, _, controller = _ctx(request)
    controller.enabled = False
    return {"ok": True, "enabled": controller.enabled}


@router.post("/panic/enable")
async def panic_enable(request: Request):
    _, _, _, controller = _ctx(request)
    controller.enabled = True
    return {"ok": True, "enabled": controller.enabled}


def _round(value):
    return None if value is None else round(value, 3)
