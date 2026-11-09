"""Programm: the cue list. A row may set a mute group, run up to three
actions (keys, desktop profiles, files) and show a song.

Playing a row is all-or-nothing up front: every reference is checked before
anything happens, then the group is set, then the actions run in order and
stop at the first failure."""

from __future__ import annotations

import asyncio
from typing import Optional

from fastapi import APIRouter, Body, Request

from ..camera_program import CAMERAS, DEFAULT_WAIT_SECONDS
from ..actions import (
    PROGRAM_ACTION_FIELDS,
    program_conflict_error,
    program_reference_error,
    run_action,
)
from ..models import ProgramItemRequest
from ..program_items import ProgramItemError
from ..responses import fail, respond, unwrap
from ..songs import show_slide

router = APIRouter()

# One program start at a time: two quick "Weiter" presses must not both start
# a row, and a row must not run twice in parallel.
_program_lock = asyncio.Lock()


async def _validate(body: ProgramItemRequest, request: Request):
    item = body.model_dump()
    reason = await program_reference_error(request, item)
    if reason is not None:
        return fail(reason, 404 if reason == "unknown group" else 400)
    reason = await program_conflict_error(request, item)
    if reason is not None:
        return fail(reason, 400)
    return None


@router.get("/program/items")
async def list_program_items(request: Request):
    return await request.app.state.program_items.list()


@router.post("/program/items")
async def create_program_item(body: ProgramItemRequest, request: Request):
    invalid = await _validate(body, request)
    if invalid:
        return invalid
    try:
        item = await request.app.state.program_items.create(body.model_dump())
    except ProgramItemError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "item": item}


@router.put("/program/items/{item_id}")
async def update_program_item(item_id: str, body: ProgramItemRequest, request: Request):
    invalid = await _validate(body, request)
    if invalid:
        return invalid
    try:
        item = await request.app.state.program_items.update(item_id, body.model_dump())
    except ProgramItemError as exc:
        return fail(str(exc), 400)
    if item is None:
        return fail("unknown program item", 404)
    return {"ok": True, "item": item}


@router.delete("/program/items/{item_id}")
async def delete_program_item(item_id: str, request: Request):
    if not await request.app.state.program_items.delete(item_id):
        return fail("unknown program item", 404)
    return {"ok": True, "item_id": item_id, "deleted": True}


@router.get("/program/status")
async def program_status(request: Request):
    """The row that ran last today and the one "Weiter" would start, plus
    the last outcome of a row's camera presets (switching runs in the
    background, so the UI learns here whether it happened)."""
    status = await request.app.state.program_items.status()
    camera_program = getattr(request.app.state, "camera_program", None)
    return {**status, "camera": camera_program.status if camera_program is not None else None}


@router.post("/program/items/{item_id}/duplicate")
async def duplicate_program_item(item_id: str, request: Request):
    item = await request.app.state.program_items.duplicate(item_id)
    if item is None:
        return fail("unknown program item", 404)
    return {"ok": True, "item": item}


@router.post("/program/reset")
async def reset_program(request: Request):
    count = await request.app.state.program_items.reset_played()
    return {"ok": True, "reset": count}


@router.post("/program/next")
async def play_next_program_item(request: Request, body: Optional[dict] = Body(None)):
    """Start the next row. With {"expected_next_id": id} only if that row is
    still the next one: a double tap, or a second device, then does not skip
    a row."""
    expected = (body or {}).get("expected_next_id")
    async with _program_lock:
        status = await request.app.state.program_items.status()
        if status["next_id"] is None:
            return fail("program finished", 409)
        if expected is not None and expected != status["next_id"]:
            return fail("program moved on", 409, next_id=status["next_id"])
        return await _play(status["next_id"], request)


@router.post("/program/items/{item_id}/play")
async def play_program_item(item_id: str, request: Request):
    async with _program_lock:
        return await _play(item_id, request)


async def _play(item_id: str, request: Request):
    program_store = request.app.state.program_items
    item = await program_store.get(item_id)
    if item is None:
        return fail("unknown program item", 404)
    # Refuse before anything happens instead of muting and then failing on a
    # deleted key halfway through.
    broken = await program_reference_error(request, item)
    if broken:
        return fail(broken, 409, program_item_id=item_id)
    group_result = None
    if item.get("group"):
        group_result = await request.app.state.controller.group_set(item["group"], False)
        if not group_result.get("ok"):
            # Not marked as started: "Weiter" still offers this row.
            return respond({
                "ok": False,
                "reason": group_result.get("reason", "mute group could not be activated"),
                "details": group_result.get("details"),
                "program_item_id": item_id,
                "group_result": group_result,
            })
    played = await program_store.mark_played(item_id)
    summary = {
        "program_item_id": item_id,
        "title": item["title"],
        "delay_seconds": played["last_delay_seconds"],
        "group_result": group_result,
    }

    linked_actions = []
    for field in PROGRAM_ACTION_FIELDS:
        value = item.get(field)
        if not value:
            continue
        result, status_code = await run_action(request, value)
        linked_actions.append({"field": field, **result})
        if status_code != 200 or result.get("ok") is False:
            reason = f"{field} action failed" + (" after group activation" if group_result else "")
            return fail(reason, status_code if status_code != 200 else 502,
                        **summary, linked_actions=linked_actions)

    song_result = None
    if item.get("song"):
        song_result, status_code = unwrap(await show_slide(request, item["song"], 0))
        if status_code != 200 or song_result.get("ok") is False:
            reason = f"song could not be shown: {song_result.get('reason', 'HTTP ' + str(status_code))}"
            return fail(reason, status_code if status_code != 200 else 502,
                        **summary, linked_actions=linked_actions)

    # Last: the camera presets. Only the camera that is off air moves now;
    # switching to it and the other camera's preset follow in the background.
    camera_result = None
    presets = {cam: item.get(f"{cam}_preset") for cam in CAMERAS}
    if any(presets.values()):
        camera_program = getattr(request.app.state, "camera_program", None)
        if camera_program is None:
            return fail("camera presets unavailable", 503, **summary, linked_actions=linked_actions)
        camera_result = await camera_program.run(
            presets, item.get("camera_wait_seconds") or DEFAULT_WAIT_SECONDS, item["title"])
        if not camera_result.get("ok"):
            return fail(camera_result.get("reason", "camera presets failed"), 502,
                        **summary, linked_actions=linked_actions, camera=camera_result)

    return {"ok": True, "song": song_result, **summary, "linked_actions": linked_actions,
            "camera": camera_result}
