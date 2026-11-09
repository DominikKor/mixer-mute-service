"""Steuerung: the user's keys and categories, pressing a key, and the beamer
state."""

from __future__ import annotations

from copy import deepcopy

from fastapi import APIRouter, Request

from ..actions import press_button
from ..companion_buttons import CATALOG_ONLY_BUTTONS, COMPANION_BUTTONS, COMPANION_BUTTONS_BY_ID
from ..models import (
    BeamerStateRequest,
    CompanionButtonCreate,
    CompanionButtonUpdate,
    CompanionCategoryCreate,
    CompanionCategoryVisibility,
)
from ..responses import fail, reply

router = APIRouter()


@router.get("/companion/buttons")
async def companion_buttons(request: Request):
    store = request.app.state.companion_button_store
    data = await store.snapshot()
    present = {button["id"] for button in data["buttons"]}
    data["companion_targets"] = [
        button for button in COMPANION_BUTTONS
        if button["kind"] != "channel" and button["id"] in present
    ]
    data["camera_buttons"] = await store.camera_buttons()
    # Light and mixer keys left Steuerung (v8); program rows may still use them.
    data["catalog_buttons"] = deepcopy(CATALOG_ONLY_BUTTONS)
    return data


@router.post("/companion/categories")
async def companion_category_create(body: CompanionCategoryCreate, request: Request):
    try:
        category = await request.app.state.companion_button_store.create_category(body.name)
    except ValueError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "category": category}


@router.post("/companion/categories/{category_id}/visibility")
async def companion_category_visibility(category_id: str, body: CompanionCategoryVisibility, request: Request):
    try:
        category = await request.app.state.companion_button_store.set_category_visible(category_id, body.visible)
    except KeyError:
        return fail("unknown category", 404)
    return {"ok": True, "category": category}


@router.post("/companion/buttons")
async def companion_button_create(body: CompanionButtonCreate, request: Request):
    cfg = request.app.state.cfg
    if body.kind == "companion":
        target = COMPANION_BUTTONS_BY_ID.get(body.target_button_id or "")
        if target is None or target["kind"] != "native":
            return fail("invalid control button target", 400)
        action = {"kind": "companion", "target_button_id": target["id"]}
    elif body.kind == "channel":
        if body.channel not in cfg.valid_channels():
            return fail("invalid channel", 400)
        if body.operation is None:
            return fail("operation is required", 400)
        action = {"kind": "channel", "channel": body.channel, "operation": body.operation}
    else:
        if not body.group or await request.app.state.groups.get(body.group) is None:
            return fail("unknown group", 404)
        if body.operation is None:
            return fail("operation is required", 400)
        action = {"kind": "group", "group": body.group, "operation": body.operation}
    try:
        button = await request.app.state.companion_button_store.create_button(body.label, body.category_id, action)
    except ValueError as exc:
        return fail(str(exc), 400)
    except KeyError:
        return fail("unknown category", 404)
    return {"ok": True, "button": button}


@router.post("/companion/buttons/{button_id}/update")
async def companion_button_update(button_id: str, body: CompanionButtonUpdate, request: Request):
    try:
        button = await request.app.state.companion_button_store.update_button(
            button_id, body.label, body.category_id
        )
    except ValueError as exc:
        return fail(str(exc), 400)
    except KeyError:
        return fail("unknown button or category", 404)
    return {"ok": True, "button": button}


@router.post("/companion/buttons/{button_id}/delete")
async def companion_button_delete(button_id: str, request: Request):
    try:
        await request.app.state.companion_button_store.delete_button(button_id)
    except KeyError:
        return fail("unknown button", 404)
    return {"ok": True, "button_id": button_id, "deleted": True}


@router.post("/companion/buttons/{button_id}/press")
async def companion_button_press(button_id: str, request: Request):
    return reply(*await press_button(request, button_id))


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
