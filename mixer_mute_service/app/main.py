"""Application entrypoint: wire config, state, mixer client and controller
together, start the background listener, and expose the FastAPI app.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port 8765
or:
    python -m app.main
"""

from __future__ import annotations

import logging
import os
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from .buses import BusRegistry
from .channel_settings import ChannelSettingsCache, ChannelSettingsStore
from .camera_ptz import PanTiltControl
from .camera_streams import CameraStreamStore, MediaMTXRelay
from .channel_preferences import ChannelPreferences
from .companion_buttons import COMPANION_BUTTONS, CompanionButtonStore
from .config import default_config_path, load_config
from .controller import Controller
from .group_presets import GroupPresetStore
from .groups import GroupStore
from .mixer_client import MixerClient
from .native_controls import NativeControlService
from .power_control import PowerControlStore
from .proxy import ProxyServer
from .request_guard import refusal
from .program_items import ProgramItemStore
from .program_templates import ProgramTemplateStore
from .api import router
from .songs import SongService
from .state import StateStore

if getattr(sys, "frozen", False):
    # Running as a PyInstaller bundle: prefer app/static/ next to the .exe
    # (updateable without a full rebuild) and fall back to the bundled copy.
    _exe_dir = Path(sys.executable).parent
    _override = _exe_dir / "app" / "static"
    STATIC_DIR = _override if _override.exists() else Path(sys._MEIPASS) / "app" / "static"
else:
    STATIC_DIR = Path(__file__).resolve().parent / "static"


def _configure_logging() -> None:
    level = os.environ.get("MIXER_LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )


def _build_id() -> str:
    env = os.environ.get("MIXER_BUILD_ID", "").strip()
    if env:
        return env
    if getattr(sys, "frozen", False):
        bid_file = Path(sys.executable).parent / "build-id.txt"
        if bid_file.exists():
            try:
                value = bid_file.read_text(encoding="utf-8").strip()
                if value:
                    return value
            except OSError:
                pass
    return "dev"


log = logging.getLogger("mixer.main")


def _optional(app: FastAPI, name: str, factory):
    """Start an optional feature. A failure there (a bad camera entry, an
    unreadable songs file, ...) must never stop the mixer control: it is
    logged, the feature stays unavailable (None) and is listed in
    app.state.feature_errors."""
    try:
        return factory()
    except Exception as exc:  # noqa: BLE001
        log.exception("%s could not start; the service runs without it", name)
        app.state.feature_errors[name] = f"{type(exc).__name__}: {exc}"
        return None


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.feature_errors = {}
    cfg = load_config()
    data_dir = default_config_path().parent

    # -- core: the mixer link and everything a mute or fader command needs --
    store = StateStore()
    # Buses come from config plus those discovered from earlier dumps; new ones
    # are added from every dump (see app/buses.py).
    buses = BusRegistry(cfg, data_dir / "buses.json")
    # Gain/EQ/dynamics of every input, refreshed from each state dump.
    settings_cache = ChannelSettingsCache()
    link_args = dict(
        bus_map=buses.bus_map,
        on_bus_objects=buses.discover,
        dump_listeners=[settings_cache.on_dump],
    )
    if cfg.mixer.mode == "proxy":
        client = ProxyServer(cfg, store, **link_args)
    else:
        client = MixerClient(cfg, store, **link_args)
    seed = {name: g.channels for name, g in cfg.groups.items()}
    groups = GroupStore(data_dir / "groups.json", cfg.valid_channels(), seed)
    buses.on_new_bus(groups.allow_channel)
    channel_preferences = ChannelPreferences(data_dir / "channel_preferences.json")
    controller = Controller(
        cfg, store, client, groups, channel_preferences, panic_path=data_dir / "panic_state.json"
    )

    app.state.cfg = cfg
    app.state.store = store
    app.state.client = client
    app.state.groups = groups
    app.state.channel_preferences = channel_preferences
    app.state.controller = controller
    app.state.settings_cache = settings_cache
    app.state.build_id = _build_id()
    client.start()

    # -- optional features: each one may fail on its own -------------------
    app.state.group_presets = _optional(
        app, "group presets", lambda: GroupPresetStore(data_dir / "group_presets.json"))
    app.state.channel_settings = _optional(
        app, "channel settings", lambda: ChannelSettingsStore(data_dir / "channel_settings.json"))
    app.state.program_items = _optional(
        app, "program", lambda: ProgramItemStore(data_dir / "program_items.json"))
    app.state.songs = _optional(app, "songs", lambda: SongService(data_dir))
    app.state.program_templates = _optional(
        app, "program templates", lambda: ProgramTemplateStore(data_dir / "program_templates.json"))
    camera_ptz = _optional(app, "camera pan/tilt", PanTiltControl)
    app.state.camera_ptz = camera_ptz
    app.state.native_controls = _optional(
        app, "native controls", lambda: NativeControlService(data_dir / "native_control_state.json"))
    app.state.power_control = _optional(
        app, "power control", lambda: PowerControlStore(data_dir / "power_off_config.json"))
    app.state.companion_button_store = _optional(
        app, "control keys", lambda: CompanionButtonStore(data_dir / "companion_buttons.json", COMPANION_BUTTONS))
    camera_streams = _optional(
        app, "cameras", lambda: CameraStreamStore(data_dir / "camera_streams.json"))
    camera_relay = _optional(app, "camera relay", lambda: MediaMTXRelay(data_dir))
    if camera_streams is not None and camera_relay is not None:
        try:
            relay_status = await camera_relay.configure(await camera_streams.relay_snapshot())
            if not relay_status["ready"]:
                logging.getLogger("mixer.camera_streams").warning(
                    "Camera relay unavailable: %s", relay_status["reason"]
                )
        except Exception as exc:  # noqa: BLE001 - cameras must never stop the mixer
            log.exception("camera relay could not start; the service runs without it")
            app.state.feature_errors["camera relay"] = f"{type(exc).__name__}: {exc}"
        else:
            camera_relay.start_supervision()
    app.state.camera_streams = camera_streams
    app.state.camera_relay = camera_relay

    log.info(
        "service up: build=%s mode=%s mixer=%s:%s max_channels=%s freshness=%s "
        "max_fader_age=%s max_mute_age=%s buses=%s groups=%s sending=%s",
        app.state.build_id,
        cfg.mixer.mode, cfg.mixer.ip, cfg.mixer.port, cfg.mixer.max_channels,
        cfg.safety.freshness_mode, cfg.safety.max_fader_age_seconds,
        cfg.safety.max_mute_age_seconds,
        [b.channel for b in cfg.mixer.buses], sorted(await groups.list()),
        "enabled" if controller.enabled else "DISABLED (panic, from before the restart)",
    )
    if app.state.feature_errors:
        log.error("features unavailable: %s", app.state.feature_errors)
    try:
        yield
    finally:
        try:
            await client.stop()
        finally:
            try:
                if camera_ptz is not None:
                    await camera_ptz.close()
            finally:
                if camera_relay is not None:
                    await camera_relay.stop()


_configure_logging()
app = FastAPI(title="Mixer Mute Service", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="ui")


@app.exception_handler(RequestValidationError)
async def _invalid_request(request, exc: RequestValidationError):
    """Invalid bodies get the same answer shape as every other refusal (the
    page shows "reason"); FastAPI's "detail" list stays for API clients."""
    errors = exc.errors()
    first = errors[0] if errors else {}
    where = ".".join(str(part) for part in first.get("loc", ()) if part != "body")
    reason = f"{where}: {first.get('msg', 'invalid request')}" if where else first.get("msg", "invalid request")
    return JSONResponse({"ok": False, "reason": reason, "detail": jsonable_encoder(errors)}, status_code=422)


@app.middleware("http")
async def _guard(request, call_next):
    """No requests from other web sites (app/request_guard.py)."""
    cfg = getattr(request.app.state, "cfg", None)
    extra = cfg.server.allowed_hosts if cfg is not None else ()
    reason = refusal(request.method, request.headers, extra)
    if reason is not None:
        log.warning("Refused %s %s: %s", request.method, request.url.path, reason)
        return JSONResponse({"ok": False, "reason": reason}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def _revalidate_ui(request, call_next):
    """Browsers (iPad Safari in particular) may otherwise keep showing an old
    UI after an update; make them check the page on every load."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.endswith(".html"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def main() -> None:
    import uvicorn

    cfg = load_config()
    uvicorn.run(
        "app.main:app",
        host=cfg.server.host,
        port=cfg.server.port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
