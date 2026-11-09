"""Orchestration: tie state + safety + protocol + client together.

Every public method here is the *only* sanctioned way to cause bytes to reach
the mixer. There is deliberately no path that sends without first passing the
safety layer, and no unsafe debug switch.

Single-channel commands run under one command lock: reading the state, the
safety check, building, sending and recording the result are one step, so two
concurrent requests (a Stream Deck mute and a UI fader drag) can never both act
on the same old state and leave the mixer and the store disagreeing.

Group commands additionally run under one group lock (taken before, and never
inside, the command lock): the toggle decision, the group's sends and the
exclusive unmute (muting every other channel) are one step, so two group
buttons pressed at once run one after the other instead of interleaving.

The sending flag (panic) is persisted, so a service restart during a show
keeps sending disabled until someone enables it again.

All results are plain JSON-compatible dicts matching the briefing's examples.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import List, Optional, Tuple

from .channel_preferences import ChannelPreferences
from .config import Config
from .groups import GroupError, GroupStore
from .jsonstore import CorruptJSONError, read_json, write_json_atomic
from .link import MixerLink
from .protocol import (
    build_bus_command,
    build_mute_command,
    fader_hex_to_value,
    fader_value_to_hex,
)
from .safety import SafetyResult, check_channel_mute_state, check_channel_send
from .state import ChannelState, StateStore

log = logging.getLogger("mixer.controller")


class Controller:
    def __init__(
        self, cfg: Config, store: StateStore, client: MixerLink,
        groups: GroupStore, channel_preferences: Optional[ChannelPreferences] = None,
        *, panic_path: Optional[Path] = None,
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._client = client
        self._groups = groups
        self._channel_preferences = channel_preferences
        self._command_lock = asyncio.Lock()
        self._group_lock = asyncio.Lock()
        self._panic_path = panic_path
        self._enabled = self._load_enabled()

    # -- panic (sending on/off) ----------------------------------------------

    def _load_enabled(self) -> bool:
        if self._panic_path is None:
            return True
        try:
            data = read_json(self._panic_path, dict)
        except FileNotFoundError:
            return True
        except CorruptJSONError:
            log.error("panic state %s unreadable: sending stays DISABLED until enabled again",
                      self._panic_path)
            return False
        enabled = data.get("enabled")
        if not isinstance(enabled, bool):
            log.error("panic state %s has no valid 'enabled': sending stays DISABLED until "
                      "enabled again", self._panic_path)
            return False
        if not enabled:
            log.warning("sending is DISABLED (panic was active before the restart)")
        return enabled

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        value = bool(value)
        if value != self._enabled:
            log.warning("PANIC sending %s", "ENABLED" if value else "DISABLED")
        self._enabled = value
        if self._panic_path is not None:
            try:
                write_json_atomic(self._panic_path, {"enabled": value})
            except OSError as exc:
                # The switch itself still takes effect in memory.
                log.error("could not save panic state to %s: %s", self._panic_path, exc)

    async def always_on_channels(self) -> set:
        if self._channel_preferences is None:
            return set()
        return await self._channel_preferences.list_always_on()

    async def set_channel_always_on(self, channel: int, enabled: bool) -> dict:
        if channel not in self._cfg.valid_channels():
            return {"ok": False, "channel": channel, "reason": "invalid channel"}
        if self._channel_preferences is None:
            return {"ok": False, "channel": channel, "reason": "channel preferences unavailable"}
        await self._channel_preferences.set_always_on(channel, enabled)
        return {"ok": True, "channel": channel, "always_on": enabled}

    # -- global gate -------------------------------------------------------

    def _global_block_reason(self) -> Optional[str]:
        if not self._enabled:
            return "service disabled (panic)"
        if not self._client.connected:
            return "no mixer connection"
        if self._client.link_stale():
            return "no data from mixer (link stale)"
        return None

    def is_send_safe(self, channel: int, cs: Optional[ChannelState]) -> bool:
        """Would a mute/unmute of this channel be sent right now? The same
        checks as the send path (panic, connection, stale link, channel
        state), for the UI's "safe" flag."""
        if self._global_block_reason() is not None:
            return False
        return check_channel_send(
            channel, cs, self._cfg, session_epoch=self._client.session_epoch
        ).ok

    # -- single channel ----------------------------------------------------

    def _bus(self, channel: int):
        return next((b for b in self._cfg.mixer.buses if b.channel == channel), None)

    @staticmethod
    def _blocked(channel: int, action: str, reason: str,
                 safety: Optional[SafetyResult] = None) -> dict:
        age = safety.age_seconds if safety is not None else None
        log.warning(
            "BLOCKED channel=%s action=%s reason=%r%s",
            channel, action, reason, "" if age is None else f" age={age:.2f}s",
        )
        result = {"ok": False, "channel": channel, "reason": reason}
        if age is not None:
            result["age_seconds"] = round(age, 3)
        return result

    async def _checked_state(
        self, channel: int, action: str, *, require_mute_state: bool
    ) -> Tuple[Optional[ChannelState], Optional[dict]]:
        """(state, None) if the channel may be sent to, else (None, blocked result)."""
        reason = self._global_block_reason()
        if reason:
            return None, self._blocked(channel, action, reason)
        cs = await self._store.get_channel(channel)
        safety = check_channel_send(
            channel, cs, self._cfg,
            session_epoch=self._client.session_epoch,
            require_mute_state=require_mute_state,
        )
        if not safety.ok:
            return None, self._blocked(channel, action, safety.reason, safety)
        return cs, None

    async def _send(
        self, channel: int, muted: bool, fader_hex: str, action: str, *, solo: bool
    ) -> Tuple[Optional[str], Optional[dict]]:
        """Build and send one command -> (sent_hex, None) or (None, error result).

        ``solo`` is required: every command also writes the console's solo
        flag, so the caller must pass the observed one."""
        try:
            bus = self._bus(channel)
            if bus is not None:
                payload = build_bus_command(bus.id, muted, fader_hex, solo=solo)
            else:
                payload = build_mute_command(
                    channel, muted, fader_hex, self._cfg.mixer.max_channels, solo=solo
                )
        except ValueError as exc:
            return None, self._blocked(channel, action, str(exc))
        try:
            await self._client.send(payload)
        except Exception as exc:  # noqa: BLE001
            log.error("FAILED channel=%s action=%s error=%r", channel, action, str(exc))
            return None, {"ok": False, "channel": channel, "reason": f"send failed: {exc}"}
        return payload.hex(), None

    async def channel_set(self, channel: int, target_muted: bool) -> dict:
        if channel not in self._cfg.valid_channels():
            return self._blocked(channel, _act(target_muted), "invalid channel")
        async with self._command_lock:
            return await self._channel_set_locked(channel, target_muted)

    async def _channel_set_locked(self, channel: int, target_muted: bool) -> dict:
        action = _act(target_muted)
        cs, blocked = await self._checked_state(channel, action, require_mute_state=False)
        if blocked:
            return blocked
        assert cs is not None and cs.fader_hex is not None
        fader_hex = cs.fader_hex
        # The console's solo is sent back as observed, never changed by us
        # (the safety check guarantees it is known).
        solo = bool(cs.solo)
        sent_hex, error = await self._send(channel, target_muted, fader_hex, action, solo=solo)
        if error:
            return error

        # The mixer does not echo our injected command back, so record the new
        # mute state ourselves -- otherwise toggle would never see it change.
        await self._store.set_muted(channel, target_muted)

        log.info(
            "SENT channel=%s action=%s fader=%s solo=%s payload=%s",
            channel, action, fader_hex, solo, sent_hex,
        )
        return {
            "ok": True,
            "channel": channel,
            "target_muted": target_muted,
            "fader_hex": fader_hex,
            "sent_hex": sent_hex,
        }

    async def channel_set_fader(self, channel: int, value: float) -> dict:
        """Move a channel's fader to ``value`` (0..1000, 0 = -inf).

        The mixer has no fader-only command: every command also writes the mute
        flag. So the channel's mute state must be known from an observed frame,
        and it is sent back unchanged -- an unknown mute state blocks the move
        instead of guessing (guessing "unmuted" could open a muted channel).
        """
        if channel not in self._cfg.valid_channels():
            return self._blocked(channel, "fader", "invalid channel")

        try:
            fader_hex = fader_value_to_hex(value)
        except ValueError:
            log.warning("BLOCKED channel=%s action=fader reason='invalid fader value' value=%r", channel, value)
            return {"ok": False, "channel": channel, "reason": "invalid fader value"}

        async with self._command_lock:
            cs, blocked = await self._checked_state(channel, "fader", require_mute_state=True)
            if blocked:
                return blocked
            assert cs is not None and cs.muted is not None
            muted = cs.muted
            solo = bool(cs.solo)
            sent_hex, error = await self._send(channel, muted, fader_hex, "fader", solo=solo)
            if error:
                return error
            await self._store.set_fader(channel, fader_hex)

        log.info(
            "SENT channel=%s action=fader value=%.2f muted=%s solo=%s fader=%s payload=%s",
            channel, fader_hex_to_value(fader_hex), muted, solo, fader_hex, sent_hex,
        )
        return {
            "ok": True,
            "channel": channel,
            "fader_value": fader_hex_to_value(fader_hex),
            "fader_hex": fader_hex,
            "muted": muted,
            "sent_hex": sent_hex,
        }

    async def channel_recall_fader(self, channel: int, fader_hex: str) -> dict:
        """Recall a stored fader_hex (e.g. a saved preset). Same safety as
        channel_set_fader: the value must be a real fader position and the
        current mute state is preserved, never guessed."""
        value = fader_hex_to_value(fader_hex)
        if value is None:
            log.warning("BLOCKED channel=%s action=recall-fader reason='invalid fader_hex' hex=%r",
                        channel, fader_hex)
            return {"ok": False, "channel": channel, "reason": "invalid fader_hex"}
        return await self.channel_set_fader(channel, value)

    async def channel_toggle(self, channel: int) -> dict:
        if channel not in self._cfg.valid_channels():
            return self._blocked(channel, "toggle", "invalid channel")
        async with self._command_lock:
            # Decide and send under the same lock, so the decision cannot be
            # based on a state another request is just changing.
            cs, blocked = await self._checked_state(channel, "toggle", require_mute_state=True)
            if blocked:
                return blocked
            assert cs is not None and cs.muted is not None
            return await self._channel_set_locked(channel, not cs.muted)

    # -- groups ------------------------------------------------------------

    async def _group_channels(self, group: str) -> Optional[List[int]]:
        return await self._groups.get(group)

    async def _evaluate_group(
        self, channels: List[int], *, require_mute_state: bool
    ) -> Tuple[List[dict], dict]:
        """Return (unsafe_details, {channel: ChannelState}) for the group."""
        details: List[dict] = []
        states = {}
        for ch in channels:
            cs = await self._store.get_channel(ch)
            states[ch] = cs
            safety = check_channel_send(
                ch, cs, self._cfg,
                session_epoch=self._client.session_epoch,
                require_mute_state=require_mute_state,
            )
            if not safety.ok:
                details.append(safety.as_detail(ch))
        return details, states

    def _group_blocked(self, group: str, action: str, reason: str, **extra) -> dict:
        log.warning("BLOCKED group=%s action=%s reason=%r%s", group, action, reason,
                    "".join(f" {k}={v}" for k, v in extra.items()))
        return {"ok": False, "group": group, "reason": reason, **extra}

    async def group_set(self, group: str, target_muted: bool) -> dict:
        async with self._group_lock:
            channels = await self._group_channels(group)
            if channels is None:
                return self._group_blocked(group, _act(target_muted), "unknown group")
            reason = self._global_block_reason()
            if reason:
                return self._group_blocked(group, _act(target_muted), reason)
            return await self._group_set_locked(group, channels, target_muted)

    async def _group_set_locked(self, group: str, channels: List[int], target_muted: bool) -> dict:
        always_on = await self.always_on_channels()
        protected = sorted(set(channels) & always_on) if target_muted else []
        action_channels = [ch for ch in channels if ch not in protected]
        if not action_channels:
            return {
                "ok": True, "group": group, "target_muted": target_muted,
                "results": [], "skipped": [], "protected": protected,
            }

        unsafe, _ = await self._evaluate_group(action_channels, require_mute_state=False)
        if unsafe and not self._cfg.safety.allow_partial_groups:
            log.warning("BLOCKED group=%s action=%s reason=unsafe details=%s",
                        group, _act(target_muted), unsafe)
            return {
                "ok": False,
                "group": group,
                "reason": "unsafe group operation",
                "details": unsafe,
            }

        unsafe_channels = {d["channel"] for d in unsafe}
        results = []
        delay = self._cfg.safety.group_send_delay_ms / 1000.0
        sent_any = False
        for ch in action_channels:
            if ch in unsafe_channels:
                # only reachable when allow_partial_groups is explicitly enabled
                continue
            if sent_any:
                await asyncio.sleep(delay)
            results.append(await self.channel_set(ch, target_muted))
            sent_any = True

        # The group is complete only if every member was sent successfully:
        # none skipped as unsafe (partial mode), none failed, and at least one
        # sent (all([]) would be True for a group whose members were all
        # skipped).
        group_complete = (
            bool(results) and not unsafe_channels and all(r.get("ok") for r in results)
        )

        # Exclusive unmute: unmuting a group also mutes every OTHER channel
        # (incl. buses), but muting a group leaves the others alone. Channels we
        # can't safely mute (no current fader) are skipped, not forced. Only
        # after the group itself came up completely: muting everything else
        # around a group that stayed (partly) muted could silence the whole room.
        others = []
        protected_others = []
        others_skipped_reason = None
        if not target_muted and self._cfg.safety.exclusive_group_unmute and group_complete:
            if self._channel_preferences is not None and not self._channel_preferences.readable:
                # Which channels are "always on" is unknown: muting the others
                # could silence a protected channel (e.g. the presenter mic).
                others_skipped_reason = "always-on channels unknown (preferences file unreadable)"
                log.error("group=%s action=unmute: other channels NOT muted: %s",
                          group, others_skipped_reason)
            else:
                members = set(channels)
                other_channels = self._cfg.valid_channels() - members
                protected_others = sorted(other_channels & always_on)
                for ch in sorted(other_channels - always_on):
                    cs = await self._store.get_channel(ch)
                    if cs is not None and cs.muted is True:
                        continue  # already muted, nothing to do
                    await asyncio.sleep(delay)
                    others.append(await self.channel_set(ch, True))

        out = {
            "ok": group_complete,
            "group": group,
            "target_muted": target_muted,
            "results": results,
            "skipped": sorted(unsafe_channels) if unsafe_channels else [],
        }
        if protected:
            out["protected"] = protected
        if protected_others:
            out["protected_others"] = protected_others
        if others_skipped_reason:
            out["others_skipped_reason"] = others_skipped_reason
        if others:
            out["muted_others"] = others
            blocked = [r for r in others if not r.get("ok")]
            if blocked:
                out["others_skipped"] = sorted(r["channel"] for r in blocked)
        return out

    async def group_toggle(self, group: str) -> dict:
        # Decide and act under the group lock, so the decision cannot be based
        # on a state another group command is just changing.
        async with self._group_lock:
            channels = await self._group_channels(group)
            if channels is None:
                return self._group_blocked(group, "toggle", "unknown group")

            reason = self._global_block_reason()
            if reason:
                return self._group_blocked(group, "toggle", reason)

            # Toggle semantics: a group counts as "active" only when ALL channels
            # are muted -> then unmute; otherwise mute. Requires all mute states.
            always_on = await self.always_on_channels()
            toggle_channels = [ch for ch in channels if ch not in always_on]
            if not toggle_channels:
                return {
                    "ok": True, "group": group, "results": [], "skipped": [],
                    "protected": sorted(channels),
                }

            # Always-on channels are excluded from deciding the group's toggle state.
            unsafe, states = await self._evaluate_group(toggle_channels, require_mute_state=True)
            if unsafe:
                log.warning("BLOCKED group=%s action=toggle reason=unsafe details=%s", group, unsafe)
                return {
                    "ok": False,
                    "group": group,
                    "reason": "unsafe group operation",
                    "details": unsafe,
                }

            all_muted = all(states[ch].muted for ch in toggle_channels)  # type: ignore[union-attr]
            return await self._group_set_locked(group, channels, not all_muted)

    async def create_group_from_current_status(self, name: str) -> dict:
        if not self._client.connected:
            return {"ok": False, "reason": "no mixer connection"}

        channels = []
        fader_hex = {}
        unsafe = []
        for channel in self._cfg.all_channels():
            state = await self._store.get_channel(channel)
            mute_safety = check_channel_mute_state(
                channel, state, self._cfg,
                session_epoch=self._client.session_epoch,
            )
            if not mute_safety.ok:
                unsafe.append(mute_safety.as_detail(channel))
                continue
            if state.muted:
                continue

            fader_safety = check_channel_send(
                channel, state, self._cfg, session_epoch=self._client.session_epoch
            )
            if not fader_safety.ok:
                unsafe.append(fader_safety.as_detail(channel))
                continue
            channels.append(channel)
            fader_hex[str(channel)] = state.fader_hex

        if unsafe:
            return {
                "ok": False,
                "reason": "cannot capture current channel state",
                "details": unsafe,
            }
        if not channels:
            return {"ok": False, "reason": "no unmuted channels"}

        try:
            await self._groups.create(name, channels)
        except GroupError as exc:
            return {"ok": False, "reason": str(exc)}
        return {"ok": True, "group": name.strip(), "channels": channels, "fader_hex": fader_hex}


def _act(muted: bool) -> str:
    return "mute" if muted else "unmute"
