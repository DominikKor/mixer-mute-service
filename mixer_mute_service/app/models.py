"""Pydantic response models for documentation/OpenAPI. The actual route handlers
return plain dicts (the safety layer composes them), so these models are not
enforced as response_model -- they describe the shapes for API consumers."""

from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel


class HealthResponse(BaseModel):
    ok: bool
    enabled: bool
    mixer_connected: bool
    last_update_age_seconds: Optional[float] = None


class ChannelView(BaseModel):
    fader_hex: Optional[str] = None
    # fader position 0..1000 (0 = -inf), decoded from fader_hex
    fader_value: Optional[float] = None
    fader_age_seconds: Optional[float] = None
    muted: Optional[bool] = None
    mute_age_seconds: Optional[float] = None
    safe: bool


class StateResponse(BaseModel):
    enabled: bool
    mixer_connected: bool
    channels: Dict[str, ChannelView]


class SendResult(BaseModel):
    ok: bool
    channel: Optional[int] = None
    target_muted: Optional[bool] = None
    fader_hex: Optional[str] = None
    sent_hex: Optional[str] = None
    reason: Optional[str] = None
    age_seconds: Optional[float] = None


class GroupDetail(BaseModel):
    channel: int
    reason: str
    age_seconds: Optional[float] = None


class GroupCreate(BaseModel):
    name: str
    channels: List[int]


class FaderRequest(BaseModel):
    # fader position 0..1000 (0 = -inf, 750 ~ 0 dB, 1000 = top)
    value: float


class RecallFaderRequest(BaseModel):
    fader_hex: str


class GroupUpdate(BaseModel):
    channels: List[int]


class GroupResult(BaseModel):
    ok: bool
    group: str
    target_muted: Optional[bool] = None
    results: Optional[List[SendResult]] = None
    reason: Optional[str] = None
    details: Optional[List[GroupDetail]] = None
