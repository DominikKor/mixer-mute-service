"""Mixer: health, channel state, channel and group mutes, faders, panic."""

from __future__ import annotations

import time

from fastapi import APIRouter, Request

from .. import display_agent
from ..buses import main_output_channels
from ..models import AlwaysOnRequest, FaderRequest, GroupCaptureRequest, RecallFaderRequest
from ..protocol import fader_hex_to_value
from ..responses import fail, respond, round3
from ..state import ChannelState

router = APIRouter()


@router.get("/health")
async def health(request: Request):
    state = request.app.state
    groups = await state.groups.list()
    return {
        "ok": True,
        "enabled": state.controller.enabled,
        "mixer_connected": state.client.connected,
        "last_update_age_seconds": round3(await state.store.last_update_age()),
        "build_id": getattr(state, "build_id", "dev"),
        "group_count": len(groups),
        "unavailable_features": getattr(state, "feature_errors", {}),
        "desktop_display_enabled": state.cfg.desktop_display.enabled,
        "desktop_display_ready": display_agent.is_ready(state.cfg.desktop_display),
    }


def _channel_view(request: Request, channel: int, cs: "ChannelState | None", always_on: set, now: float) -> dict:
    cfg, controller = request.app.state.cfg, request.app.state.controller
    fader_age = None if (cs is None or cs.fader_updated_at is None) else now - cs.fader_updated_at
    mute_age = None if (cs is None or cs.muted_updated_at is None) else now - cs.muted_updated_at
    return {
        "name": cfg.channel_label(channel),
        "fader_hex": None if cs is None else cs.fader_hex,
        "fader_value": None if cs is None else fader_hex_to_value(cs.fader_hex),
        "fader_age_seconds": round3(fader_age),
        "muted": None if cs is None else cs.muted,
        "solo": None if cs is None else cs.solo,
        "mute_age_seconds": round3(mute_age),
        # Same checks as the send path, incl. a stale link (no current dump).
        "safe": controller.is_send_safe(channel, cs),
        "always_on": channel in always_on,
        # the main L/R output: groups and solo never switch it (controller)
        "main_output": channel in main_output_channels(cfg),
    }


@router.get("/state")
async def state(request: Request):
    app_state = request.app.state
    now = time.monotonic()
    snap = await app_state.store.snapshot()
    always_on = await app_state.controller.always_on_channels()
    return {
        "enabled": app_state.controller.enabled,
        "mixer_connected": app_state.client.connected,
        "channels": {
            str(ch): _channel_view(request, ch, snap.get(ch), always_on, now)
            for ch in app_state.cfg.all_channels()
        },
    }


@router.get("/channels/{channel}")
async def get_channel(channel: int, request: Request):
    app_state = request.app.state
    if channel not in app_state.cfg.valid_channels():
        return fail("invalid channel", 400, channel=channel)
    cs = await app_state.store.get_channel(channel)
    always_on = await app_state.controller.always_on_channels()
    return {"channel": channel, **_channel_view(request, channel, cs, always_on, time.monotonic())}


@router.post("/channels/{channel}/mute")
async def channel_mute(channel: int, request: Request):
    return respond(await request.app.state.controller.channel_set(channel, True))


@router.post("/channels/{channel}/unmute")
async def channel_unmute(channel: int, request: Request):
    return respond(await request.app.state.controller.channel_set(channel, False))


@router.post("/channels/{channel}/always-on")
async def channel_always_on(channel: int, body: AlwaysOnRequest, request: Request):
    return respond(await request.app.state.controller.set_channel_always_on(channel, body.enabled))


@router.post("/channels/{channel}/toggle")
async def channel_toggle(channel: int, request: Request):
    return respond(await request.app.state.controller.channel_toggle(channel))


@router.post("/channels/{channel}/fader")
async def channel_fader(channel: int, body: FaderRequest, request: Request):
    return respond(await request.app.state.controller.channel_set_fader(channel, body.value))


@router.post("/channels/{channel}/recall-fader")
async def channel_recall_fader(channel: int, body: RecallFaderRequest, request: Request):
    return respond(await request.app.state.controller.channel_recall_fader(channel, body.fader_hex))


@router.post("/groups/{group}/mute")
async def group_mute(group: str, request: Request):
    return respond(await request.app.state.controller.group_set(group, True))


@router.post("/groups/{group}/unmute")
async def group_unmute(group: str, request: Request):
    return respond(await request.app.state.controller.group_set(group, False))


@router.post("/groups/{group}/toggle")
async def group_toggle(group: str, request: Request):
    return respond(await request.app.state.controller.group_toggle(group))


@router.post("/groups/from-current-status")
async def create_group_from_current_status(body: GroupCaptureRequest, request: Request):
    """A new group of the unmuted channels; their fader values are saved as
    the group's presets."""
    state = request.app.state
    result = await state.controller.create_group_from_current_status(body.name)
    if result.get("ok") and state.group_presets is not None:
        await state.group_presets.set_faders(result["group"], result.get("fader_hex", {}))
    return respond(result)


# Panic: logged at WARNING and saved by the controller (survives a restart).

@router.post("/panic/disable")
async def panic_disable(request: Request):
    controller = request.app.state.controller
    controller.enabled = False
    return {"ok": True, "enabled": controller.enabled}


@router.post("/panic/enable")
async def panic_enable(request: Request):
    controller = request.app.state.controller
    controller.enabled = True
    return {"ok": True, "enabled": controller.enabled}
