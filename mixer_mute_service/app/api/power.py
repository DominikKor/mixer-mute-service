"""Power off at the end: the selected PCs (through their display agents)
and the beamer."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .. import display_agent
from ..display_agent import AgentError
from ..models import PowerOffConfigRequest
from ..native_controls import NativeControlError
from ..responses import Result, fail

router = APIRouter()


def _agent_targets(cfg) -> list[dict]:
    """The PCs of the desktop profiles, each with the profile titles on it."""
    labels: dict[str, list[str]] = {}
    for profile in cfg.desktop_display.profiles.values():
        labels.setdefault(profile.target_ip, []).append(profile.title)
    return [
        {"ip": ip, "label": ", ".join(dict.fromkeys(names))}
        for ip, names in sorted(labels.items())
    ]


async def _shutdown_agent(target_ip: str, cfg) -> Result:
    try:
        return await display_agent.call(cfg.desktop_display, target_ip, "/power", {"action": "shutdown"}), 202
    except AgentError as exc:
        if exc.agent_status is None:
            return exc.answer(), exc.status
        if exc.body is not None:
            return exc.body, exc.agent_status
        return {"ok": False, "reason": "display agent rejected the shutdown request"}, exc.agent_status


@router.get("/power/off-config")
async def power_off_config(request: Request):
    available_targets = _agent_targets(request.app.state.cfg)
    selection = await request.app.state.power_control.get({item["ip"] for item in available_targets})
    return {
        "ok": True,
        "available_targets": available_targets,
        "selected_targets": selection["targets"],
        "beamer": selection["beamer"],
    }


@router.put("/power/off-config")
async def power_off_config_update(body: PowerOffConfigRequest, request: Request):
    available = {item["ip"] for item in _agent_targets(request.app.state.cfg)}
    try:
        selection = await request.app.state.power_control.set(body.target_ips, body.beamer, available)
    except ValueError as exc:
        return fail(str(exc), 400)
    return {"ok": True, "selected_targets": selection["targets"], "beamer": selection["beamer"]}


@router.post("/power/agents/{target_ip}/shutdown")
async def power_off_agent(target_ip: str, request: Request):
    available = {item["ip"] for item in _agent_targets(request.app.state.cfg)}
    if target_ip not in available:
        return fail("unknown agent PC", 404)
    result, status = await _shutdown_agent(target_ip, request.app.state.cfg)
    return JSONResponse(result, status_code=status)


@router.post("/power/off")
async def power_off_selected(request: Request):
    cfg = request.app.state.cfg
    available = {item["ip"] for item in _agent_targets(cfg)}
    selection = await request.app.state.power_control.get(available)
    if not selection["targets"] and not selection["beamer"]:
        return fail("no shutdown targets selected", 400)

    results = []
    if selection["beamer"]:
        try:
            result = await request.app.state.native_controls.press(
                {"type": "beamer-command", "command": "power-off"}
            )
            results.append({"target": "beamer", **result})
        except NativeControlError as exc:
            results.append({"target": "beamer", "ok": False, "reason": str(exc)})
    for target_ip in selection["targets"]:
        result, status = await _shutdown_agent(target_ip, cfg)
        results.append({"target": target_ip, **result, "status": status})

    ok = all(result.get("ok") for result in results)
    return JSONResponse({"ok": ok, "results": results}, status_code=200 if ok else 207)
