"""Async-safe state store.

The listener coroutine and the HTTP handlers run on the same event loop, so a
plain dict would already be consistent; we still guard with an asyncio.Lock as
the briefing requires, both for correctness if a thread executor is ever added
and to make the read-modify-write of a channel atomic.

Timestamps are taken from time.monotonic(): freshness is the safety-critical
quantity, and a monotonic clock cannot jump backwards if the wall clock is
adjusted (NTP, DST), which would otherwise make a stale value look fresh.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, replace
from typing import Dict, Optional

from .protocol import FrameEvent

# After we send a command to a channel, dump values for that channel are ignored
# this long. The mixer answers a keepalive handshake with a full dump ~0.1 s
# later; if our command goes out between the handshake and that dump, the dump
# still holds the old values and would otherwise overwrite what we just set for
# up to 10 s -- and the next mute would then re-send the OLD fader value.
# Live status frames are never ignored; the next dump is accepted again.
DUMP_GUARD_SECONDS = 1.0


@dataclass
class ChannelState:
    fader_hex: Optional[str] = None
    fader_updated_at: Optional[float] = None
    muted: Optional[bool] = None
    muted_updated_at: Optional[float] = None
    last_seen_raw: Optional[str] = None


class StateStore:
    def __init__(self, dump_guard_seconds: float = DUMP_GUARD_SECONDS) -> None:
        self._channels: Dict[int, ChannelState] = {}
        self._lock = asyncio.Lock()
        self._dump_guard = dump_guard_seconds
        # Monotonic time we last commanded each channel (see DUMP_GUARD_SECONDS).
        self._commanded_at: Dict[int, float] = {}
        # Monotonic timestamp of the last successfully parsed frame (any channel).
        self.last_update_at: Optional[float] = None

    async def apply_event(self, event: FrameEvent) -> bool:
        """Apply an observed frame. Returns False if it was ignored as a dump
        that may predate a command we just sent to this channel."""
        now = time.monotonic()
        async with self._lock:
            commanded = self._commanded_at.get(event.channel)
            if event.from_dump and commanded is not None and now - commanded < self._dump_guard:
                return False
            cs = self._channels.get(event.channel) or ChannelState()
            cs.fader_hex = event.fader_hex
            cs.fader_updated_at = now
            cs.muted = event.muted
            cs.muted_updated_at = now
            cs.last_seen_raw = event.raw_match
            self._channels[event.channel] = cs
            self.last_update_at = now
            return True

    async def set_muted(self, channel: int, muted: bool) -> None:
        """Optimistically record a mute state we just commanded.

        The mixer does not echo our injected commands back to us (they look like
        the iPad's own), so without this the store would stay stale and toggle
        would never see the channel as muted. A later real status frame (a
        physical change or fader move) overrides this if reality differs.
        """
        now = time.monotonic()
        async with self._lock:
            cs = self._channels.get(channel) or ChannelState()
            cs.muted = muted
            cs.muted_updated_at = now
            self._channels[channel] = cs
            self._commanded_at[channel] = now
            self.last_update_at = now

    async def set_fader(self, channel: int, fader_hex: str) -> None:
        """Optimistically record a fader value we just commanded (the mixer does
        not echo our own commands back, same as for set_muted)."""
        now = time.monotonic()
        async with self._lock:
            cs = self._channels.get(channel) or ChannelState()
            cs.fader_hex = fader_hex
            cs.fader_updated_at = now
            self._channels[channel] = cs
            self._commanded_at[channel] = now
            self.last_update_at = now

    async def get_channel(self, channel: int) -> Optional[ChannelState]:
        async with self._lock:
            cs = self._channels.get(channel)
            return replace(cs) if cs is not None else None

    async def snapshot(self) -> Dict[int, ChannelState]:
        async with self._lock:
            return {ch: replace(cs) for ch, cs in self._channels.items()}

    async def last_update_age(self) -> Optional[float]:
        async with self._lock:
            if self.last_update_at is None:
                return None
            return time.monotonic() - self.last_update_at
