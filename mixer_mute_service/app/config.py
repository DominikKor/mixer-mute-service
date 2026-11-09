"""Configuration loading. Reads config.yaml if present, otherwise uses the
hardcoded first-milestone defaults from the briefing."""

from __future__ import annotations

import logging
import os
from ipaddress import IPv4Address
from pathlib import Path
from typing import Any, Dict, List, Literal, Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

log = logging.getLogger("mixer.config")


# Site labels for the input channels, used when config.yaml has no
# ``channel_names`` entry. Only applied to inputs (1..max_channels); a bus shows
# its own name instead (see Config.channel_label).
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
    # Number of input channels (the DigiLive 16 has 16). Bus channel numbers
    # must lie above this, so inputs and buses can never share a number.
    max_channels: int = Field(default=16, ge=1, le=64)
    # Optional explicit display labels keyed by API channel number.
    channel_names: Dict[int, str] = Field(default_factory=dict)
    buses: List[BusConfig] = Field(default_factory=list)
    # "direct" (default, recommended): the service opens its own connection next
    #           to the app; its subscribe/keepalive makes the mixer send a full
    #           state dump every keepalive_seconds plus live status frames.
    # "proxy":  the app connects to this host (listen_host:listen_port) and the
    #           service relays to the mixer. Caveat: the mixer does not echo
    #           injected commands, so the app never learns about them and its
    #           next touch may re-send its stale mute flag.
    mode: Literal["direct", "proxy"] = "direct"
    # Address the iPad app should connect to in proxy mode.
    listen_host: str = "0.0.0.0"
    listen_port: int = 6400
    # direct mode: resend the subscribe handshake every N seconds as a keepalive
    # so the mixer keeps streaming (0 disables periodic resend). The mixer
    # answers each one with a dump, so with a keepalive the link counts as dead
    # (and is reconnected) after LINK_TIMEOUT_FACTOR x this without any data.
    keepalive_seconds: float = 10.0

    @model_validator(mode="after")
    def _check_buses(self) -> "MixerConfig":
        channels, ids = set(), set()
        for b in self.buses:
            if b.channel <= self.max_channels:
                raise ValueError(
                    f"bus {b.name!r} uses channel {b.channel}, which is an input "
                    f"channel (1..{self.max_channels}); bus channels must be higher"
                )
            if b.channel in channels:
                raise ValueError(f"bus channel {b.channel} is used twice")
            if b.id.lower() in ids:
                raise ValueError(f"bus id {b.id!r} is used twice")
            channels.add(b.channel)
            ids.add(b.id.lower())
        return self


class SafetyConfig(BaseModel):
    # How a stored fader value is judged trustworthy before sending:
    #   "latest"  - the most recent value observed on the current mixer link
    #               (default). Values from before a reconnect are never used.
    #   "session" - identical to "latest" (an older name, still accepted).
    #   "age"     - additionally require values newer than max_fader_age_seconds
    #               (1 s if unset).
    freshness_mode: Literal["latest", "session", "age"] = "latest"
    # Optional hard age cap (used by "age" mode, or as an extra guard otherwise).
    # null or 0 = no cap. In direct mode the dump refreshes values only every
    # keepalive_seconds, so a smaller cap blocks most sends between dumps.
    max_fader_age_seconds: Optional[float] = Field(default=None, ge=0)
    max_mute_age_seconds: Optional[float] = Field(default=None, ge=0)
    allow_partial_groups: bool = False
    # Exclusive unmute: unmuting a group also mutes every other channel (buses
    # included). Muting a group never unmutes others. Channels without a safe
    # fader value are skipped, not forced.
    exclusive_group_unmute: bool = True
    # Delay between per-channel sends inside a group action.
    group_send_delay_ms: int = Field(default=50, ge=0, le=1000)
    # There is deliberately no unsafe debug send path. A leftover
    # ``allow_unsafe_debug_send`` key in an old config.yaml is ignored.


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
    # Host names (with dots) the page may be opened under besides IP
    # addresses and local names; see app/request_guard.py.
    allowed_hosts: List[str] = Field(default_factory=list)


class DesktopProfileConfig(BaseModel):
    title: str
    kind: Literal["image", "program", "file", "audio"]
    path: str
    target_ip: str = "192.168.188.11"
    args: List[str] = Field(default_factory=list)
    fullscreen: bool = True
    monitor: int = Field(default=1, ge=1)
    position: Literal["top-left", "top-right", "bottom-left", "bottom-right", "center"] = "top-left"
    # Audio only: fade in from silence to fade_db when playing and back out
    # on stop, over fade_seconds (mpv volume, the mixer is not touched).
    fade: bool = False
    fade_db: float = Field(default=-30.0, ge=-60.0, le=0.0)
    fade_seconds: float = Field(default=5.0, ge=0.5, le=30.0)

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

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_keys(cls, data: Any) -> Any:
        # The Companion HTTP forwarding was removed; old config files may still
        # carry its block. Unknown keys are ignored anyway, this only says so.
        if isinstance(data, dict) and "companion" in data:
            data = {k: v for k, v in data.items() if k != "companion"}
            log.warning("config.yaml: the 'companion' section is no longer used and is ignored")
        return data

    def valid_channels(self) -> set:
        """All addressable channel numbers: inputs 1..max plus configured buses."""
        return set(range(1, self.mixer.max_channels + 1)) | {b.channel for b in self.mixer.buses}

    def all_channels(self) -> list:
        return sorted(self.valid_channels())

    def channel_label(self, channel: int) -> str:
        name = self.mixer.channel_names.get(channel)
        if isinstance(name, str) and name.strip():
            return name.strip()
        if channel <= self.mixer.max_channels and channel in DEFAULT_CHANNEL_NAMES:
            return DEFAULT_CHANNEL_NAMES[channel]
        for b in self.mixer.buses:
            if b.channel == channel:
                return b.name
        return f"Channel {channel}"

    def validate_groups(self) -> None:
        """Check the seed groups. A channel above the inputs may be a bus that
        is only discovered from the mixer later (buses.json), so it is accepted
        here; the group store and the safety layer check it when it is used."""
        valid = self.valid_channels()
        for name, group in self.groups.items():
            for ch in group.channels:
                if ch < 1:
                    raise ValueError(f"group {name!r} references invalid channel {ch}")
                if ch not in valid:
                    log.info("seed group %r uses channel %s, not known yet (a bus?)", name, ch)

    def warn_about_settings(self) -> None:
        """Log settings that are valid but very likely not what was meant."""
        caps = [c for c in (self.safety.max_fader_age_seconds, self.safety.max_mute_age_seconds) if c]
        if self.safety.freshness_mode == "age" and not caps:
            caps = [1.0]
        keepalive = self.mixer.keepalive_seconds
        if self.mixer.mode == "direct" and keepalive and caps and min(caps) < keepalive:
            log.warning(
                "safety age cap %.1fs is shorter than the dump interval (%.1fs): most sends "
                "will be blocked between two dumps", min(caps), keepalive,
            )


def default_config_path() -> Path:
    """config.yaml next to the package, or overridden via MIXER_CONFIG."""
    env = os.environ.get("MIXER_CONFIG")
    if env:
        return Path(env)
    return Path(__file__).resolve().parent.parent / "config.yaml"


def load_config(path: Optional[Path] = None) -> Config:
    path = path or default_config_path()
    if path and path.exists():
        data = yaml.safe_load(path.read_text(encoding="utf-8-sig")) or {}
        cfg = Config(**data)
    else:
        cfg = Config()
    cfg.validate_groups()
    cfg.warn_about_settings()
    return cfg
