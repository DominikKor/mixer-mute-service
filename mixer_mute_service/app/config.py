"""Configuration loading. Reads config.yaml if present, otherwise uses the
hardcoded first-milestone defaults from the briefing."""

from __future__ import annotations

import os
from ipaddress import IPv4Address
from pathlib import Path
from typing import Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator


DEFAULT_CHANNEL_NAMES: Dict[int, str] = {
    1: "Pult",
    2: "Gebet",
    3: "Instrument",
    4: "Raum hinten",
    5: "Ear Monitor",
    6: "Headset 1",
    7: "Headset 2",
    8: "Geige",
    9: "Gruen",
    10: "Lila",
    11: "Schwarz",
    12: "Klavier",
    13: "AUX",
    14: "PC2",
    15: "PC1",
    16: "USB",
    17: "ECHO 1",
    18: "Monitor",
}


class BusConfig(BaseModel):
    """A bus/aux that uses the type-72/71 command/status frames and an object id
    varint (e.g. Echo id "8001"=128) instead of positional 08 XX addressing."""

    channel: int  # API/UI channel number (e.g. 17), must not collide with inputs
    name: str     # display label, e.g. "Echo"
    id: str       # object-id varint as hex, e.g. "8001"


class MixerConfig(BaseModel):
    ip: str = "192.168.188.30"
    port: int = 6400
    max_channels: int = 8
    # Optional explicit display labels keyed by API channel number.
    channel_names: Dict[int, str] = Field(default_factory=dict)
    buses: List[BusConfig] = Field(default_factory=list)
    # "proxy": service sits between the iPad and the mixer (point the iPad app at
    #          this host). Guarantees state visibility -- all traffic flows through.
    # "direct": service opens its own connection to the mixer (only useful if the
    #           mixer pushes state to extra clients, which this one does not).
    mode: str = "proxy"
    # Address the iPad app should connect to in proxy mode.
    listen_host: str = "0.0.0.0"
    listen_port: int = 6400
    # direct mode: resend the subscribe handshake every N seconds as a keepalive
    # so the mixer keeps streaming (0 disables periodic resend).
    keepalive_seconds: float = 10.0


class SafetyConfig(BaseModel):
    # How a stored fader value is judged trustworthy before sending:
    #   "latest"  - use the most recent observed value as long as the proxy link
    #               is live (default). Relies on the proxy relaying every change.
    #   "session" - only trust values observed during the current link session.
    #   "age"     - only trust values newer than max_fader_age_seconds.
    freshness_mode: str = "latest"
    # Optional hard age cap (used by "age" mode, or as an extra guard otherwise).
    max_fader_age_seconds: Optional[float] = None
    max_mute_age_seconds: Optional[float] = None
    allow_partial_groups: bool = False
    # Solo behaviour: unmuting a group also mutes every other channel (buses
    # included). Muting a group never unmutes others. Channels without a safe
    # fader value are skipped, not forced.
    exclusive_group_unmute: bool = True
    # Delay between per-channel sends inside a group action.
    group_send_delay_ms: int = 50
    # Debug escape hatch. MUST default to False. When False there is simply no
    # code path that sends raw bytes without a safety check.
    allow_unsafe_debug_send: bool = False


class GroupConfig(BaseModel):
    channels: List[int]

    @field_validator("channels")
    @classmethod
    def _non_empty(cls, v: List[int]) -> List[int]:
        if not v:
            raise ValueError("group must define at least one channel")
        return v


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8765


class DesktopProfileConfig(BaseModel):
    title: str
    kind: Literal["image", "program", "file"]
    path: str
    target_ip: str = "192.168.188.11"
    args: List[str] = Field(default_factory=list)
    fullscreen: bool = True
    monitor: int = Field(default=1, ge=1)

    @field_validator("target_ip")
    @classmethod
    def _valid_target_ip(cls, value: str) -> str:
        IPv4Address(value)
        return value


class DesktopDisplayConfig(BaseModel):
    enabled: bool = False
    token_env: str = "DESKTOP_AGENT_TOKEN"
    profiles: Dict[str, DesktopProfileConfig] = Field(default_factory=dict)


class Config(BaseModel):
    mixer: MixerConfig = Field(default_factory=MixerConfig)
    safety: SafetyConfig = Field(default_factory=SafetyConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    desktop_display: DesktopDisplayConfig = Field(default_factory=DesktopDisplayConfig)
    groups: Dict[str, GroupConfig] = Field(default_factory=dict)

    def valid_channels(self) -> set:
        """All addressable channel numbers: inputs 1..max plus configured buses."""
        return set(range(1, self.mixer.max_channels + 1)) | {b.channel for b in self.mixer.buses}

    def all_channels(self) -> list:
        return sorted(self.valid_channels())

    def channel_label(self, channel: int) -> str:
        name = self.mixer.channel_names.get(channel)
        if isinstance(name, str) and name.strip():
            return name.strip()
        default_name = DEFAULT_CHANNEL_NAMES.get(channel)
        if isinstance(default_name, str) and default_name.strip():
            return default_name.strip()
        for b in self.mixer.buses:
            if b.channel == channel:
                return b.name
        return f"Channel {channel}"

    def validate_groups(self) -> None:
        """Ensure every group references only valid channels (inputs or buses)."""
        valid = self.valid_channels()
        for name, group in self.groups.items():
            for ch in group.channels:
                if ch not in valid:
                    raise ValueError(f"group {name!r} references unknown channel {ch}")


def default_config_path() -> Path:
    """config.yaml next to the package, or overridden via MIXER_CONFIG."""
    env = os.environ.get("MIXER_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: Optional[Path] = None) -> Config:
    path = path or default_config_path()
    if path and path.exists():
        data = yaml.safe_load(path.read_text()) or {}
        cfg = Config(**data)
    else:
        cfg = Config()
    cfg.validate_groups()
    return cfg
