"""Shared helpers for the small JSON files next to config.yaml.

Every store (groups, buses, program, Steuerung keys, ...) used to write its
file in place and to fall back to defaults when it could not parse it. A crash
or power cut mid-write therefore silently wiped the user's data on the next
start. These helpers write atomically (temp file + os.replace) and keep an
unreadable file as ``<name>.corrupt`` before anything overwrites it.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("mixer.jsonstore")

_MISSING = object()


class CorruptJSONError(ValueError):
    """The file exists but does not contain valid JSON of the expected type."""


def read_json(path: Path, expected_type: type, default: Any = _MISSING) -> Any:
    """Return the parsed contents of ``path``.

    A missing file returns ``default`` (or raises FileNotFoundError when no
    default is given). An unreadable file is copied to ``<name>.corrupt``,
    logged, and then treated like a missing one, so the caller can start from
    defaults without losing the broken file for good.
    """
    try:
        text = path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        if default is _MISSING:
            raise
        return default
    try:
        data = json.loads(text) if text.strip() else expected_type()
        if not isinstance(data, expected_type):
            raise CorruptJSONError(
                f"expected {expected_type.__name__}, got {type(data).__name__}"
            )
        return data
    except (ValueError, TypeError) as exc:
        try:
            backup = backup_corrupt(path)
            log.error("Unreadable %s (%s); kept a copy as %s", path, exc, backup)
        except OSError as copy_exc:
            log.error("Unreadable %s (%s); backup failed: %s", path, exc, copy_exc)
        if default is _MISSING:
            raise CorruptJSONError(str(exc)) from exc
        return default


def backup_corrupt(path: Path) -> Path:
    """Copy a broken file to ``<name>.corrupt`` and return the copy's path.

    An existing copy with other contents is never overwritten (a timestamped
    name is used instead), so the first broken version stays available.
    """
    backup = path.with_name(path.name + ".corrupt")
    if backup.exists() and backup.read_bytes() != path.read_bytes():
        backup = path.with_name(f"{path.name}.corrupt-{time.strftime('%Y%m%d-%H%M%S')}")
    shutil.copyfile(path, backup)
    return backup


def write_json_atomic(path: Path, data: Any, **dumps_kwargs: Any) -> None:
    """Write ``data`` as JSON so a crash leaves either the old or the new file."""
    dumps_kwargs.setdefault("indent", 2)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(data, **dumps_kwargs))
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)
