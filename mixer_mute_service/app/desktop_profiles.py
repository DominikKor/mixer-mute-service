"""Add and edit desktop display profiles in config.yaml from the web UI.

config.yaml is hand-written (comments, other profiles, mixer settings), so it is
never re-serialised. Every change is a small splice into the file's current
text, located with PyYAML's node positions:

- adding a profile inserts its lines after the last existing profile,
- editing a profile replaces only the values that changed and adds missing
  keys; comments, other keys and key order stay as they are.

Before anything is written, the edited text is parsed again and must equal the
old file with only that one profile added or changed, and must still load as a
valid Config. Otherwise nothing is written. The file is re-read for every
change (manual edits made while the service runs are kept), the previous
version is kept as config.yaml.bak, and the new one replaces it atomically.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path, PureWindowsPath
from typing import List, Optional, Tuple

import yaml
from pydantic import ValidationError

from .config import Config, DesktopProfileConfig

PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
# Order in which a new profile's keys are written.
FIELD_ORDER = ("title", "kind", "path", "args", "target_ip", "fullscreen", "monitor", "position")
_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f]")
_PRINTABLE_ASCII_RE = re.compile(r"^[\x20-\x7e]*$")
_EMPTY_VALUE_RE = re.compile(r"^[ \t]*(\{[ \t]*\}|null|Null|NULL|~)?[ \t]*(#.*)?$")


class ProfileEditError(ValueError):
    """The change is invalid or cannot be applied safely; nothing was written."""

    def __init__(self, reason: str, status: int = 400) -> None:
        super().__init__(reason)
        self.status = status


def validate_profile(profile: DesktopProfileConfig) -> None:
    """Checks the display agent applies on the target PC, done here so the UI
    gets the reason when saving instead of a failure when the button is used.
    Not part of DesktopProfileConfig, so existing hand-written profiles keep
    loading."""
    if not profile.title.strip():
        raise ProfileEditError("title must not be empty")
    for value in (profile.title, profile.path, *profile.args):
        if _CONTROL_RE.search(value):
            raise ProfileEditError("text must not contain line breaks or control characters")
    if len(profile.title) > 80:
        raise ProfileEditError("title is too long (max 80 characters)")
    path = PureWindowsPath(profile.path)
    if not path.is_absolute():
        raise ProfileEditError(r"path must be an absolute Windows path, e.g. C:\Folder\file.png")
    if profile.kind == "program" and path.suffix.lower() != ".exe":
        raise ProfileEditError("a program path must point to an .exe file")
    if profile.kind != "program" and profile.args:
        raise ProfileEditError("arguments are only used for programs")


# -- YAML rendering ----------------------------------------------------------


def _scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        return "[" + ", ".join(_scalar(v) for v in value) + "]"
    text = str(value)
    if _PRINTABLE_ASCII_RE.match(text):
        if "\\" not in text and '"' not in text:
            return f'"{text}"'
        return "'" + text.replace("'", "''") + "'"
    # Non-ASCII: JSON escapes are valid YAML double-quoted escapes and keep the
    # file pure ASCII, whatever encoding it is later read with.
    return json.dumps(text)


def _profile_values(profile: DesktopProfileConfig) -> dict:
    data = profile.model_dump()
    return {key: data[key] for key in FIELD_ORDER}


# -- locating nodes ----------------------------------------------------------


def _mapping_get(node, key: str):
    """(key node, value node) of a block mapping entry, or None."""
    found = [(k, v) for k, v in node.value if isinstance(k, yaml.ScalarNode) and k.value == key]
    if len(found) > 1:
        raise ProfileEditError(f"config.yaml defines {key!r} twice; edit it by hand", 409)
    return found[0] if found else None


def _last_end(node) -> int:
    """Index just after the last character that belongs to the node. Block
    collections end at the next token (after any comments), so use the end of
    their last scalar instead."""
    if isinstance(node, yaml.ScalarNode) or getattr(node, "flow_style", False):
        return node.end_mark.index
    children = []
    for item in node.value:
        children.extend(item if isinstance(item, tuple) else (item,))
    if not children:
        return node.end_mark.index
    return max(_last_end(child) for child in children)


def _content_end(text: str, node) -> int:
    """_last_end without the line break a block scalar (| or >) ends with."""
    end = _last_end(node)
    while end > 0 and text[end - 1] in "\r\n":
        end -= 1
    return end


def _line_end(text: str, index: int) -> int:
    """Index of the line break ending the line that contains index."""
    for pos in range(index, len(text)):
        if text[pos] in "\r\n":
            return pos
    return len(text)


def _is_block_mapping(node) -> bool:
    return isinstance(node, yaml.MappingNode) and not node.flow_style


def _is_empty(node) -> bool:
    return (
        (isinstance(node, yaml.ScalarNode) and node.tag == "tag:yaml.org,2002:null")
        or (isinstance(node, yaml.MappingNode) and not node.value)
    )


def _child_indent(node, fallback: int) -> int:
    return node.value[0][0].start_mark.column if node.value else fallback


# -- text edits --------------------------------------------------------------


def _block(lines: List[Tuple[int, str]], nl: str) -> str:
    return "".join(nl + " " * indent + line for indent, line in lines)


def _profile_lines(profile_id: str, values: dict, indent: int, field_indent: int) -> List[Tuple[int, str]]:
    # An id like "123" or "true" would load as a number/bool: quote it then.
    key = profile_id if yaml.safe_load(f"{profile_id}: 0") == {profile_id: 0} else _scalar(profile_id)
    lines = [(indent, f"{key}:")]
    for key in FIELD_ORDER:
        if key == "args" and not values[key]:
            continue
        lines.append((field_indent, f"{key}: {_scalar(values[key])}"))
    return lines


def _replace_empty_value(text: str, key_node, block: str) -> str:
    """Turn `key:` / `key: {}` / `key: null` into `key:` followed by block."""
    colon = key_node.end_mark.index
    end = _line_end(text, colon)
    rest = text[colon:end]
    if not rest.lstrip().startswith(":"):
        raise ProfileEditError("config.yaml layout not understood; edit it by hand", 409)
    match = _EMPTY_VALUE_RE.match(rest.lstrip()[1:])
    if not match:
        raise ProfileEditError("config.yaml layout not understood; edit it by hand", 409)
    comment = f"  {match.group(2)}" if match.group(2) else ""
    return text[:colon] + ":" + comment + block + text[end:]


def _insert_after(text: str, node, block: str) -> str:
    pos = _line_end(text, _content_end(text, node))
    return text[:pos] + block + text[pos:]


def _add_text(text: str, profile_id: str, values: dict, nl: str) -> str:
    root = yaml.compose(text) if text.strip() else None
    if root is None:
        lines = [(0, "desktop_display:"), (2, "profiles:")] + _profile_lines(profile_id, values, 4, 6)
        return text + _block(lines, nl).lstrip("\r\n") + nl
    if not _is_block_mapping(root):
        raise ProfileEditError("config.yaml layout not understood; edit it by hand", 409)
    top = _child_indent(root, 0)
    step = 2
    entry = _mapping_get(root, "desktop_display")
    if entry is None:
        lines = [(top, "desktop_display:"), (top + step, "profiles:")]
        lines += _profile_lines(profile_id, values, top + 2 * step, top + 3 * step)
        sep = "" if text.endswith(("\n", "\r")) else nl
        return text + sep + _block(lines, nl).lstrip("\r\n") + nl
    dd_key, dd = entry
    if _is_empty(dd):
        base = dd_key.start_mark.column + step
        lines = [(base, "profiles:")] + _profile_lines(profile_id, values, base + step, base + 2 * step)
        return _replace_empty_value(text, dd_key, _block(lines, nl))
    if not _is_block_mapping(dd):
        raise ProfileEditError("desktop_display in config.yaml is not a block mapping; edit it by hand", 409)
    dd_indent = _child_indent(dd, dd_key.start_mark.column + step)
    entry = _mapping_get(dd, "profiles")
    if entry is None:
        lines = [(dd_indent, "profiles:")]
        lines += _profile_lines(profile_id, values, dd_indent + step, dd_indent + 2 * step)
        return _insert_after(text, dd, _block(lines, nl))
    pr_key, profiles = entry
    if _is_empty(profiles):
        base = pr_key.start_mark.column + step
        return _replace_empty_value(text, pr_key, _block(_profile_lines(profile_id, values, base, base + step), nl))
    if not _is_block_mapping(profiles):
        raise ProfileEditError("desktop_display.profiles is not a block mapping; edit it by hand", 409)
    indent = _child_indent(profiles, pr_key.start_mark.column + step)
    first = profiles.value[0][1]
    field_indent = _child_indent(first, indent + step) if _is_block_mapping(first) else indent + step
    return _insert_after(text, profiles, _block(_profile_lines(profile_id, values, indent, field_indent), nl))


def _profile_node(text: str, profile_id: str):
    root = yaml.compose(text) if text.strip() else None
    dd = _mapping_get(root, "desktop_display") if _is_block_mapping(root) else None
    profiles = _mapping_get(dd[1], "profiles") if dd and _is_block_mapping(dd[1]) else None
    entry = _mapping_get(profiles[1], profile_id) if profiles and _is_block_mapping(profiles[1]) else None
    if entry is None:
        raise ProfileEditError("unknown desktop profile", 404)
    if not _is_block_mapping(entry[1]) or not entry[1].value:
        raise ProfileEditError("this profile is not written as a block mapping; edit it by hand", 409)
    return entry[1]


def _update_text(text: str, profile_id: str, values: dict, old_raw: dict, nl: str) -> str:
    """Replace changed values in place and add missing keys, one edit at a time
    (each edit shifts the positions, so the file is parsed again in between)."""
    defaults = DesktopProfileConfig.model_fields
    for key in FIELD_ORDER:
        new = values[key]
        if key in old_raw:
            if old_raw[key] == new:
                continue
            node = _profile_node(text, profile_id)
            _, value_node = _mapping_get(node, key)
            if isinstance(value_node, yaml.MappingNode):
                raise ProfileEditError(f"{key} in this profile has an unexpected layout; edit it by hand", 409)
            start, end = value_node.start_mark.index, _content_end(text, value_node)
            if start >= end:  # `key:` with no value
                text = text[:start] + " " + _scalar(new) + text[start:]
            else:
                text = text[:start] + _scalar(new) + text[end:]
        else:
            field = defaults[key]
            default = field.default_factory() if field.default_factory else field.default
            if new == default:
                continue
            node = _profile_node(text, profile_id)
            indent = node.value[0][0].start_mark.column
            text = _insert_after(text, node, _block([(indent, f"{key}: {_scalar(new)}")], nl))
    return text


# -- verification and writing ------------------------------------------------


def _load(text: str) -> dict:
    data = yaml.safe_load(text) if text.strip() else {}
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ProfileEditError("config.yaml is not a mapping; edit it by hand", 409)
    return data


def _profiles_of(data: dict) -> dict:
    dd = data.get("desktop_display") or {}
    return dict(dd.get("profiles") or {}) if isinstance(dd, dict) else {}


def _verify(old: dict, new: dict, profile_id: str, profile: DesktopProfileConfig) -> Config:
    """The new file must be the old one with only this profile added/changed."""
    rest_old = {k: v for k, v in old.items() if k != "desktop_display"}
    rest_new = {k: v for k, v in new.items() if k != "desktop_display"}
    dd_old = {k: v for k, v in (old.get("desktop_display") or {}).items() if k != "profiles"}
    dd_new = {k: v for k, v in (new.get("desktop_display") or {}).items() if k != "profiles"}
    p_old, p_new = _profiles_of(old), _profiles_of(new)
    written = p_new.pop(profile_id, None)
    previous = p_old.pop(profile_id, None)
    ok = rest_old == rest_new and dd_old == dd_new and p_old == p_new and isinstance(written, dict)
    if ok and isinstance(previous, dict):
        # Keys the model does not know (or comments) must survive an edit.
        ok = all(written.get(k) == v for k, v in previous.items() if k not in FIELD_ORDER)
    if ok:
        try:
            ok = DesktopProfileConfig(**written) == profile
            cfg = Config(**new)
            cfg.validate_groups()
        except (ValidationError, ValueError, TypeError):
            ok = False
    if not ok:
        raise ProfileEditError("config.yaml could not be changed safely; nothing was written", 500)
    return cfg


def _read(path: Path) -> Tuple[bytes, str]:
    raw = path.read_bytes() if path.exists() else b""
    try:
        return raw, raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ProfileEditError("config.yaml is not UTF-8; edit it by hand", 409)


def _write(path: Path, raw_before: bytes, text: str) -> None:
    bom = b"\xef\xbb\xbf" if raw_before.startswith(b"\xef\xbb\xbf") else b""
    data = bom + text.encode("utf-8")
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "wb") as fh:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
    # Someone saved the file by hand while we were working: keep their version.
    current = path.read_bytes() if path.exists() else b""
    if current != raw_before:
        tmp.unlink(missing_ok=True)
        raise ProfileEditError("config.yaml changed while saving; please try again", 409)
    if raw_before:
        path.with_name(path.name + ".bak").write_bytes(raw_before)
    os.replace(tmp, path)


def _apply(path: Path, profile_id: str, profile: DesktopProfileConfig, create: bool) -> Config:
    if not PROFILE_ID_RE.match(profile_id or ""):
        raise ProfileEditError("invalid profile id (letters, digits, - and _; max 64)")
    validate_profile(profile)
    raw, text = _read(path)
    nl = "\r\n" if "\r\n" in text else "\n"
    try:
        old = _load(text)
    except yaml.YAMLError:
        raise ProfileEditError("config.yaml cannot be parsed; fix it by hand first", 409)
    existing = _profiles_of(old)
    values = _profile_values(profile)
    if create:
        if profile_id in existing:
            raise ProfileEditError(f"desktop profile already exists: {profile_id!r}", 409)
        new_text = _add_text(text, profile_id, values, nl)
    else:
        if not isinstance(existing.get(profile_id), dict):
            raise ProfileEditError("unknown desktop profile", 404)
        new_text = _update_text(text, profile_id, values, existing[profile_id], nl)
    try:
        cfg = _verify(old, _load(new_text), profile_id, profile)
    except yaml.YAMLError:
        raise ProfileEditError("config.yaml could not be changed safely; nothing was written", 500)
    if new_text != text:
        _write(path, raw, new_text)
    return cfg


def add_profile(path: Path, profile_id: str, profile: DesktopProfileConfig) -> Config:
    """Append a new profile to config.yaml. Returns the newly loaded Config."""
    return _apply(path, profile_id, profile, create=True)


def update_profile(path: Path, profile_id: str, profile: DesktopProfileConfig) -> Config:
    """Change an existing profile in config.yaml in place."""
    return _apply(path, profile_id, profile, create=False)
