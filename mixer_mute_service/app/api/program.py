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
from pathlib import PureWindowsPath
from typing import Optional

from fastapi import APIRouter, Body, Request
from fastapi.responses import Response
from pydantic import ValidationError

from ..camera_program import CAMERAS, DEFAULT_WAIT_SECONDS, obs_scene_names
from ..actions import (
    program_area_problems,
    program_conflict_error,
    program_reference_error,
    press_button,
    press_native,
    pressable_button,
    run_profile,
    run_via,
    switches_mutes,
)
from ..file_actions import is_file_action, run_file_action
from ..row_files import (KEY_PREFIX, STEP_FREEZE, STEP_SONG, STEP_UNFREEZE, fold_legacy, is_via, path_kind,
                         step_profile, streaming_pc_ips)
from ..models import ProgramItemRequest, ShortcutRequest
from ..program_file import Names, annotate, resolve
from ..native_controls import NativeControlError
from ..profile_stops import program_starts, running_steps, stop_previous, stop_started
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


async def _folded(request: Request, item: dict) -> dict:
    """A row (or shortcut) as it came in, with any old Playback /
    Übersetzung / Liedtext moved into its Desktop steps (app/row_files.py)."""
    fold_legacy(item, request.app.state.cfg.desktop_display.profiles, await streaming_pc_ips(request.app.state))
    return item


async def _validate(body, request: Request):
    item = body if isinstance(body, dict) else body.model_dump()
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
        data = await _folded(request, item.model_dump())
        invalid = await _validate(data, request)
        if invalid:
            return invalid
        imported.append({**data, "hidden": hidden})

    try:
        items = await request.app.state.program_items.replace_all(imported, preserve_hidden=True)
    except ProgramItemError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "items": items, "warnings": warnings}


@router.post("/program/items")
async def create_program_item(body: ProgramItemRequest, request: Request):
    data = await _folded(request, body.model_dump())
    invalid = await _validate(data, request)
    if invalid:
        return invalid
    try:
        item = await request.app.state.program_items.create(data)
    except ProgramItemError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "item": item}


@router.put("/program/items/{item_id}")
async def update_program_item(item_id: str, body: ProgramItemRequest, request: Request):
    data = await _folded(request, body.model_dump())
    invalid = await _validate(data, request)
    if invalid:
        return invalid
    try:
        item = await request.app.state.program_items.update(item_id, data)
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
    data = await _folded(request, body.model_dump())
    invalid = await _validate(data, request)
    if invalid:
        return invalid
    try:
        shortcut = await store.create(data)
    except ProgramItemError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "shortcut": shortcut}


@router.put("/shortcuts/{shortcut_id}")
async def update_shortcut(shortcut_id: str, body: ShortcutRequest, request: Request):
    store = _shortcut_store(request)
    if store is None:
        return fail("shortcuts unavailable", 503)
    data = await _folded(request, body.model_dump())
    invalid = await _validate(data, request)
    if invalid:
        return invalid
    try:
        shortcut = await store.update(shortcut_id, data)
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
    shown = getattr(request.app.state, "shown_profiles", None)
    current = await request.app.state.program_items.get(status["current_id"]) if status["current_id"] else None
    return {**status, "camera": camera_program.status if camera_program is not None else None,
            # What program rows started and may still show or play ("Stopp").
            "started": len(shown.shown()) if shown is not None else 0,
            # The running row's steps among them (a stop key on their chips).
            "running": running_steps(request.app.state, current)}


@router.post("/program/stop")
async def stop_program(request: Request, body: Optional[dict] = Body(None)):
    """Stopp: everything program rows started (music fades out, windows
    close, images go); the program's place stays. What was started by hand
    keeps running (app/profile_stops.py). With {"step": start id} (a step's
    stop key, ids from GET /program/status "running") only that one."""
    step = (body or {}).get("step")
    if step is not None:
        if not isinstance(step, str) or not step or len(step) > 200:
            return fail("step must be a start id", 400)
        answer, status = await stop_started(request.app.state, step)
        return answer if status == 200 else fail(answer["reason"], status, step=step)
    async with _program_lock:  # not while a row is starting
        shown = getattr(request.app.state, "shown_profiles", None)
        count = len(shown.shown()) if shown is not None else 0
        task = stop_previous(request.app.state, {})
    if task is not None:
        await task
    return {"ok": True, "stopped": count}


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


# A step that shows something (an image, a file, a program, the song) gets
# this long to be on screen before the next step that freezes or unfreezes
# the beamer or shows something on the same PC, unless the agent said it is
# there ("shown": true, from agents that draw documents themselves).
# Pictures on other PCs do not wait for it.
SETTLE_SECONDS = 2.0
_PSEUDO_TITLES = {STEP_FREEZE: "Beamer einfrieren", STEP_UNFREEZE: "Beamer auftauen", STEP_SONG: "Liedtext"}


def _step_title(request: Request, step: str) -> str:
    """The step as the operator knows it (for the toast and the log)."""
    profiles = request.app.state.cfg.desktop_display.profiles
    if step in _PSEUDO_TITLES:
        return _PSEUDO_TITLES[step]
    if is_file_action(step) or is_via(step):
        name = PureWindowsPath(step.split("|", 1)[-1]).name
        profile = profiles.get(step_profile(step) or "")
        return f"{name} über {profile.title}" if profile is not None else name
    if step.startswith(KEY_PREFIX):
        return step.removeprefix(KEY_PREFIX)
    profile = profiles.get(step)
    return profile.title if profile is not None else step


def _step_pcs(request: Request, item: dict, step: str) -> set:
    """The PCs a step shows something on."""
    profiles = request.app.state.cfg.desktop_display.profiles
    if step == STEP_SONG:
        settings = getattr(getattr(request.app.state, "songs", None), "settings", None)
        return {target.ip for target in settings.targets} if settings is not None else set()
    if is_file_action(step):
        return {step[len("file:"):].split("|", 1)[0]}
    profile = profiles.get(step_profile(step) or "")
    return {profile.target_ip} if profile is not None else set()


def _step_shows(request: Request, step: str) -> bool:
    """Whether a step puts a picture on a screen (not audio, not a key)."""
    profiles = request.app.state.cfg.desktop_display.profiles
    if step == STEP_SONG:
        return True
    if is_via(step) or is_file_action(step):
        return path_kind(step.split("|", 1)[-1]) != "audio"
    profile = profiles.get(step)
    return profile is not None and profile.kind != "audio"


async def _run_content(request, item, problems, guarded, done, linked_actions, results) -> None:
    """Beamer hide / unhide as the row says, then the Desktop steps in their
    order (app/row_files.py), then the song if no step has it.

    Order: steps on the same PC one after the other, a freeze / unfreeze
    after everything before it, everything after it after it; steps on other
    PCs side by side (a PC that does not answer holds up only its own). A
    step that shows something is given SETTLE_SECONDS (unless the agent
    answered "shown": true) before a freeze / unfreeze or the next picture on
    its PC. A step that fails is logged and reported; the next steps run all
    the same. Only a freeze right after a failed step is skipped (it would
    freeze the wrong picture), and said so."""
    state = request.app.state

    async def beamer(hidden: bool):
        button_id = "beamer-hide" if hidden else "beamer-unhide"
        result, status_code = await press_native(request, button_id, {"type": "beamer-set", "hidden": hidden})
        linked_actions.append({"field": "beamer", **result})
        ok = status_code == 200 and result.get("ok") is not False
        done("beamer", ok, None if ok else f"{'hide' if hidden else 'unhide'}: {result.get('reason', 'HTTP ' + str(status_code))}")

    async def song():
        result, status_code = unwrap(await show_slide(request, item["song"], 0))
        results["song"] = result
        ok = status_code == 200 and result.get("ok") is not False
        done("song", ok, None if ok else str(result.get("reason") or f"HTTP {status_code}"))

    async def run_step(step: str):
        if is_via(step):
            return await run_via(request, step)
        if is_file_action(step):
            return await run_file_action(request, step)
        if step.startswith(KEY_PREFIX):
            button = await pressable_button(request, step.removeprefix(KEY_PREFIX))
            if button is None:
                return {"ok": False, "reason": "unknown button"}, 404
            if item.get("group") and switches_mutes(button):
                return {"ok": False, "reason": "this key switches mutes; the row's mute group sets them itself"}, 409
            return await press_button(request, step.removeprefix(KEY_PREFIX))
        return await run_profile(request, step)

    if item.get("beamer") in ("hide", "unhide"):
        await guarded("beamer", lambda: beamer(item["beamer"] == "hide"))  # before anything new is shown

    steps = list(item.get("desktop") or [])
    if item.get("song") and STEP_SONG not in steps:
        steps.append(STEP_SONG)  # a row from before the song was a step: last, as it ran
    failures: list = []
    ran = [0]
    outcome: dict = {}         # step index -> ok
    unconfirmed: set = set()   # PCs whose last picture may not be there yet

    async def settle(pcs=None):
        """Wait for an unconfirmed picture: before a freeze (pcs None) any,
        before another picture only one on the same PC."""
        if unconfirmed and (pcs is None or unconfirmed & pcs):
            await asyncio.sleep(SETTLE_SECONDS)
            unconfirmed.difference_update(unconfirmed if pcs is None else pcs)

    async def run_one(index: int, step: str) -> None:
        title = _step_title(request, step)
        if step == STEP_SONG:
            if not item.get("song"):
                outcome[index] = True  # the step stays for a song chosen later (or a template's slot)
                return
            pcs = _step_pcs(request, item, step)
            await settle(pcs)
            await guarded("song", song)
            outcome[index] = areas_ok(results["song"])
            if outcome[index]:
                unconfirmed.update(pcs)
            return
        if step in (STEP_FREEZE, STEP_UNFREEZE):
            freeze = step == STEP_FREEZE
            if freeze and index > 0 and not outcome.get(index - 1, True):
                log.warning("Program %s: %s skipped, the step before did not work or showed nothing",
                            item.get("title"), title)
                failures.append(f"{title}: übersprungen, der Schritt davor hat nicht geklappt")
                outcome[index] = False
                return
            native = getattr(state, "native_controls", None)
            if native is None or await native.get_beamer_frozen() is None:
                # Unknown: nothing would be sent anyway; no settle wait for it.
                log.warning("Program %s: %s not sent, the freeze state is unknown", item.get("title"), title)
                failures.append(f"{title}: Beamer freeze state is unknown (set it in the Beamer section)")
                outcome[index] = False
                return
            await settle()
            try:
                result, status_code = await press_native(request, "beamer-freeze" if freeze else "beamer-unfreeze",
                                                         {"type": "beamer-freeze-set", "frozen": freeze})
            except Exception as exc:  # noqa: BLE001 - the next steps run whatever the beamer does
                result, status_code = {"ok": False, "reason": f"beamer control unavailable: {exc}"}, 503
        else:
            shows = _step_shows(request, step)
            if shows:
                await settle(_step_pcs(request, item, step))
            try:
                result, status_code = await run_step(step)
            except Exception as exc:  # noqa: BLE001 - one step never stops the others
                log.exception("Program step %s failed", step)
                result, status_code = {"ok": False, "reason": f"internal error: {exc}"}, 500
        linked_actions.append({"field": "desktop", "step": step,
                               **({"profile": step_profile(step)} if step_profile(step) else {}), **result})
        ok = status_code == 200 and result.get("ok") is not False
        # A document the agent did not get on screen in time ("shown": false)
        # counts as not shown for a freeze right after it.
        outcome[index] = ok and result.get("shown") is not False
        if ok:
            ran[0] += 1
            if step not in (STEP_FREEZE, STEP_UNFREEZE) and _step_shows(request, step):
                pcs = _step_pcs(request, item, step)
                if result.get("shown") is None:
                    unconfirmed.update(pcs)  # no word from the agent: give it the settle time
                else:
                    unconfirmed.difference_update(pcs)  # drawn, or reported as not drawn: no wait
        else:
            failures.append(f"{title}: {result.get('reason') or 'HTTP ' + str(status_code)}")
            log.warning("Program %s: step %s failed: %s", item.get("title"), title, result.get("reason"))

    # In order where it matters: a step waits for the step before it on the
    # same PC(s) and for the last freeze / unfreeze; a freeze / unfreeze waits
    # for every step before it. Steps on other PCs do not wait for a PC that
    # does not answer.
    tasks: list = []
    last_on: dict = {}   # PC -> its latest step's task
    barrier = None       # the latest freeze / unfreeze
    for index, step in enumerate(steps):
        if step in (STEP_FREEZE, STEP_UNFREEZE):
            before = list(tasks)
        else:
            pcs = _step_pcs(request, item, step) or {"keys"}
            before = [last_on[pc] for pc in pcs if pc in last_on] + ([barrier] if barrier is not None else [])
        task = asyncio.create_task(_after(before, run_one(index, step)))
        if step in (STEP_FREEZE, STEP_UNFREEZE):
            barrier = task
        else:
            for pc in _step_pcs(request, item, step) or {"keys"}:
                last_on[pc] = task
        tasks.append(task)
    await asyncio.gather(*tasks)
    if failures or ran[0]:
        done("desktop", not failures, "; ".join(failures) or None,
             **({"partial": True} if failures and ran[0] else {}))


async def _after(before: list, step) -> None:
    """Run step once the steps it waits for are done (however)."""
    if before:
        await asyncio.gather(*before, return_exceptions=True)
    await step


def areas_ok(result) -> bool:
    """A step's answer said it worked."""
    return isinstance(result, dict) and result.get("ok") is not False and result.get("ok") is not None
