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

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .groups import GroupError
from .models import FaderRequest, GroupCreate, GroupUpdate, RecallFaderRequest
from .protocol import fader_hex_to_value
from .safety import is_channel_safe

router = APIRouter()
_logger = logging.getLogger("mixer.desktop_display")


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
                "target_ip": profile.target_ip,
                "fullscreen": profile.fullscreen,
                "monitor": profile.monitor,
                "position": profile.position,
            }
            for profile_id, profile in cfg.profiles.items()
        ],
    }


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
    }


@router.post("/channels/{channel}/mute")
async def channel_mute(channel: int, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.channel_set(channel, True))


@router.post("/channels/{channel}/unmute")
async def channel_unmute(channel: int, request: Request):
    _, _, _, controller = _ctx(request)
    return _respond(await controller.channel_set(channel, False))


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
