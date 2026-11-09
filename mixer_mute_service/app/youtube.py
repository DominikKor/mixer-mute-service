"""YouTube links in audio profiles: instead of a file on the target PC the
path may be a YouTube link; the display agent plays its sound with mpv and
yt-dlp. The same check is in desktop_agent/show_image_agent.py (the agent is
deployed on its own) and app/static/js/desktop.js."""

from __future__ import annotations

import re

YOUTUBE_URL_RE = re.compile(
    r"^https?://(?:(?:www|m|music)\.)?"
    r"(?:youtube\.com/(?:watch\?(?:[^\s#]*&)?v=[\w-]{6,}|shorts/[\w-]{6,}|live/[\w-]{6,}|embed/[\w-]{6,})"
    r"|youtu\.be/[\w-]{6,})[^\s]*$"
)


def is_youtube_url(value) -> bool:
    return (isinstance(value, str) and len(value) <= 500
            and not any(ord(c) < 32 for c in value) and YOUTUBE_URL_RE.match(value) is not None)
