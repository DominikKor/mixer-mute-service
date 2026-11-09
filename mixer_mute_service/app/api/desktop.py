"""Desktop profiles: images, programs, files and music on the display PCs
(edited in config.yaml), and the Stage switch cookie."""

from __future__ import annotations

import asyncio
import logging

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from .. import display_agent
from ..actions import control_audio, network_program, show_profile
from ..config import DesktopProfileConfig, default_config_path
from ..desktop_profiles import ProfileEditError, add_profile, delete_profile, update_profile
from ..models import StagePreferenceRequest
from ..responses import fail, reply
from ..target_pcs import TargetPcError

router = APIRouter()
log = logging.getLogger("mixer.api.desktop")
# One config.yaml edit at a time.
_profile_lock = asyncio.Lock()


@router.get("/desktop/profiles")
async def desktop_profiles(request: Request):
    cfg = request.app.state.cfg.desktop_display
    return {
        "ready": display_agent.is_ready(cfg),
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
                "fade": profile.fade,
                "fade_seconds": profile.fade_seconds,
            }
            for profile_id, profile in cfg.profiles.items()
        ],
    }


def _validation_reason(exc: ValidationError) -> str:
    err = exc.errors()[0]
    field = ".".join(str(part) for part in err.get("loc", ())) or "profile"
    return f"{field}: {err.get('msg', 'invalid value')}"


async def _edit_config(request: Request, profile_id: str, edit, *args):
    """Run one config.yaml edit and take over the new profile list."""
    path = default_config_path()
    async with _profile_lock:
        try:
            cfg = await asyncio.to_thread(edit, path, profile_id, *args)
        except ProfileEditError as exc:
            log.warning("Desktop profile %s not saved: %s", profile_id, exc)
            return fail(str(exc), exc.status, id=profile_id)
        except OSError as exc:
            log.warning("Desktop profile %s not saved: %s", profile_id, exc)
            return fail(f"config.yaml not writable: {exc}", 500, id=profile_id)
        # Buttons appear/change without a restart; other settings still need one.
        request.app.state.cfg.desktop_display.profiles = cfg.desktop_display.profiles
    return None


async def _save_profile(request: Request, profile_id: str, body: dict, create: bool):
    if not isinstance(body, dict):
        return fail("profile must be a JSON object", 400)
    fields = {k: v for k, v in body.items() if k != "id"}
    try:
        profile = DesktopProfileConfig(**fields)
    except ValidationError as exc:
        return fail(_validation_reason(exc), 400)
    if not display_agent.allowed_target(profile.target_ip):
        return fail("target_ip: must be a LAN address", 400)
    if network_program(profile.kind, profile.path):
        return fail("path: programs must be on the target PC, not on a network share", 400)
    failed = await _edit_config(request, profile_id, add_profile if create else update_profile, profile)
    if failed:
        return failed
    log.info("Desktop profile %s %s", profile_id, "added" if create else "updated")
    return {"ok": True, "id": profile_id, "created": create}


@router.post("/desktop/profiles")
async def create_desktop_profile(request: Request, body: dict = Body(...)):
    profile_id = body.get("id") if isinstance(body, dict) else None
    if not isinstance(profile_id, str):
        return fail("id is required", 400)
    return await _save_profile(request, profile_id, body, create=True)


@router.put("/desktop/profiles/{profile_id}")
@router.post("/desktop/profiles/{profile_id}/update")
async def update_desktop_profile(profile_id: str, request: Request, body: dict = Body(...)):
    return await _save_profile(request, profile_id, body, create=False)


@router.delete("/desktop/profiles/{profile_id}")
async def delete_desktop_profile(profile_id: str, request: Request):
    failed = await _edit_config(request, profile_id, delete_profile)
    if failed:
        return failed
    log.info("Desktop profile %s deleted", profile_id)
    return {"ok": True, "id": profile_id, "deleted": True}


@router.post("/desktop/{profile_id}/open")
async def open_desktop_profile(profile_id: str, request: Request):
    return reply(*await show_profile(request, profile_id))


@router.post("/desktop/{profile_id}/audio/{action}")
async def control_desktop_audio(profile_id: str, action: str, request: Request):
    return reply(*await control_audio(request, profile_id, action))


# The Stage switch is kept per browser. The page stores it in localStorage, but
# Safari wipes script-written storage of a site unused for 7 days (a weekly
# service). A cookie set by the server is not subject to that, so it is kept
# here as well, for 10 years; the page reads it back on load.
@router.get("/target-pcs")
async def target_pcs(request: Request):
    store = request.app.state.target_pcs
    if store is None:
        return fail("target PCs are unavailable", 503)
    return {"ok": True, "pcs": await store.get()}


@router.put("/target-pcs")
async def update_target_pcs(request: Request, body: dict = Body(...)):
    """Save the names and addresses of the Streaming PC and the Anzeige PC.
    A changed address is carried over to everything that used the old one, so
    keys, songs and program rows keep their PC."""
    store = request.app.state.target_pcs
    if store is None:
        return fail("target PCs are unavailable", 503)
    pcs = body.get("pcs") if isinstance(body, dict) else None
    if isinstance(pcs, list) and any(
            isinstance(pc, dict) and not display_agent.allowed_target(str(pc.get("ip", "")).strip()) for pc in pcs):
        return fail("ip: must be a LAN address", 400)
    try:
        moves = await store.set(pcs)
    except TargetPcError as exc:
        return fail(str(exc), 400)
    moved = await _move_targets(request, moves) if moves else {"problems": []}
    if moves:
        log.info("Target PCs moved %s: %s", moves, moved)
    return {"ok": True, "pcs": await store.get(), "moved": moved}


async def _move_targets(request: Request, moves: dict) -> dict:
    """Point everything that names a moved PC by its old address at the new one
    (all at once, so swapping the two addresses works)."""
    state = request.app.state
    result = {"profiles": 0, "songs": False, "power": False, "program_rows": 0, "template_rows": 0, "problems": []}
    path = default_config_path()
    async with _profile_lock:
        for profile_id, profile in list(state.cfg.desktop_display.profiles.items()):
            if profile.target_ip not in moves:
                continue
            moved = profile.model_copy(update={"target_ip": moves[profile.target_ip]})
            try:
                cfg = await asyncio.to_thread(update_profile, path, profile_id, moved)
            except (ProfileEditError, OSError) as exc:
                result["problems"].append(f"{profile.title}: {exc}")
                continue
            state.cfg.desktop_display.profiles = cfg.desktop_display.profiles
            result["profiles"] += 1
    songs = state.songs
    if songs is not None and songs.settings.target_ip in moves:
        async with songs.lock:
            songs.save_settings(songs.settings.model_copy(update={"target_ip": moves[songs.settings.target_ip]}))
        result["songs"] = True
    if state.power_control is not None:
        result["power"] = await state.power_control.retarget(moves)
    if state.program_items is not None:
        result["program_rows"] = await state.program_items.retarget_files(moves)
    if state.program_templates is not None:
        result["template_rows"] = await state.program_templates.retarget_files(moves)
    return result


STAGE_COOKIE = "mixer_stage"
STAGE_COOKIE_MAX_AGE = 10 * 365 * 24 * 3600


@router.post("/ui/stage")
async def remember_stage(body: StagePreferenceRequest):
    response = JSONResponse({"ok": True, "on": body.on})
    response.set_cookie(
        STAGE_COOKIE, "1" if body.on else "0",
        max_age=STAGE_COOKIE_MAX_AGE, path="/", samesite="lax",
    )
    return response
