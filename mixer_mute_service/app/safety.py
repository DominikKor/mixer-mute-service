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


def _session_and_age(
    what: str,
    updated_at: float,
    cap: Optional[float],
    cfg: Config,
    session_epoch: Optional[float],
) -> Optional[SafetyResult]:
    """Shared freshness rule for fader and mute values (None = fresh enough).

    In every mode a value must have been observed on the *current* mixer link
    (``session_epoch`` = when it began): after a reconnect, values from before
    the outage are never trusted, whatever the mode. "age" mode additionally
    requires the value to be younger than the cap (1 s if none is set); in the
    other modes a configured cap still applies as an extra guard.
    """
    age = time.monotonic() - updated_at
    if session_epoch is None:
        return SafetyResult(False, "no live mixer session", age_seconds=age)
    if updated_at < session_epoch:
        return SafetyResult(False, f"{what} predates current session", age_seconds=age)
    if cfg.safety.freshness_mode == "age" and not cap:
        cap = 1.0
    if cap and age > cap:
        return SafetyResult(False, f"{what} stale", age_seconds=age)
    return None


def check_channel_send(
    channel: int,
    cs: Optional[ChannelState],
    cfg: Config,
    *,
    session_epoch: Optional[float] = None,
    require_mute_state: bool = False,
) -> SafetyResult:
    """Per-channel safety for a mute/unmute (and, with require_mute_state, toggle
    or a fader move, which re-send the observed mute flag).

    Global gates (service enabled, mixer connected) are checked by the caller so
    they can produce a single top-level reason instead of repeating per channel.
    Presence and validity of an observed value are required in all modes -- a
    default/dummy fader value is never substituted. Freshness: see
    _session_and_age.
    """
    if channel not in cfg.valid_channels():
        return SafetyResult(False, "invalid channel")

    if cs is None or cs.fader_hex is None or cs.fader_updated_at is None:
        return SafetyResult(False, "no fader value known")

    if not is_safe_fader_hex(cs.fader_hex):
        # Never trust a malformed value (bad hex, NaN, out of 0..1000);
        # never substitute a default.
        return SafetyResult(False, "invalid fader value")

    if cs.solo is None:
        # Every command also writes the solo flag; without the observed value
        # a send could switch the console's solo (headphone monitoring) off.
        return SafetyResult(False, "solo state unknown")

    blocked = _session_and_age(
        "fader value", cs.fader_updated_at, cfg.safety.max_fader_age_seconds,
        cfg, session_epoch,
    )
    if blocked is not None:
        return blocked

    if require_mute_state:
        mute_safety = check_channel_mute_state(
            channel, cs, cfg, session_epoch=session_epoch
        )
        if not mute_safety.ok:
            return mute_safety

    return SafetyResult(True, age_seconds=time.monotonic() - cs.fader_updated_at)


def check_channel_mute_state(
    channel: int,
    cs: Optional[ChannelState],
    cfg: Config,
    *,
    session_epoch: Optional[float] = None,
) -> SafetyResult:
    """Check that an observed mute state is valid and current."""
    if channel not in cfg.valid_channels():
        return SafetyResult(False, "invalid channel")
    if cs is None or cs.muted is None or cs.muted_updated_at is None:
        return SafetyResult(False, "mute state unknown")
    blocked = _session_and_age(
        "mute state", cs.muted_updated_at, cfg.safety.max_mute_age_seconds,
        cfg, session_epoch,
    )
    if blocked is not None:
        return blocked
    return SafetyResult(True, age_seconds=time.monotonic() - cs.muted_updated_at)

