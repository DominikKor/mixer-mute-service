"""Application entrypoint: wire config, state, mixer client and controller
together, start the background listener, and expose the FastAPI app.

Run with:
    uvicorn app.main:app --host 0.0.0.0 --port 8765
or:
    python -m app.main
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from pathlib import Path
import os

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .agent_download import router as agent_download_router
from .buses import BusRegistry
from .channel_colors import router as channel_colors_router
from .channel_preferences import ChannelPreferences
from .config import default_config_path, load_config
from .controller import Controller
from .groups import GroupStore
from .mixer_client import MixerClient
from .proxy import ProxyServer
from .routes import router
from .updater import router as updater_router
from .state import StateStore

import sys

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
    # Buses come from config plus those discovered from earlier dumps; new ones
    # are added from every dump (see app/buses.py).
    buses = BusRegistry(cfg, data_dir / "buses.json")
    if cfg.mixer.mode == "proxy":
        client = ProxyServer(cfg, store, buses.bus_map)
    else:
        client = MixerClient(cfg, store, buses.bus_map)
    seed = {name: g.channels for name, g in cfg.groups.items()}
    groups = GroupStore(data_dir / "groups.json", cfg.valid_channels(), seed)
    channel_preferences = ChannelPreferences(data_dir / "channel_preferences.json")
    buses.on_new_bus(groups.allow_channel)
    controller = Controller(cfg, store, client, groups, channel_preferences)

    app.state.cfg = cfg
    app.state.store = store
    app.state.client = client
    app.state.groups = groups
    app.state.channel_preferences = channel_preferences
    app.state.controller = controller
    app.state.build_id = _build_id()

    client.start()
    logging.getLogger("mixer.main").info(
        "service up: build=%s mode=%s mixer=%s:%s max_channels=%s max_fader_age=%ss groups=%s",
        app.state.build_id,
        cfg.mixer.mode, cfg.mixer.ip, cfg.mixer.port, cfg.mixer.max_channels,
        cfg.safety.max_fader_age_seconds, list(cfg.groups),
    )
    try:
        yield
    finally:
        await client.stop()


_configure_logging()
app = FastAPI(title="Mixer Mute Service", version="0.1.0", lifespan=lifespan)
app.include_router(router)
app.include_router(updater_router)
app.include_router(agent_download_router)
app.include_router(channel_colors_router)
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
