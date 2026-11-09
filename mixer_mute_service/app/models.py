"""Pydantic request bodies. Route handlers return plain dicts (the safety layer
composes them); the response shapes are documented in the README and /docs."""

from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, Field


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
    hidden: bool


class CameraZoomRequest(BaseModel):
    # Hold-to-zoom: "in"/"out" start zooming, "stop" ends it.
    direction: Literal["in", "out", "stop"]


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


class ProgramItemRequest(BaseModel):
    time: str
    title: str
    person: str = ""
    group: Optional[str] = ""  # empty: the row changes no mutes
    playback: Optional[str] = None
    translation: Optional[str] = None
    lyrics: Optional[str] = None
    playback_description: str = Field(default="", max_length=160)
    translation_description: str = Field(default="", max_length=160)
    lyrics_description: str = Field(default="", max_length=160)
    song: Optional[str] = None  # FoilPresenter song (file name), shown when the row starts
