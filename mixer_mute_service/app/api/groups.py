"""Mute group management: create, change, rename, delete, and the saved
channel settings (gain, HPF, EQ, gate, compressor) per group. Muting a group
is in api/mixer.py."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from ..channel_settings import SettingsError
from ..groups import GroupError
from ..models import GroupCreate, GroupRename, GroupUpdate
from ..responses import fail

router = APIRouter()
log = logging.getLogger("mixer.api.groups")


def _group_error(group: str, exc: GroupError):
    status = 404 if str(exc).startswith("unknown group") else 400
    return fail(str(exc), status, group=group)


@router.get("/groups")
async def list_groups(request: Request):
    return await request.app.state.groups.list()


@router.post("/groups")
async def create_group(body: GroupCreate, request: Request):
    try:
        channels = await request.app.state.groups.create(body.name, body.channels)
    except GroupError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "group": body.name.strip(), "channels": channels}


@router.get("/groups/{group}")
async def get_group(group: str, request: Request):
    channels = await request.app.state.groups.get(group)
    if channels is None:
        return fail("unknown group", 404, group=group)
    return {"group": group, "channels": channels}


@router.put("/groups/{group}")
@router.post("/groups/{group}/update")
async def update_group(group: str, body: GroupUpdate, request: Request):
    try:
        channels = await request.app.state.groups.update(group, body.channels)
    except GroupError as exc:
        return _group_error(group, exc)
    return {"ok": True, "group": group, "channels": channels}


@router.delete("/groups/{group}")
@router.post("/groups/{group}/delete")
async def delete_group(group: str, request: Request):
    try:
        await request.app.state.groups.delete(group)
    except GroupError as exc:
        return fail(str(exc), 404, group=group)
    await request.app.state.channel_settings.delete_group(group)
    return {"ok": True, "group": group, "deleted": True}


@router.post("/groups/{group}/rename")
async def rename_group(group: str, body: GroupRename, request: Request):
    """Rename a group and every reference to it (Programm rows, Steuerung keys,
    Overall macros), so nothing that used the old name breaks."""
    state = request.app.state
    try:
        channels = await state.groups.rename(group, body.name)
    except GroupError as exc:
        return _group_error(group, exc)
    new_name = body.name.strip()
    await state.channel_settings.rename_group(group, new_name)
    program_rows = await state.program_items.rename_group(group, new_name)
    keys = await state.companion_button_store.rename_group(group, new_name)
    log.info("Group %r renamed to %r (%d program rows, %d keys)", group, new_name, program_rows, keys)
    return {
        "ok": True, "group": new_name, "old_name": group, "channels": channels,
        "program_items": program_rows, "buttons": keys,
    }


def _label_channels(request: Request, snapshot: dict) -> None:
    cfg = request.app.state.cfg
    for channel, settings in snapshot["channels"].items():
        settings["name"] = cfg.channel_label(int(channel))


@router.get("/groups/{group}/settings-snapshot")
async def group_settings_snapshot(group: str, request: Request):
    """The saved gain/HPF/EQ/gate/compressor settings of a group's channels."""
    snapshot = await request.app.state.channel_settings.get(group)
    if snapshot is None:
        return fail("no settings saved", 404, group=group)
    _label_channels(request, snapshot)
    return {"ok": True, **snapshot}


@router.post("/groups/{group}/settings-snapshot")
async def capture_group_settings(group: str, request: Request):
    """Save the current processing settings of a group's input channels.
    Read from the mixer's state dump; nothing is sent to the mixer."""
    state = request.app.state
    channels = await state.groups.get(group)
    if channels is None:
        return fail("unknown group", 404, group=group)
    try:
        snapshot = await state.channel_settings.capture(
            group, channels, state.settings_cache,
            state.client.session_epoch, state.cfg.mixer.max_channels,
        )
    except SettingsError as exc:
        return fail(str(exc), 409, group=group, details=exc.details)
    log.info("Saved channel settings of group %r (%d channels)", group, len(snapshot["channels"]))
    _label_channels(request, snapshot)
    return {"ok": True, **snapshot}
