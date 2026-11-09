"""Persist the devices selected by the configurable all-off action."""

from __future__ import annotations

import asyncio
import ipaddress
from pathlib import Path

from .jsonstore import read_json, write_json_atomic


class PowerControlStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        data = read_json(path, dict, default={})
        targets = data.get("targets", [])
        self._targets = {ip for ip in targets if self._valid_ip(ip)} if isinstance(targets, list) else set()
        self._beamer = data.get("beamer", False) is True

    @staticmethod
    def _valid_ip(value: object) -> bool:
        if not isinstance(value, str):
            return False
        try:
            ipaddress.IPv4Address(value)
            return True
        except ipaddress.AddressValueError:
            return False

    async def get(self, available_targets: set[str]) -> dict:
        async with self._lock:
            return {
                "targets": sorted(self._targets.intersection(available_targets)),
                "beamer": self._beamer,
            }

    async def set(self, targets: list[str], beamer: bool, available_targets: set[str]) -> dict:
        if not isinstance(targets, list) or not all(self._valid_ip(ip) for ip in targets):
            raise ValueError("invalid agent target")
        if any(ip not in available_targets for ip in targets):
            raise ValueError("agent target is not configured in desktop profiles")
        async with self._lock:
            self._targets = set(targets)
            self._beamer = beamer
            write_json_atomic(self._path, {"targets": sorted(self._targets), "beamer": self._beamer})
            return {"targets": sorted(self._targets), "beamer": self._beamer}