"""Programm: the cue list. A row may set a mute group, run up to three
actions (keys, desktop profiles, files) and show a song.

Playing a row is all-or-nothing up front: every reference is checked before
anything happens, then the group is set, then the actions run in order and
stop at the first failure."""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response
from pydantic import ValidationError

from ..camera_program import CAMERAS, DEFAULT_WAIT_SECONDS, obs_scene_names
from ..actions import (
    PROGRAM_ACTION_FIELDS,
    program_conflict_error,
    program_reference_error,
    press_native,
    run_action,
    run_profile,
)
from ..models import ProgramItemRequest, ShortcutRequest
from ..native_controls import NativeControlError
from ..profile_stops import program_starts, stop_previous
from ..program_items import ProgramItemError, unlink_shortcut
from ..responses import fail, respond, unwrap
from ..lights import apply_scene
from ..songs import show_slide

router = APIRouter()

_PROGRAM_FILE_FORMAT = "mixer-mute-service-program"
_PROGRAM_FILE_VERSION = 1
_MAX_PROGRAM_FILE_ITEMS = 100
_PROGRAM_FILE_FIELDS = (
    "time", "title", "person", "group", "playback", "translation", "lyrics",
    "playback_description", "translation_description", "lyrics_description", "song", "light",
    "cam1_preset", "cam2_preset", "camera_wait_seconds", "desktop", "cam1_scene", "cam2_scene", "beamer",
)

# One program start at a time: two quick "Weiter" presses must not both start
# a row, and a row must not run twice in parallel.
_program_lock = asyncio.Lock()


async def _validate(body, request: Request):
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


@router.get("/program/export")
async def export_program(request: Request):
    items = await request.app.state.program_items.list()
    payload = {
        "format": _PROGRAM_FILE_FORMAT,
        "version": _PROGRAM_FILE_VERSION,
        "items": [
            {**{field: item.get(field) for field in _PROGRAM_FILE_FIELDS}, "hidden": item["hidden"]}
            for item in items
        ],
    }
    return Response(
        content=json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="programm.json"'},
    )


@router.post("/program/import")
async def import_program(request: Request, body: dict = Body(...)):
    if body.get("format") != _PROGRAM_FILE_FORMAT or body.get("version") != _PROGRAM_FILE_VERSION:
        return fail("unsupported program file format or version", 400)
    rows = body.get("items")
    if not isinstance(rows, list):
        return fail("items must be a list", 400)
    if len(rows) > _MAX_PROGRAM_FILE_ITEMS:
        return fail(f"at most {_MAX_PROGRAM_FILE_ITEMS} program items", 400)

    imported = []
    shortcuts = None  # read once, when a row refers to shortcuts
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return fail(f"item {index + 1} must be an object", 400)
        hidden = row.get("hidden", False)
        if type(hidden) is not bool:
            return fail(f"item {index + 1}: hidden must be true or false", 400)
        # A file from when rows referred to shortcuts: their fields are taken
        # in as the stored rows' are (app/program_items.py unlink_shortcut);
        # deleted ones are left out.
        store = _shortcut_store(request)
        if store is not None:
            if shortcuts is None:
                shortcuts = {shortcut["id"]: shortcut for shortcut in await store.list()}
            row, _status = unlink_shortcut(f"Imported row {index + 1}", row, row, shortcuts, lambda merged: merged)
        try:
            item = ProgramItemRequest.model_validate(row)
        except ValidationError:
            return fail(f"item {index + 1} has invalid fields", 400)
        invalid = await _validate(item, request)
        if invalid:
            return invalid
        imported.append({**item.model_dump(), "hidden": hidden})

    try:
        items = await request.app.state.program_items.replace_all(imported, preserve_hidden=True)
    except ProgramItemError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "items": items}


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


@router.post("/program/items/{item_id}/hidden")
async def hide_program_item(item_id: str, request: Request, body: dict = Body(...)):
    """{"hidden": true} greys the row out and "Weiter" skips it; false shows it again."""
    hidden = body.get("hidden") if isinstance(body, dict) else None
    if not isinstance(hidden, bool):
        return fail("hidden must be true or false", 400)
    item = await request.app.state.program_items.set_hidden(item_id, hidden)
    if item is None:
        return fail("unknown program item", 404)
    return {"ok": True, "item": item}


@router.post("/program/items/{item_id}/shortcut")
async def shortcut_from_program_item(item_id: str, request: Request):
    """Save what the row does as a new shortcut (a copy: editing one leaves
    the other)."""
    store = _shortcut_store(request)
    if store is None:
        return fail("shortcuts unavailable", 503)
    item = await request.app.state.program_items.get(item_id)
    if item is None:
        return fail("unknown program item", 404)
    try:
        shortcut = await store.create(item)
    except ProgramItemError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "shortcut": shortcut}


# ---- shortcuts (app/shortcuts.py) ------------------------------------------

def _shortcut_store(request: Request):
    return getattr(request.app.state, "shortcuts", None)


@router.get("/shortcuts")
async def list_shortcuts(request: Request):
    store = _shortcut_store(request)
    if store is None:
        return fail("shortcuts unavailable", 503)
    return {"ok": True, "shortcuts": await store.list()}


@router.post("/shortcuts")
async def create_shortcut(body: ShortcutRequest, request: Request):
    store = _shortcut_store(request)
    if store is None:
        return fail("shortcuts unavailable", 503)
    invalid = await _validate(body, request)
    if invalid:
        return invalid
    try:
        shortcut = await store.create(body.model_dump())
    except ProgramItemError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "shortcut": shortcut}


@router.put("/shortcuts/{shortcut_id}")
async def update_shortcut(shortcut_id: str, body: ShortcutRequest, request: Request):
    store = _shortcut_store(request)
    if store is None:
        return fail("shortcuts unavailable", 503)
    invalid = await _validate(body, request)
    if invalid:
        return invalid
    try:
        shortcut = await store.update(shortcut_id, body.model_dump())
    except ProgramItemError as exc:
        return fail(str(exc), 400)
    if shortcut is None:
        return fail("unknown shortcut", 404)
    return {"ok": True, "shortcut": shortcut}


@router.post("/shortcuts/{shortcut_id}/duplicate")
async def duplicate_shortcut(shortcut_id: str, request: Request):
    store = _shortcut_store(request)
    if store is None:
        return fail("shortcuts unavailable", 503)
    try:
        shortcut = await store.duplicate(shortcut_id)
    except ProgramItemError as exc:
        return fail(str(exc), 400)
    if shortcut is None:
        return fail("unknown shortcut", 404)
    return {"ok": True, "shortcut": shortcut}


@router.delete("/shortcuts/{shortcut_id}")
async def delete_shortcut(shortcut_id: str, request: Request):
    store = _shortcut_store(request)
    if store is None:
        return fail("shortcuts unavailable", 503)
    # Rows that loaded it keep their copy of its fields.
    if not await store.delete(shortcut_id):
        return fail("unknown shortcut", 404)
    return {"ok": True, "shortcut_id": shortcut_id, "deleted": True}


@router.post("/shortcuts/{shortcut_id}/play")
async def play_shortcut(shortcut_id: str, request: Request):
    """Run a shortcut now; the program's run order and start times stay as they are."""
    store = _shortcut_store(request)
    if store is None:
        return fail("shortcuts unavailable", 503)
    async with _program_lock:
        shortcut = await store.get(shortcut_id)
        if shortcut is None:
            return fail("unknown shortcut", 404)
        broken = await program_reference_error(request, shortcut) or await program_conflict_error(request, shortcut)
        if broken:
            return fail(broken, 409, shortcut_id=shortcut_id)
        return await _run_steps(request, shortcut, {"shortcut_id": shortcut_id, "title": shortcut["title"]})


@router.get("/program/obs-scenes")
async def program_obs_scenes():
    """The scenes OBS has, in the order of its scene list."""
    try:
        scenes = await asyncio.to_thread(obs_scene_names)
    except NativeControlError as exc:
        return fail(f"OBS not reachable: {exc}", 503)
    return {"ok": True, "scenes": scenes}


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
    # deleted key halfway through; the mute conflict, too (a key may switch
    # mutes since the row was saved).
    broken = await program_reference_error(request, item) or await program_conflict_error(request, item)
    if broken:
        return fail(broken, 409, program_item_id=item_id)
    summary = {"program_item_id": item_id, "title": item["title"]}

    async def started() -> dict:
        # Marked once its group is set: a refused group leaves "Weiter" on this row.
        # Then what the rows before showed or played stops, in the background
        # (app/profile_stops.py): the new row's steps do not wait for it.
        stop_previous(request.app.state, item)
        played = await program_store.mark_played(item_id)
        return {"delay_seconds": played["last_delay_seconds"]}

    # What the row starts counts as the program's (stopped on the next row).
    with program_starts(item_id):
        return await _run_steps(request, item, summary, started)


async def _run_steps(request: Request, item: dict, summary: dict, started=None):
    """Run what a program row or shortcut does: group, light, beamer hide,
    files, desktop profiles, song, beamer unhide, cameras. Stops at the first
    failure. Returns the answer (a dict when everything ran)."""
    group_result = None
    if item.get("group"):
        group_result = await request.app.state.controller.group_set(item["group"], False)
        if not group_result.get("ok"):
            return respond({
                "ok": False,
                "reason": group_result.get("reason", "mute group could not be activated"),
                "details": group_result.get("details"),
                **summary,
                "group_result": group_result,
            })
    if started is not None:
        summary.update(await started())
    summary["group_result"] = group_result

    light_result = None
    if item.get("light"):
        # The light first: it fades while the keys and the song run.
        light_result, status_code = await apply_scene(request, item["light"])
        if status_code != 200 or light_result.get("ok") is False:
            reason = f"light scene could not be applied: {light_result.get('reason')}"
            return fail(reason, status_code if status_code != 200 else 502, **summary, light_result=light_result)

    linked_actions = []
    # Hide before anything new is shown; unhide only once it is (below).
    if item.get("beamer") == "hide":
        failed = await _set_beamer(request, True, summary, linked_actions)
        if failed:
            return failed
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

    for profile_id in item.get("desktop") or []:
        result, status_code = await run_profile(request, profile_id)
        linked_actions.append({"field": "desktop", "profile": profile_id, **result})
        if status_code != 200 or result.get("ok") is False:
            reason = "desktop profile failed" + (" after group activation" if group_result else "")
            return fail(reason, status_code if status_code != 200 else 502,
                        **summary, linked_actions=linked_actions)

    song_result = None
    if item.get("song"):
        song_result, status_code = unwrap(await show_slide(request, item["song"], 0))
        if status_code != 200 or song_result.get("ok") is False:
            reason = f"song could not be shown: {song_result.get('reason', 'HTTP ' + str(status_code))}"
            return fail(reason, status_code if status_code != 200 else 502,
                        **summary, linked_actions=linked_actions)

    if item.get("beamer") == "unhide":
        failed = await _set_beamer(request, False, summary, linked_actions)
        if failed:
            return failed

    # Last: the camera presets. Only the camera that is off air moves now;
    # switching to it (or to its OBS scene) and the other camera's preset
    # follow in the background.
    camera_result = None
    presets = {cam: item.get(f"{cam}_preset") for cam in CAMERAS}
    if any(presets.values()):
        camera_program = getattr(request.app.state, "camera_program", None)
        if camera_program is None:
            return fail("camera presets unavailable", 503, **summary, linked_actions=linked_actions)
        camera_result = await camera_program.run(
            presets, item.get("camera_wait_seconds") or DEFAULT_WAIT_SECONDS, item["title"],
            scenes={cam: item.get(f"{cam}_scene") for cam in CAMERAS})
        if not camera_result.get("ok"):
            return fail(camera_result.get("reason", "camera presets failed"), 502,
                        **summary, linked_actions=linked_actions, camera=camera_result)

    return {"ok": True, "song": song_result, "light": light_result, **summary,
            "linked_actions": linked_actions, "camera": camera_result}


async def _set_beamer(request: Request, hidden: bool, summary: dict, linked_actions: list):
    """The row's beamer state, like the "Beamer hide/unhide" keys; a failure answer or None."""
    button_id = "beamer-hide" if hidden else "beamer-unhide"
    result, status_code = await press_native(request, button_id, {"type": "beamer-set", "hidden": hidden})
    linked_actions.append({"field": "beamer", **result})
    if status_code != 200 or result.get("ok") is False:
        return fail(f"beamer could not be set: {result.get('reason', 'HTTP ' + str(status_code))}",
                    status_code if status_code != 200 else 502, **summary, linked_actions=linked_actions)
    return None
