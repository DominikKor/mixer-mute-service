"""Orchestration: tie state + safety + protocol + client together.

Every public method here is the *only* sanctioned way to cause bytes to reach
the mixer. There is deliberately no path that sends without first passing the
safety layer (the unsafe debug flag is honoured but defaults off and is logged
loudly).

All results are plain JSON-compatible dicts matching the briefing's examples.
"""

from __future__ import annotations

import asyncio
import logging
from typing import List, Optional, Tuple

from .config import Config
from .groups import GroupStore
from .mixer_client import MixerClient
from .protocol import (
    build_bus_command,
    build_mute_command,
    fader_hex_to_value,
    fader_value_to_hex,
)
from .safety import check_channel_send
from .state import StateStore

log = logging.getLogger("mixer.controller")


class Controller:
    def __init__(
        self, cfg: Config, store: StateStore, client: MixerClient, groups: GroupStore
    ) -> None:
        self._cfg = cfg
        self._store = store
        self._client = client
        self._groups = groups
        self.enabled: bool = True  # panic toggles this

    # -- global gate -------------------------------------------------------

    def _global_block_reason(self) -> Optional[str]:
        if not self.enabled:
            return "service disabled (panic)"
        if not self._client.connected:
            return "no mixer connection"
        return None

    # -- single channel ----------------------------------------------------

    def _bus(self, channel: int):
        return next((b for b in self._cfg.mixer.buses if b.channel == channel), None)

    async def channel_set(self, channel: int, target_muted: bool) -> dict:
        if channel not in self._cfg.valid_channels():
            return {"ok": False, "channel": channel, "reason": "invalid channel"}

        reason = self._global_block_reason()
        if reason:
            log.warning("BLOCKED channel=%s action=%s reason=%r", channel, _act(target_muted), reason)
            return {"ok": False, "channel": channel, "reason": reason}

        cs = await self._store.get_channel(channel)
        safety = check_channel_send(
            channel, cs, self._cfg, session_epoch=self._client.session_epoch
        )
        if not safety.ok:
            log.warning(
                "BLOCKED channel=%s action=%s reason=%r age=%s",
                channel, _act(target_muted), safety.reason,
                None if safety.age_seconds is None else f"{safety.age_seconds:.2f}s",
            )
            result = {"ok": False, "channel": channel, "reason": safety.reason}
            if safety.age_seconds is not None:
                result["age_seconds"] = round(safety.age_seconds, 3)
            return result

        assert cs is not None and cs.fader_hex is not None
        fader_hex = cs.fader_hex
        try:
            bus = self._bus(channel)
            if bus is not None:
                payload = build_bus_command(bus.id, target_muted, fader_hex)
            else:
                payload = build_mute_command(
                    channel, target_muted, fader_hex, self._cfg.mixer.max_channels
                )
        except ValueError as exc:
            log.warning("BLOCKED channel=%s action=%s reason=%r", channel, _act(target_muted), str(exc))
            return {"ok": False, "channel": channel, "reason": str(exc)}

        sent_hex = payload.hex()
        try:
            await self._client.send(payload)
        except Exception as exc:  # noqa: BLE001
            log.error("FAILED channel=%s action=%s error=%r", channel, _act(target_muted), str(exc))
            return {"ok": False, "channel": channel, "reason": f"send failed: {exc}"}

        # The mixer does not echo our injected command back, so record the new
        # mute state ourselves -- otherwise toggle would never see it change.
        await self._store.set_muted(channel, target_muted)

        log.info(
            "SENT channel=%s action=%s fader=%s payload=%s",
            channel, _act(target_muted), fader_hex, sent_hex,
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
            return {"ok": False, "channel": channel, "reason": "invalid channel"}

        try:
            fader_hex = fader_value_to_hex(value)
        except ValueError:
            log.warning("BLOCKED channel=%s action=fader reason='invalid fader value' value=%r", channel, value)
            return {"ok": False, "channel": channel, "reason": "invalid fader value"}

        reason = self._global_block_reason()
        if reason:
            log.warning("BLOCKED channel=%s action=fader reason=%r", channel, reason)
            return {"ok": False, "channel": channel, "reason": reason}

        cs = await self._store.get_channel(channel)
        safety = check_channel_send(
            channel, cs, self._cfg,
            session_epoch=self._client.session_epoch, require_mute_state=True,
        )
        if not safety.ok:
            log.warning("BLOCKED channel=%s action=fader reason=%r", channel, safety.reason)
            result = {"ok": False, "channel": channel, "reason": safety.reason}
            if safety.age_seconds is not None:
                result["age_seconds"] = round(safety.age_seconds, 3)
            return result

        assert cs is not None and cs.muted is not None
        muted = cs.muted
        try:
            bus = self._bus(channel)
            if bus is not None:
                payload = build_bus_command(bus.id, muted, fader_hex)
            else:
                payload = build_mute_command(
                    channel, muted, fader_hex, self._cfg.mixer.max_channels
                )
        except ValueError as exc:
            log.warning("BLOCKED channel=%s action=fader reason=%r", channel, str(exc))
            return {"ok": False, "channel": channel, "reason": str(exc)}

        sent_hex = payload.hex()
        try:
            await self._client.send(payload)
        except Exception as exc:  # noqa: BLE001
            log.error("FAILED channel=%s action=fader error=%r", channel, str(exc))
            return {"ok": False, "channel": channel, "reason": f"send failed: {exc}"}

        await self._store.set_fader(channel, fader_hex)

        log.info(
            "SENT channel=%s action=fader value=%.2f muted=%s fader=%s payload=%s",
            channel, fader_hex_to_value(fader_hex), muted, fader_hex, sent_hex,
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
            return {"ok": False, "channel": channel, "reason": "invalid channel"}

        reason = self._global_block_reason()
        if reason:
            return {"ok": False, "channel": channel, "reason": reason}

        cs = await self._store.get_channel(channel)
        safety = check_channel_send(
            channel, cs, self._cfg,
            session_epoch=self._client.session_epoch, require_mute_state=True,
        )
        if not safety.ok:
            result = {"ok": False, "channel": channel, "reason": safety.reason}
            if safety.age_seconds is not None:
                result["age_seconds"] = round(safety.age_seconds, 3)
            log.warning("BLOCKED channel=%s action=toggle reason=%r", channel, safety.reason)
            return result

        assert cs is not None and cs.muted is not None
        return await self.channel_set(channel, not cs.muted)

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

    async def group_set(self, group: str, target_muted: bool) -> dict:
        channels = await self._group_channels(group)
        if channels is None:
            return {"ok": False, "group": group, "reason": "unknown group"}

        reason = self._global_block_reason()
        if reason:
            log.warning("BLOCKED group=%s action=%s reason=%r", group, _act(target_muted), reason)
            return {"ok": False, "group": group, "reason": reason}

        unsafe, _ = await self._evaluate_group(channels, require_mute_state=False)
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
        for ch in channels:
            if ch in unsafe_channels:
                # only reachable when allow_partial_groups is explicitly enabled
                continue
            if sent_any:
                await asyncio.sleep(delay)
            results.append(await self.channel_set(ch, target_muted))
            sent_any = True

        # Solo-on-unmute: unmuting a group also mutes every OTHER channel (incl.
        # buses), but muting a group leaves the others alone. Channels we can't
        # safely mute (no fresh fader) are skipped, not forced.
        others = []
        if not target_muted and self._cfg.safety.exclusive_group_unmute:
            members = set(channels)
            for ch in sorted(self._cfg.valid_channels() - members):
                cs = await self._store.get_channel(ch)
                if cs is not None and cs.muted is True:
                    continue  # already muted, nothing to do
                await asyncio.sleep(delay)
                others.append(await self.channel_set(ch, True))

        all_ok = bool(results) and all(r.get("ok") for r in results)
        out = {
            "ok": all_ok,
            "group": group,
            "target_muted": target_muted,
            "results": results,
            "skipped": sorted(unsafe_channels) if unsafe_channels else [],
        }
        if others:
            out["muted_others"] = others
            blocked = [r for r in others if not r.get("ok")]
            if blocked:
                out["others_skipped"] = sorted(r["channel"] for r in blocked)
        return out

    async def group_toggle(self, group: str) -> dict:
        channels = await self._group_channels(group)
        if channels is None:
            return {"ok": False, "group": group, "reason": "unknown group"}

        reason = self._global_block_reason()
        if reason:
            return {"ok": False, "group": group, "reason": reason}

        # Toggle semantics: a group counts as "active" only when ALL channels are
        # muted -> then unmute; otherwise mute. Requires all mute states fresh.
        unsafe, states = await self._evaluate_group(channels, require_mute_state=True)
        if unsafe:
            log.warning("BLOCKED group=%s action=toggle reason=unsafe details=%s", group, unsafe)
            return {
                "ok": False,
                "group": group,
                "reason": "unsafe group operation",
                "details": unsafe,
            }

        all_muted = all(states[ch].muted for ch in channels)  # type: ignore[union-attr]
        target = not all_muted
        return await self.group_set(group, target)


def _act(muted: bool) -> str:
    return "mute" if muted else "unmute"
