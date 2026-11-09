"""All HTTP routes, one module per area. Handlers stay thin: they call the
controller, a store or app/actions.py and turn the result into an answer
(app/responses.py). On any block the answer is 4xx with a clear reason,
never a silent success."""

from fastapi import APIRouter

from ..agent_download import router as agent_download_router
from ..camera_hls import router as camera_hls_router
from ..camera_webrtc import router as camera_webrtc_router
from ..channel_colors import router as channel_colors_router
from ..file_actions import router as file_actions_router
from ..lights import router as lights_router
from ..program_templates import router as program_templates_router
from ..songs import router as songs_router
from ..updater import router as updater_router
from . import cameras, controls, desktop, groups, mixer, power, program

router = APIRouter()
for _sub in (
    controls.router,
    mixer.router,
    groups.router,
    program.router,
    desktop.router,
    power.router,
    cameras.router,
    updater_router,
    agent_download_router,
    channel_colors_router,
    songs_router,
    program_templates_router,
    lights_router,
    file_actions_router,
    camera_hls_router,
    camera_webrtc_router,
):
    router.include_router(_sub)
