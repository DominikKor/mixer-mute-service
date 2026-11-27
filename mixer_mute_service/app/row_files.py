"""The Desktop steps of a program row: what it shows and plays, in order.

A row's "desktop" list is a sequence of steps, run strictly in this order
(app/api/program.py _run_content):

    "<profile id>"             a Desktop key (desktop profile) as configured
    "via:<profile id>|<path>"  that key with another file: the key's PC,
                               monitor(s), full screen, position and fade, the
                               step's file (its kind comes from the path)
    "@freeze" / "@unfreeze"    Beamer einfrieren / auftauen (no-op if it
                               already is; never sent while the state is unknown)
    "@song"                    the row's song from the library (Liedtext), at
                               most once; skipped while the row has no song
    "file:<ip>|<path>"         older: a file on a PC chosen in the row, for
                               which no Desktop key on that PC exists yet
                               ("Desktop-Taste wählen"); runs as before
    "key:<button id>"          older: a Steuerung key from Playback /
                               Übersetzung / Liedtext; runs as before

Strings keep the list readable in the stored files, the templates and the
program file; "|" cannot occur in a Windows path and profile ids are
[A-Za-z0-9_-], so a "via:" step splits unambiguously.

The agent knows a "via:" start as "<profile id>--<hash of the path>": it
keeps windows and tracks by that id (keep_if_open, keep_if_playing,
app/profile_stops.py), so the same key with another file is another start.

Rows from before had the separate fields Playback, Übersetzung and Liedtext
(a file "file:<ip>|<path>", a key, or "desktop:<profile>"). fold_legacy()
moves them into the steps, in the order they ran (Playback, Übersetzung,
Liedtext, then the row's own steps), a file to a Desktop key on the same PC
(best kind and monitor; 127.0.0.1 is the streaming PC), and adds "@song" at
the end of a row with a song (or a template's song slot). Stored rows,
templates and shortcuts are migrated at every start (only what is still to
move changes, so it is idempotent); rows from the API and the program file
when they come in.
"""

from __future__ import annotations

import hashlib
import logging
import re
from typing import Iterable, Optional, Tuple

from .file_actions import MAX_PATH, THIS_PC, is_file_action, parse_file_action, runs_from_network_share
from .profile_kind import detect_kind

log = logging.getLogger("mixer.row_files")

VIA_PREFIX = "via:"
_PROFILE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_ABSOLUTE_RE = re.compile(r"^(?:[A-Za-z]:\\|\\\\[^\\]+\\[^\\]+|/)")
FIELDS = ("playback", "translation", "lyrics")
STEP_FREEZE, STEP_UNFREEZE, STEP_SONG = "@freeze", "@unfreeze", "@song"
PSEUDO_STEPS = (STEP_FREEZE, STEP_UNFREEZE, STEP_SONG)
KEY_PREFIX = "key:"
DESKTOP_PREFIX = "desktop:"
MAX_STEPS = 16


def is_via(value) -> bool:
    return isinstance(value, str) and value.startswith(VIA_PREFIX)


def parse_via(value: str) -> Tuple[str, str]:
    """(profile id, path) of a "via:<profile id>|<path>" value, or ValueError."""
    profile_id, sep, path = value[len(VIA_PREFIX):].partition("|")
    if not sep or not _PROFILE_ID_RE.match(profile_id):
        raise ValueError("needs a Desktop key and a path")
    if not path or len(path) > MAX_PATH or not _ABSOLUTE_RE.match(path) or any(ord(c) < 32 for c in path):
        raise ValueError("file path must be a full path on the target PC")
    if runs_from_network_share(path, may_be_program=True):
        raise ValueError("programs must be on the target PC, not on a network share")
    return profile_id, path


def via_error(value: str) -> Optional[str]:
    try:
        parse_via(value)
    except ValueError as exc:
        return str(exc)
    return None


def via_value(profile_id: str, path: str) -> str:
    return f"{VIA_PREFIX}{profile_id}|{path}"


def path_kind(path: str) -> str:
    """image, audio, program (.exe) or file (opened with its Windows app):
    the Desktop editor's detection (app/profile_kind.py); a name without an
    extension is a file."""
    return detect_kind(path) or "file"


def start_id(profile_id: str, path: str) -> str:
    """The id the agent knows this start by: the key and the file."""
    digest = hashlib.sha1(path.casefold().encode("utf-8")).hexdigest()[:10]
    return f"{profile_id}--{digest}"


def profile_of_start(start: str) -> str:
    """The Desktop key of a start id (a plain profile id stays as it is)."""
    return start.split("--", 1)[0]


def step_error(step) -> Optional[str]:
    """Why a Desktop step is not valid, or None."""
    if not isinstance(step, str) or not step:
        return "a step must be a text"
    if step in PSEUDO_STEPS or _PROFILE_ID_RE.match(step):
        return None
    if is_via(step):
        return via_error(step)
    if is_file_action(step):
        try:
            parse_file_action(step)
        except ValueError as exc:
            return str(exc)
        return None
    if step.startswith(KEY_PREFIX) and 0 < len(step) - len(KEY_PREFIX) <= 80:
        return None
    return "unknown step"


def step_profile(step: str) -> Optional[str]:
    """The Desktop key a step runs through ("<id>" or "via:<id>|..."), else None."""
    if _PROFILE_ID_RE.match(step) and step not in PSEUDO_STEPS:
        return step
    if is_via(step):
        try:
            return parse_via(step)[0]
        except ValueError:
            return None
    return None


def step_start_id(step: str) -> Optional[str]:
    """The id the agent knows the step's start by (profile or key+file)."""
    if is_via(step):
        try:
            return start_id(*parse_via(step))
        except ValueError:
            return None
    return step_profile(step)


# -- Playback / Übersetzung / Liedtext into the steps -------------------------------


def _same_pc(a: str, b: str, this_pc: Iterable[str]) -> bool:
    """The same PC: equal, or both the streaming PC (127.0.0.1 and its LAN address)."""
    here = set(this_pc) | {THIS_PC}
    return a == b or (a in here and b in here)


def pick_profile(field: str, target: str, path: str, profiles: dict, this_pc: Iterable[str] = ()) -> Optional[str]:
    """The Desktop key for a file on that PC: one on the same PC, best the
    same kind; music best a key without fade (a playback starts at once);
    Übersetzung (shown on the beamer) prefers monitor 2, others monitor 1.
    Ties: the first in the configuration. None if the PC has none."""
    this_pc = tuple(this_pc)
    kind = path_kind(path)
    wanted_monitor = 2 if field == "translation" else 1
    best, best_score = None, None
    for index, (profile_id, profile) in enumerate(profiles.items()):
        if not _same_pc(profile.target_ip, target, this_pc):
            continue
        same_kind = profile.kind == kind or (kind == "file" and profile.kind in ("file", "image"))
        score = (2 if profile.kind == kind else 1 if same_kind else 0,
                 1 if kind == "audio" and not getattr(profile, "fade", False) else 0,
                 1 if kind != "audio" and profile.monitor == wanted_monitor else 0,
                 -index)
        if best_score is None or score > best_score:
            best, best_score = profile_id, score
    return best


def migrate_value(field: str, value, profiles: dict, this_pc: Iterable[str] = ()):
    """A "file:<ip>|<path>" value as "via:<key>|<path>" when its PC has a
    Desktop key; anything else (also a file whose PC has none) unchanged."""
    if not is_file_action(value):
        return value
    try:
        target, path = parse_file_action(value)
    except ValueError:
        return value
    profile_id = pick_profile(field, target, path, profiles, this_pc)
    return via_value(profile_id, path) if profile_id else value


def _field_step(field: str, value, profiles: dict, this_pc) -> Optional[str]:
    """The step an old Playback / Übersetzung / Liedtext value becomes."""
    if not isinstance(value, str) or not value:
        return None
    if is_file_action(value):
        return migrate_value(field, value, profiles, this_pc)
    if is_via(value):
        return value
    if value.startswith(DESKTOP_PREFIX):
        return value.removeprefix(DESKTOP_PREFIX) or None
    return KEY_PREFIX + value


def fold_legacy(row: dict, profiles: dict, this_pc: Iterable[str] = (), song_slot: bool = False) -> list:
    """Move a row's (template row's, shortcut's) Playback / Übersetzung /
    Liedtext into its Desktop steps and add "@song" for a row with a song;
    in place. Returns what changed as text (for the log), [] if nothing."""
    this_pc = tuple(this_pc)
    steps = list(row.get("desktop") or [])
    notes, front = [], []
    for field in FIELDS:
        value = row.get(field)
        if not value:
            if field in row and row[field] is not None:
                row[field] = None
            continue
        step = _field_step(field, value, profiles, this_pc)
        row[field] = None
        if step is None:
            continue
        if step_profile(step) == step and (step in steps or step in front):
            notes.append(f"{field} {value!r}: already a step")
            continue
        front.append(step)
        profile = profiles.get(step_profile(step) or "")
        fades = (profile is not None and getattr(profile, "fade", False)
                 and path_kind(step.split("|", 1)[-1]) == "audio" and is_via(step))
        notes.append(f"{field} {value!r} -> step {step!r}"
                     + (" (Desktop-Taste wählen)" if is_file_action(step) else "")
                     + (" (no Desktop key without fade on that PC: it fades in like that key)" if fades else ""))
    if front:
        steps = front + steps
    if (row.get("song") or song_slot) and STEP_SONG not in steps:
        steps.append(STEP_SONG)
        notes.append("song -> last step")
    if steps != list(row.get("desktop") or []):
        row["desktop"] = steps
    return notes


async def migrate_stores(state) -> dict:
    """Fold the old fields of the program rows, the templates and the
    shortcuts into their Desktop steps (at service start). Returns
    {store: rows changed, "legacy": files still without a Desktop key}."""
    profiles = state.cfg.desktop_display.profiles
    this_pc = await streaming_pc_ips(state)
    summary = {"program": 0, "templates": 0, "shortcuts": 0, "legacy": 0}
    for name, attr in (("program", "program_items"), ("templates", "program_templates"), ("shortcuts", "shortcuts")):
        store = getattr(state, attr, None)
        if store is None:
            continue

        def change(row, where, song_slot=False, _name=name):
            notes = fold_legacy(row, profiles, this_pc, song_slot=song_slot)
            for note in notes:
                log.info("Desktop steps %s %s: %s", _name, where, note)
            for step in row.get("desktop") or []:
                if is_file_action(step):
                    summary["legacy"] += 1
                    if notes:
                        log.warning("Desktop steps %s %s: no Desktop key on the PC of %s; it keeps running as "
                                    "before (choose a Desktop key in the row)", _name, where, step)
            return bool(notes)

        summary[name] = await store.map_rows(change)
    if any(summary.values()):
        log.info("Desktop steps migration: %s", summary)
    return summary


async def streaming_pc_ips(state) -> list:
    """The streaming PC's LAN address(es), for 127.0.0.1 in old file values."""
    from .target_pcs import DEFAULT_PCS  # the service's own PC by default
    default = [pc["ip"] for pc in DEFAULT_PCS if pc["id"] == "streaming"]
    pcs = getattr(state, "target_pcs", None)
    if pcs is None:
        return default
    try:
        return [pc["ip"] for pc in await pcs.get() if pc.get("id") == "streaming"] or default
    except Exception:  # noqa: BLE001 - only a hint for 127.0.0.1
        log.debug("Target PCs unavailable for the Desktop steps migration", exc_info=True)
        return default
