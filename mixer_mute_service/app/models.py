"""Pydantic request bodies. Route handlers return plain dicts (the safety layer
composes them); the response shapes are documented in the README and /docs."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field, StrictBool, StrictInt


class GroupCreate(BaseModel):
    name: str
    channels: List[int]


class GroupUpdate(BaseModel):
    channels: List[int]


class GroupRename(BaseModel):
    name: str


class GroupCaptureRequest(BaseModel):
    name: str


class FaderRequest(BaseModel):
    # fader position 0..1000 (0 = -inf, 750 ~ 0 dB, 1000 = top)
    value: float


class RecallFaderRequest(BaseModel):
    fader_hex: str


class AlwaysOnRequest(BaseModel):
    enabled: bool


class BeamerStateRequest(BaseModel):
    """The state the service takes the beamer to be in (nothing is sent)."""
    hidden: Optional[bool] = None
    frozen: Optional[bool] = None


class StagePreferenceRequest(BaseModel):
    on: bool


class CameraZoomRequest(BaseModel):
    # Hold-to-zoom: "in"/"out" start zooming, "stop" ends it.
    direction: Literal["in", "out", "stop"]


class CameraPanTiltRequest(BaseModel):
    # Hold-to-move: a direction starts (or keeps alive) a move, "stop" ends it.
    direction: Literal["left", "right", "up", "down", "stop"]


class PowerOffConfigRequest(BaseModel):
    target_ips: List[str] = Field(default_factory=list)
    beamer: bool = False


class CameraPresetRecallRequest(BaseModel):
    # Any VISCA preset number the service uses, named in the app or not.
    preset: StrictInt = Field(ge=1, le=16)


class CameraPresetSaveRequest(BaseModel):
    # The camera's current position becomes this preset number, with a name.
    preset: StrictInt = Field(ge=1, le=16)
    name: str = Field(max_length=58)
    # A number the app already names is only overwritten when asked for.
    overwrite: StrictBool = False


class CameraStreamRequest(BaseModel):
    name: str = Field(max_length=64)
    rtsp_url: str = Field(max_length=1024)
    username: str = Field(default="", max_length=128)
    password: str = Field(default="", max_length=256)


class CompanionCategoryCreate(BaseModel):
    name: str


class CompanionCategoryVisibility(BaseModel):
    visible: bool


class CompanionButtonCreate(BaseModel):
    label: str
    category_id: str
    # "companion" = a user key pointing at a native catalog action
    kind: Literal["companion", "channel", "group"]
    target_button_id: Optional[str] = None
    channel: Optional[int] = None
    group: Optional[str] = None
    operation: Optional[Literal["mute", "unmute", "toggle"]] = None


class CompanionButtonUpdate(BaseModel):
    label: str
    category_id: str


class ProgramActionFields(BaseModel):
    """What a program row or a shortcut does when it starts."""
    title: str
    group: Optional[str] = ""  # empty: the row changes no mutes
    playback: Optional[str] = None
    translation: Optional[str] = None
    lyrics: Optional[str] = None
    playback_description: str = Field(default="", max_length=160)
    translation_description: str = Field(default="", max_length=160)
    lyrics_description: str = Field(default="", max_length=160)
    song: Optional[str] = None  # FoilPresenter song (file name), shown when the row starts
    # Camera presets, moved only while that camera is off air (app/camera_program.py)
    cam1_preset: Optional[int] = None
    cam2_preset: Optional[int] = None
    camera_wait_seconds: Optional[float] = None
    light: Optional[str] = None  # light scene id (app/lights.py), applied when the row starts
    desktop: List[str] = Field(default_factory=list)  # desktop profile ids, run when the row starts
    # OBS scene per camera, switched to when that camera moved (app/camera_program.py)
    cam1_scene: Optional[str] = None
    cam2_scene: Optional[str] = None
    beamer: Optional[Literal["hide", "unhide", ""]] = None  # empty: the beamer stays as it is


class ProgramItemRequest(ProgramActionFields):
    time: str
    person: str = ""
    # "shortcut" / "shortcuts" from pages of v1.2.1-210 and before are
    # ignored: a row always runs its own fields ("Shortcut laden" copies a
    # shortcut's fields into them).


class ShortcutRequest(ProgramActionFields):
    pass
