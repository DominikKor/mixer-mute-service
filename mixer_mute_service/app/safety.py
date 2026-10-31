"""Safety checks. The whole point of the service: refuse to send unless we are
certain the fader value we would write is fresh and mixer-observed."""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from .config import Config
from .protocol import is_safe_fader_hex
from .state import ChannelState


@dataclass
class SafetyResult:
    ok: bool
    reason: Optional[str] = None
    age_seconds: Optional[float] = None

    def as_detail(self, channel: int) -> dict:
        d = {"channel": channel, "reason": self.reason}
        if self.age_seconds is not None:
            d["age_seconds"] = round(self.age_seconds, 3)
        return d


def check_channel_send(
    channel: int,
    cs: Optional[ChannelState],
    cfg: Config,
    *,
    session_epoch: Optional[float] = None,
    require_mute_state: bool = False,
) -> SafetyResult:
    """Per-channel safety for a mute/unmute (and, with require_mute_state, toggle).

    Global gates (service enabled, mixer connected) are checked by the caller so
    they can produce a single top-level reason instead of repeating per channel.

    Freshness is governed by ``cfg.safety.freshness_mode`` (see SafetyConfig):
    "latest" trusts the most recent observed value (the proxy relays every
    change), "session" trusts only values seen during the current link session
    (``session_epoch`` = when it began), and "age" requires the value to be
    newer than ``max_fader_age_seconds``. A non-null age cap always applies as an
    extra guard. Presence and validity of an observed value are required in all
    modes -- a default/dummy fader value is never substituted.
    """
    now = time.monotonic()
    mode = cfg.safety.freshness_mode

    if channel not in cfg.valid_channels():
        return SafetyResult(False, "invalid channel")

    if cs is None or cs.fader_hex is None or cs.fader_updated_at is None:
        return SafetyResult(False, "no fader value known")

    if not is_safe_fader_hex(cs.fader_hex):
        # Never trust a malformed value (bad hex, NaN, out of 0..1000);
        # never substitute a default.
        return SafetyResult(False, "invalid fader value")

    fader_age = now - cs.fader_updated_at

    if mode == "session":
        if session_epoch is None:
            return SafetyResult(False, "no live mixer session", age_seconds=fader_age)
        if cs.fader_updated_at < session_epoch:
            return SafetyResult(False, "fader value predates current session", age_seconds=fader_age)

    cap = cfg.safety.max_fader_age_seconds
    if mode == "age" and not cap:
        cap = 1.0  # "age" mode without an explicit cap defaults to 1s
    if cap and fader_age > cap:
        return SafetyResult(False, "fader value stale", age_seconds=fader_age)

    if require_mute_state:
        mute_safety = check_channel_mute_state(
            channel, cs, cfg, session_epoch=session_epoch
        )
        if not mute_safety.ok:
            return mute_safety

    return SafetyResult(True, age_seconds=fader_age)


def check_channel_mute_state(
    channel: int,
    cs: Optional[ChannelState],
    cfg: Config,
    *,
    session_epoch: Optional[float] = None,
) -> SafetyResult:
    """Check that an observed mute state is valid and current."""
    now = time.monotonic()
    mode = cfg.safety.freshness_mode
    if channel not in cfg.valid_channels():
        return SafetyResult(False, "invalid channel")
    if cs is None or cs.muted is None or cs.muted_updated_at is None:
        return SafetyResult(False, "mute state unknown")

    mute_age = now - cs.muted_updated_at
    if mode == "session":
        if session_epoch is None:
            return SafetyResult(False, "no live mixer session", age_seconds=mute_age)
        if cs.muted_updated_at < session_epoch:
            return SafetyResult(False, "mute state predates current session", age_seconds=mute_age)

    mute_cap = cfg.safety.max_mute_age_seconds
    if mode == "age" and not mute_cap:
        mute_cap = 1.0
    if mute_cap and mute_age > mute_cap:
        return SafetyResult(False, "mute state stale", age_seconds=mute_age)
    return SafetyResult(True, age_seconds=mute_age)


def is_channel_safe(
    channel: int, cs: Optional[ChannelState], cfg: Config, session_epoch: Optional[float] = None
) -> bool:
    return check_channel_send(channel, cs, cfg, session_epoch=session_epoch).ok
