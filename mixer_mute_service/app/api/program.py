"""Programm: the cue list. A row may set a mute group, run up to three
actions (keys, desktop profiles, files) and show a song.

Playing a row is best effort per area (_run_steps): the mixer, the light,
the beamer, each file / key field, the desktop profiles, the song and the
cameras each run or fail on their own, so a light that does not answer or
a mixer that is not connected costs only that part. The mixer's own safety
stays absolute: a blocked group sends nothing to the mixer at all."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response
from pydantic import ValidationError

from ..camera_program import CAMERAS, DEFAULT_WAIT_SECONDS, obs_scene_names
from ..actions import (
    DESKTOP_PREFIX,
    PROGRAM_ACTION_FIELDS,
    program_area_problems,
    program_conflict_error,
    program_reference_error,
    press_native,
    run_action,
    run_profile,
)
from ..file_actions import is_file_action, parse_file_action
from ..models import ProgramItemRequest, ShortcutRequest
from ..program_file import Names, annotate, resolve
from ..native_controls import NativeControlError
from ..profile_stops import program_starts, stop_previous
from ..program_items import ProgramItemError, unlink_shortcut
from ..responses import fail, respond, unwrap
from ..lights import apply_scene
from ..songs import show_slide

router = APIRouter()
log = logging.getLogger("mixer.program")

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
    names = await Names.load(request)
    warnings: list = []  # references that cannot be written as a (unique) name
    payload = {
        "format": _PROGRAM_FILE_FORMAT,
        "version": _PROGRAM_FILE_VERSION,
        # Opaque IDs are written as names, readable ones stay (app/program_file.py).
        "items": [
            await annotate({**{field: item.get(field) for field in _PROGRAM_FILE_FIELDS}, "hidden": item["hidden"]},
                           names, warnings)
            for item in items
        ],
    }
    if warnings:
        payload["warnings"] = warnings
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
    warnings = []  # references that exist nowhere here (by ID or name): dropped
    names = await Names.load(request)
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
        if not isinstance(row.get("desktop", []), list):
            return fail(f"item {index + 1} has invalid fields", 400)
        row, dropped = await resolve(row, names)
        warnings += [{"item": index + 1, "title": str(row.get("title") or ""), **entry} for entry in dropped]
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
    return {"ok": True, "items": items, "warnings": warnings}


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
        # Best effort per area, like a program row.
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
    # Nothing is refused up front: a deleted key, group or light scene only
    # skips its own area (_run_steps), so the row is played all the same.
    summary = {"program_item_id": item_id, "title": item["title"]}

    async def started() -> dict:
        # Marked as played once the mixer part is done, whatever its outcome:
        # the program moves on even with the mixer offline. Then what the rows
        # before showed or played stops, in the background
        # (app/profile_stops.py): the new row's steps do not wait for it.
        stop_previous(request.app.state, item)
        played = await program_store.mark_played(item_id)
        return {"delay_seconds": played["last_delay_seconds"]}

    # What the row starts counts as the program's (stopped on the next row).
    with program_starts(item_id):
        return await _run_steps(request, item, summary, started)


async def _run_steps(request: Request, item: dict, summary: dict, started=None):
    """Run what a program row or shortcut does, best effort per area.

    The mixer first (the mutes are set before anything new shows or plays);
    its safety layer blocks the group as a whole (unknown values, no or a
    dead connection, panic): then nothing is sent to the mixer, and the rest
    runs all the same. Then, at once and side by side: the light, the
    cameras, and the content (beamer hide, the file / key fields, the
    desktop profiles in their order, the song, beamer unhide). Content steps
    on the same PC keep their order; other PCs do not wait for a PC that
    does not answer. A problem found before (program_area_problems: a key,
    profile, song or light scene that is gone) skips only its area.

    Returns {"ok": all areas ran, "partial": some did, "areas": {area: {"ok",
    "reason"}}, "reason": the failed areas, ...the earlier fields}."""
    state = request.app.state
    problems = await program_area_problems(request, item)
    areas: dict = {}
    linked_actions: list = []
    results: dict = {"song": None, "light": None, "camera": None}

    def done(area: str, ok: bool, reason: Optional[str] = None, **extra) -> None:
        areas[area] = {"ok": ok, **({"reason": reason} if reason else {}), **extra}

    async def guarded(area: str, step) -> None:
        """One area's step; whatever goes wrong there stays there."""
        if area in problems and area != "desktop":
            done(area, False, problems[area], skipped=True)
            return
        try:
            await step()
        except Exception as exc:  # noqa: BLE001 - one area never stops the others
            log.exception("Program step %s failed", area)
            done(area, False, f"internal error: {exc}")

    # -- the mixer ------------------------------------------------------------
    group_result = None
    if item.get("group"):
        async def mixer():
            nonlocal group_result
            group_result = await state.controller.group_set(item["group"], False)
            if group_result.get("ok"):
                done("mixer", True)
            else:
                done("mixer", False, group_result.get("reason", "mute group could not be activated"),
                     details=group_result.get("details"))
        await guarded("mixer", mixer)
    if started is not None:
        summary.update(await started())
    summary["group_result"] = group_result

    # -- the light --------------------------------------------------------------
    async def light():
        result, status_code = await apply_scene(request, item["light"])
        results["light"] = result
        ok = status_code == 200 and result.get("ok") is not False
        done("light", ok, None if ok else str(result.get("reason") or f"HTTP {status_code}"))

    # -- the cameras (only the camera that is off air moves; the switch follows
    # in the background) -----------------------------------------------------------
    presets = {cam: item.get(f"{cam}_preset") for cam in CAMERAS}

    async def cameras():
        camera_program = getattr(state, "camera_program", None)
        if camera_program is None:
            done("cameras", False, "camera presets unavailable")
            return
        result = await camera_program.run(
            presets, item.get("camera_wait_seconds") or DEFAULT_WAIT_SECONDS, item["title"],
            scenes={cam: item.get(f"{cam}_scene") for cam in CAMERAS})
        results["camera"] = result
        done("cameras", bool(result.get("ok")), None if result.get("ok") else result.get("reason", "camera presets failed"))

    parallel = []
    if item.get("light"):
        parallel.append(guarded("light", light))
    if any(presets.values()):
        parallel.append(guarded("cameras", cameras))
    parallel.append(_run_content(request, item, problems, guarded, done, linked_actions, results))
    await asyncio.gather(*parallel)

    failed = {area: entry for area, entry in areas.items() if not entry["ok"]}
    ran = any(entry["ok"] or entry.get("partial") for entry in areas.values())
    answer = {"ok": not failed, "partial": bool(failed) and ran, "areas": areas,
              "song": results["song"], "light": results["light"], **summary,
              "linked_actions": linked_actions, "camera": results["camera"]}
    if failed:
        answer["reason"] = "; ".join(f"{area}: {entry.get('reason', 'failed')}" for area, entry in failed.items())
        log.warning("Program step(s) failed for %s: %s", item.get("title"), answer["reason"])
    return answer


def _step_pcs(request: Request, value: str) -> set:
    """The PCs a file / key field's step goes to ("keys": no PC)."""
    if is_file_action(value):
        try:
            return {parse_file_action(value)[0]}
        except ValueError:
            return {"keys"}
    if value.startswith(DESKTOP_PREFIX):
        profile = request.app.state.cfg.desktop_display.profiles.get(value.removeprefix(DESKTOP_PREFIX))
        return {profile.target_ip} if profile is not None else {"keys"}
    return {"keys"}


async def _run_content(request, item, problems, guarded, done, linked_actions, results) -> None:
    """Beamer hide, the file / key fields, the desktop profiles, the song,
    beamer unhide. Each step is its own; steps on the same PC run in this
    order (a song after the image on its screen), others side by side."""
    state = request.app.state
    profiles = state.cfg.desktop_display.profiles

    async def beamer(hidden: bool):
        button_id = "beamer-hide" if hidden else "beamer-unhide"
        result, status_code = await press_native(request, button_id, {"type": "beamer-set", "hidden": hidden})
        linked_actions.append({"field": "beamer", **result})
        ok = status_code == 200 and result.get("ok") is not False
        done("beamer", ok, None if ok else f"{'hide' if hidden else 'unhide'}: {result.get('reason', 'HTTP ' + str(status_code))}")

    def field_step(field: str, value: str):
        async def step():
            result, status_code = await run_action(request, value)
            linked_actions.append({"field": field, **result})
            ok = status_code == 200 and result.get("ok") is not False
            done(field, ok, None if ok else str(result.get("reason") or f"HTTP {status_code}"))
        return step

    desktop_failures: list = []
    desktop_ran: list = []

    def profile_step(profile_id: str):
        async def step():
            result, status_code = await run_profile(request, profile_id)
            linked_actions.append({"field": "desktop", "profile": profile_id, **result})
            if status_code != 200 or result.get("ok") is False:
                title = profiles[profile_id].title if profile_id in profiles else profile_id
                desktop_failures.append(f"{title}: {result.get('reason') or 'HTTP ' + str(status_code)}")
            else:
                desktop_ran.append(profile_id)
        return step

    async def song():
        result, status_code = unwrap(await show_slide(request, item["song"], 0))
        results["song"] = result
        ok = status_code == 200 and result.get("ok") is not False
        done("song", ok, None if ok else str(result.get("reason") or f"HTTP {status_code}"))

    # (area, the PCs it goes to, the step), in the row's order.
    steps = []
    for field in PROGRAM_ACTION_FIELDS:
        value = item.get(field)
        if value:
            steps.append((field, _step_pcs(request, value), field_step(field, value)))
    desktop = [p for p in item.get("desktop") or [] if p in profiles]
    for profile_id in desktop:
        steps.append(("desktop", {profiles[profile_id].target_ip}, profile_step(profile_id)))
    if item.get("song"):
        song_service = getattr(state, "songs", None)
        settings = getattr(song_service, "settings", None)
        pcs = {target.ip for target in settings.targets} if settings is not None else {"song"}
        steps.append(("song", pcs, song))

    if item.get("beamer") == "hide":
        await guarded("beamer", lambda: beamer(True))  # before anything new is shown
    last: dict = {}  # PC -> the latest step on it
    tasks = []
    for area, pcs, step in steps:
        before = [last[pc] for pc in pcs if pc in last]
        task = asyncio.create_task(_after(before, guarded(area, step)))
        for pc in pcs:
            last[pc] = task
        tasks.append(task)
    await asyncio.gather(*tasks)
    if desktop or "desktop" in problems:
        failures = desktop_failures + ([problems["desktop"]] if "desktop" in problems else [])
        # Some profiles ran, others not: "partial".
        done("desktop", not failures, "; ".join(failures) or None,
             **({"partial": True} if failures and desktop_ran else {}))
    if item.get("beamer") == "unhide":
        # Only once the content is there (or did not come).
        await guarded("beamer", lambda: beamer(False))


async def _after(before: list, step) -> None:
    """Run step once the steps before it on the same PC are done (however)."""
    if before:
        await asyncio.gather(*before, return_exceptions=True)
    await step
