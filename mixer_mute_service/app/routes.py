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

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .config import DesktopProfileConfig, default_config_path
from .companion_buttons import COMPANION_BUTTONS, COMPANION_BUTTONS_BY_ID
from .desktop_profiles import ProfileEditError, add_profile, update_profile
from .groups import GroupError
from .mute_snapshots import MuteSnapshotError
from .models import (
    AlwaysOnRequest,
    CompanionButtonCreate,
    CompanionButtonUpdate,
    CompanionCategoryCreate,
    CompanionCategoryVisibility,
    FaderRequest,
    GroupCreate,
    GroupUpdate,
    MuteSnapshotCreate,
    RecallFaderRequest,
)
from .protocol import fader_hex_to_value
from .safety import check_channel_send, is_channel_safe

router = APIRouter()
_logger = logging.getLogger("mixer.desktop_display")
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


def _forward_companion_press(base_url: str, page: int, row: int, column: int, timeout: float) -> None:
    url = f"{base_url}/api/location/{page}/{row}/{column}/press"
    req = urllib.request.Request(url, data=b"", method="POST")
    with urllib.request.urlopen(req, timeout=timeout):
        return


@router.get("/companion/buttons")
async def companion_buttons(request: Request):
    data = await request.app.state.companion_button_store.snapshot()
    present = {button["id"] for button in data["buttons"]}
    data["companion_targets"] = [
        button for button in COMPANION_BUTTONS
        if button["kind"] == "companion" and button["id"] in present
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
        if target is None or target["kind"] != "companion":
            return JSONResponse({"ok": False, "reason": "invalid Companion button target"}, 400)
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


@router.post("/companion/buttons/{button_id}/press")
async def companion_button_press(button_id: str, request: Request):
    button = await request.app.state.companion_button_store.get_button(button_id)
    if button is None:
        return JSONResponse({"ok": False, "reason": "unknown Companion button"}, 404)

    _, _, _, controller = _ctx(request)
    if button["kind"] == "channel":
        if button["operation"] == "toggle":
            result = await controller.channel_toggle(button["channel"])
        else:
            result = await controller.channel_set(
                button["channel"], button["operation"] == "mute"
            )
        return _respond({**result, "button": button_id})
    if button["kind"] == "group":
        if button["operation"] == "toggle":
            result = await controller.group_toggle(button["group"])
        else:
            result = await controller.group_set(
                button["group"], button["operation"] == "mute"
            )
        return _respond({**result, "button": button_id})
    if button["kind"] != "companion":
        return JSONResponse({"ok": False, "reason": "invalid button action"}, 400)

    target_id = button.get("target_button_id", button["id"])
    target = COMPANION_BUTTONS_BY_ID.get(target_id)
    if target is None or target["kind"] != "companion":
        return JSONResponse({"ok": False, "reason": "invalid Companion button target"}, 400)

    cfg = request.app.state.cfg.companion
    try:
        await asyncio.to_thread(
            _forward_companion_press,
            cfg.base_url,
            target["page"],
            target["row"],
            target["column"],
            cfg.timeout_seconds,
        )
    except urllib.error.HTTPError as exc:
        reason = "Companion HTTP API is disabled" if exc.code == 403 else f"Companion returned HTTP {exc.code}"
        return JSONResponse({"ok": False, "button": button_id, "reason": reason}, 503 if exc.code == 403 else 502)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        log.warning("Companion press failed button=%s reason=%r", button_id, exc)
        return JSONResponse({"ok": False, "button": button_id, "reason": "Companion is unreachable"}, 503)
    return {"ok": True, "button": button_id}


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
        "desktop_display_ready": (
            cfg.desktop_display.enabled
            and bool(os.environ.get(cfg.desktop_display.token_env, ""))
        ),
    }


def _display_ready(cfg) -> bool:
    return cfg.enabled and bool(os.environ.get(cfg.token_env, ""))


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
            _logger.warning("Desktop profile %s not saved: %s", profile_id, exc)
            return JSONResponse({"ok": False, "id": profile_id, "reason": str(exc)}, exc.status)
        except OSError as exc:
            _logger.warning("Desktop profile %s not saved: %s", profile_id, exc)
            return JSONResponse({"ok": False, "id": profile_id, "reason": f"config.yaml not writable: {exc}"}, 500)
        # Buttons appear/change without a restart; other settings still need one.
        request.app.state.cfg.desktop_display.profiles = cfg.desktop_display.profiles
    _logger.info("Desktop profile %s %s in %s", profile_id, "added" if create else "updated", path)
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


@router.post("/desktop/{profile_id}/open")
async def open_desktop_profile(profile_id: str, request: Request):
    cfg = request.app.state.cfg.desktop_display
    profile = cfg.profiles.get(profile_id)
    if profile is None:
        return JSONResponse({"ok": False, "reason": "unknown desktop profile"}, 404)
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
        _logger.warning("Display agent rejected request: HTTP %s", exc.code)
        return JSONResponse(
            {"ok": False, "reason": "display agent rejected the request"}, 502
        )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        _logger.warning("Display agent unavailable: %s", exc)
        return JSONResponse(
            {"ok": False, "reason": "display agent unavailable"}, 502
        )


@router.get("/state")
async def state(request: Request):
    cfg, store, client, controller = _ctx(request)
    now = time.monotonic()
    snap = await store.snapshot()
    always_on = await controller.always_on_channels()

    channels = {}
    for ch in cfg.all_channels():
        cs = snap.get(ch)
        fader_age = None if (cs is None or cs.fader_updated_at is None) else now - cs.fader_updated_at
        mute_age = None if (cs is None or cs.muted_updated_at is None) else now - cs.muted_updated_at
        safe = (
            client.connected and controller.enabled
            and is_channel_safe(ch, cs, cfg, client.session_epoch)
        )
        channels[str(ch)] = {
            "name": cfg.channel_label(ch),
            "fader_hex": None if cs is None else cs.fader_hex,
            "fader_value": None if cs is None else fader_hex_to_value(cs.fader_hex),
            "fader_age_seconds": _round(fader_age),
            "muted": None if cs is None else cs.muted,
            "mute_age_seconds": _round(mute_age),
            "safe": safe,
            "always_on": ch in always_on,
        }

    return {
        "enabled": controller.enabled,
        "mixer_connected": client.connected,
        "channels": channels,
    }


@router.get("/channels/{channel}")
async def get_channel(channel: int, request: Request):
    cfg, store, client, controller = _ctx(request)
    if channel not in cfg.valid_channels():
        return JSONResponse({"ok": False, "channel": channel, "reason": "invalid channel"}, 400)
    now = time.monotonic()
    cs = await store.get_channel(channel)
    fader_age = None if (cs is None or cs.fader_updated_at is None) else now - cs.fader_updated_at
    mute_age = None if (cs is None or cs.muted_updated_at is None) else now - cs.muted_updated_at
    return {
        "channel": channel,
        "name": cfg.channel_label(channel),
        "fader_hex": None if cs is None else cs.fader_hex,
        "fader_value": None if cs is None else fader_hex_to_value(cs.fader_hex),
        "fader_age_seconds": _round(fader_age),
        "muted": None if cs is None else cs.muted,
        "mute_age_seconds": _round(mute_age),
        "safe": (
            client.connected and controller.enabled
            and is_channel_safe(channel, cs, cfg, client.session_epoch)
        ),
        "always_on": channel in await controller.always_on_channels(),
    }


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


@router.get("/mute-snapshots")
async def list_mute_snapshots(request: Request):
    return await request.app.state.mute_snapshots.list()


@router.post("/mute-snapshots")
async def create_mute_snapshot(body: MuteSnapshotCreate, request: Request):
    cfg, store, client, controller = _ctx(request)
    if not client.connected:
        return JSONResponse({"ok": False, "reason": "no mixer connection"}, 409)

    states = {}
    unsafe = []
    for channel in cfg.all_channels():
        channel_state = await store.get_channel(channel)
        safety = check_channel_send(
            channel, channel_state, cfg,
            session_epoch=client.session_epoch,
            require_mute_state=True,
        )
        if not safety.ok:
            unsafe.append(safety.as_detail(channel))
        else:
            states[str(channel)] = channel_state.muted

    if unsafe:
        return JSONResponse({
            "ok": False,
            "reason": "cannot capture incomplete channel state",
            "details": unsafe,
        }, 409)
    try:
        snapshot = await request.app.state.mute_snapshots.create(body.name, states)
    except MuteSnapshotError as exc:
        return JSONResponse({"ok": False, "reason": str(exc)}, 400)
    return {"ok": True, "snapshot": snapshot}


@router.post("/mute-snapshots/{snapshot_id}/restore")
async def restore_mute_snapshot(snapshot_id: str, request: Request):
    snapshot = await request.app.state.mute_snapshots.get(snapshot_id)
    if snapshot is None:
        return JSONResponse({"ok": False, "reason": "unknown snapshot"}, 404)
    _, _, _, controller = _ctx(request)
    return _respond(await controller.restore_mute_snapshot(snapshot["name"], snapshot["channels"]))


@router.post("/mute-snapshots/{snapshot_id}/delete")
async def delete_mute_snapshot(snapshot_id: str, request: Request):
    deleted = await request.app.state.mute_snapshots.delete(snapshot_id)
    if not deleted:
        return JSONResponse({"ok": False, "reason": "unknown snapshot"}, 404)
    return {"ok": True, "snapshot_id": snapshot_id, "deleted": True}


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
