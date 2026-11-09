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
from fastapi.staticfiles import StaticFiles

from .agent_download import router as agent_download_router
from .buses import BusRegistry
from .channel_settings import ChannelSettingsCache, ChannelSettingsStore
from .camera_hls import router as camera_hls_router
from .camera_ptz import PanTiltControl
from .camera_webrtc import router as camera_webrtc_router
from .camera_streams import CameraStreamStore, MediaMTXRelay
from .channel_colors import router as channel_colors_router
from .channel_preferences import ChannelPreferences
from .companion_buttons import COMPANION_BUTTONS, CompanionButtonStore
from .config import default_config_path, load_config
from .controller import Controller
from .groups import GroupStore
from .mixer_client import MixerClient
from .native_controls import NativeControlService
from .power_control import PowerControlStore
from .proxy import ProxyServer
from .program_items import ProgramItemStore
from .program_templates import ProgramTemplateStore, router as program_templates_router
from .file_actions import router as file_actions_router
from .routes import router
from .songs import SongService, router as songs_router
from .updater import router as updater_router
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    cfg = load_config()
    store = StateStore()
    data_dir = default_config_path().parent
    camera_streams = CameraStreamStore(data_dir / "camera_streams.json")
    camera_relay = MediaMTXRelay(data_dir)
    relay_status = await camera_relay.configure(await camera_streams.relay_snapshot())
    if not relay_status["ready"]:
        logging.getLogger("mixer.camera_streams").warning(
            "Camera relay unavailable: %s", relay_status["reason"]
        )
    # Buses come from config plus those discovered from earlier dumps; new ones
    # are added from every dump (see app/buses.py).
    buses = BusRegistry(cfg, data_dir / "buses.json")
    # Gain/EQ/dynamics of every input, refreshed from each state dump.
    settings_cache = ChannelSettingsCache()
    buses.bus_map.on_settings = settings_cache.update
    channel_settings = ChannelSettingsStore(data_dir / "channel_settings.json")
    if cfg.mixer.mode == "proxy":
        client = ProxyServer(cfg, store, buses.bus_map)
    else:
        client = MixerClient(cfg, store, buses.bus_map)
    seed = {name: g.channels for name, g in cfg.groups.items()}
    groups = GroupStore(data_dir / "groups.json", cfg.valid_channels(), seed)
    program_items = ProgramItemStore(data_dir / "program_items.json")
    program_templates = ProgramTemplateStore(data_dir / "program_templates.json")
    songs = SongService(data_dir)
    native_controls = NativeControlService(data_dir / "native_control_state.json")
    camera_ptz = PanTiltControl()
    power_control = PowerControlStore(data_dir / "power_off_config.json")
    companion_button_store = CompanionButtonStore(
        data_dir / "companion_buttons.json", COMPANION_BUTTONS
    )
    channel_preferences = ChannelPreferences(data_dir / "channel_preferences.json")
    buses.on_new_bus(groups.allow_channel)
    controller = Controller(cfg, store, client, groups, channel_preferences)

    app.state.cfg = cfg
    app.state.store = store
    app.state.client = client
    app.state.groups = groups
    app.state.program_items = program_items
    app.state.program_templates = program_templates
    app.state.songs = songs
    app.state.native_controls = native_controls
    app.state.camera_ptz = camera_ptz
    app.state.power_control = power_control
    app.state.companion_button_store = companion_button_store
    app.state.channel_preferences = channel_preferences
    app.state.camera_streams = camera_streams
    app.state.camera_relay = camera_relay
    app.state.controller = controller
    app.state.settings_cache = settings_cache
    app.state.channel_settings = channel_settings
    app.state.build_id = _build_id()

    client.start()
    logging.getLogger("mixer.main").info(
        "service up: build=%s mode=%s mixer=%s:%s max_channels=%s freshness=%s "
        "max_fader_age=%s max_mute_age=%s buses=%s groups=%s",
        app.state.build_id,
        cfg.mixer.mode, cfg.mixer.ip, cfg.mixer.port, cfg.mixer.max_channels,
        cfg.safety.freshness_mode, cfg.safety.max_fader_age_seconds,
        cfg.safety.max_mute_age_seconds,
        [b.channel for b in cfg.mixer.buses], sorted(await groups.list()),
    )
    try:
        yield
    finally:
        try:
            await client.stop()
        finally:
            try:
                await camera_ptz.close()
            finally:
                await camera_relay.stop()


_configure_logging()
app = FastAPI(title="Mixer Mute Service", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(updater_router)
app.include_router(agent_download_router)
app.include_router(channel_colors_router)
app.include_router(songs_router)
app.include_router(program_templates_router)
app.include_router(file_actions_router)
app.include_router(camera_hls_router)
app.include_router(camera_webrtc_router)
app.mount("/", StaticFiles(directory=str(STATIC_DIR), html=True), name="ui")


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
