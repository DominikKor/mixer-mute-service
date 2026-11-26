"""The kind of a desktop profile (image, audio, program, file), taken from its
path instead of being chosen by hand.

- a YouTube link: audio (the agent plays its sound),
- Mission Spotlight (the path is a folder): file for a new profile; the
  folder says nothing about the week's file, so an existing Spotlight
  profile keeps its kind (image, file or audio: weekly music plays with mpv),
- an image or audio extension: image / audio,
- .exe: program (only .exe files are started as programs; .lnk, .bat and
  .cmd open through Windows like any other file, which runs them),
- anything else (video, PDF, slides, .url, ...): file, opened with the
  Windows default app.

A name without an extension (e.g. ``obs64*``) says nothing either; then the
kind the profile already has is kept, and a new profile becomes a file.

The page shows the same result live (app/static/js/desktop.js,
profileKindOf); tests/test_profile_kind.py checks both agree.
"""

from __future__ import annotations

from pathlib import PureWindowsPath
from typing import Optional

from .youtube import is_youtube_url

IMAGE_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".jfif", ".gif", ".bmp", ".webp", ".tif", ".tiff", ".heic", ".svg", ".ico",
})
AUDIO_EXTENSIONS = frozenset({
    ".mp3", ".wav", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wma", ".aif", ".aiff", ".mka",
})
PROGRAM_EXTENSIONS = frozenset({".exe"})
KINDS = ("image", "audio", "program", "file")


def detect_kind(path: str, mission_spotlight: bool = False) -> Optional[str]:
    """The kind the path asks for, or None when it says nothing (no extension)."""
    value = (path or "").strip()
    if is_youtube_url(value):
        return "audio"
    if mission_spotlight:
        return None  # a folder: the kind is not in the path
    name = PureWindowsPath(value).name
    if not name or name.endswith("*") or "." not in name.strip("."):
        return None
    suffix = "." + name.rsplit(".", 1)[1].lower()
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    if suffix in PROGRAM_EXTENSIONS:
        return "program"
    return "file"


def profile_kind(path: str, mission_spotlight: bool = False, *, sent: Optional[str] = None,
                 stored: Optional[dict] = None) -> str:
    """The kind a saved profile gets.

    stored: the profile as it was ({"path", "kind", "mission_spotlight"}) when
    an existing one is edited. Its kind stays while path and Mission
    Spotlight are unchanged (also against a different kind sent by an old
    page). Otherwise the path decides. Only when it says nothing does a
    kind sent by an old page count, then the stored one, then "file".
    """
    if stored and stored.get("kind") in KINDS and (stored.get("path") or "") == (path or "") \
            and bool(stored.get("mission_spotlight")) == bool(mission_spotlight):
        return stored["kind"]
    detected = detect_kind(path, mission_spotlight)
    if detected:
        return detected
    for kind in (sent, (stored or {}).get("kind")):
        if kind in KINDS and not (mission_spotlight and kind == "program"):
            return kind
    return "file"
