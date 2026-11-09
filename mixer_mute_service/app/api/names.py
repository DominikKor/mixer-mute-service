"""Renaming: user names for fixed things (app/names.py), light scenes,
program templates and Steuerung categories.

PUT  /names/{kind}/{id} {name}            kind: light-zone, light-circuit,
                                          power-target ("" = built-in name)
PUT  /lights/scenes/{id} {name}           rename a light scene
PUT  /program/templates/{id} {name}       rename a program template
POST /companion/categories/{id}/rename {name}
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Body, Request

from ..lights import SceneError
from ..program_templates import TemplateError
from ..responses import fail

router = APIRouter()
log = logging.getLogger("mixer.names")


@router.put("/names/{kind}/{item_id}")
async def set_name(kind: str, item_id: str, request: Request, body: dict = Body(...)):
    names = request.app.state.names
    if names is None:
        return fail("names are not available", 503)
    try:
        name = await names.set(kind, item_id, body.get("name", ""))
    except KeyError:
        return fail("unknown name target", 404)
    except ValueError as exc:
        return fail(str(exc), 400)
    log.info("Name %s/%s -> %r", kind, item_id, name or "(built-in)")
    return {"ok": True, "kind": kind, "id": item_id, "name": name}


@router.put("/lights/scenes/{scene_id}")
async def rename_light_scene(scene_id: str, request: Request, body: dict = Body(...)):
    scenes = request.app.state.light_scenes
    if scenes is None:
        return fail("light control is not available", 503)
    try:
        scene = await scenes.rename(scene_id, body.get("name"))
    except KeyError:
        return fail("unknown light scene", 404)
    except FileExistsError:
        return fail("a light scene with this name already exists", 409)
    except SceneError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "scene": scene}


@router.put("/program/templates/{template_id}")
async def rename_program_template(template_id: str, request: Request, body: dict = Body(...)):
    templates = request.app.state.program_templates
    if templates is None:
        return fail("program templates are not available", 503)
    try:
        template = await templates.rename(template_id, body.get("name"))
    except KeyError:
        return fail("unknown template", 404)
    except FileExistsError:
        return fail("a template with this name already exists", 409)
    except TemplateError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "template": template}


@router.post("/companion/categories/{category_id}/rename")
async def rename_companion_category(category_id: str, request: Request, body: dict = Body(...)):
    store = request.app.state.companion_button_store
    if store is None:
        return fail("control keys are not available", 503)
    try:
        category = await store.rename_category(category_id, body.get("name"))
    except KeyError:
        return fail("unknown category", 404)
    except ValueError as exc:
        return fail(str(exc), 409 if "exists" in str(exc) else 400)
    return {"ok": True, "category": category}
