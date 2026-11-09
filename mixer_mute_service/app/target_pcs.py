"""The two PCs the service sends things to: Streaming PC and Anzeige PC.

Desktop keys, the song display, the all-off selection and file actions in the
program keep plain IP addresses; the web UI offers these two PCs instead of an
IP field. Their names and addresses are set in one place (target_pcs.json next
to config.yaml). Changing an address moves everything that used the old one
to the new one (see routes.update_target_pcs).
"""

from __future__ import annotations

import asyncio
from ipaddress import IPv4Address
from pathlib import Path
from typing import Dict, List

from .jsonstore import read_json, write_json_atomic

DEFAULT_PCS = [
    {"id": "streaming", "name": "Streaming PC", "ip": "192.168.188.20"},
    {"id": "display", "name": "Anzeige PC", "ip": "192.168.188.11"},
]
PC_IDS = [pc["id"] for pc in DEFAULT_PCS]


class TargetPcError(ValueError):
    pass


def _validated(pcs) -> List[dict]:
    """Exactly the two PCs, each with a name and a distinct IPv4 address."""
    if not isinstance(pcs, list):
        raise TargetPcError("pcs must be a list")
    by_id = {pc.get("id"): pc for pc in pcs if isinstance(pc, dict)}
    if sorted(by_id) != sorted(PC_IDS) or len(pcs) != len(PC_IDS):
        raise TargetPcError("expected the Streaming PC and the Anzeige PC")
    result = []
    for pc_id in PC_IDS:
        pc = by_id[pc_id]
        name = pc.get("name").strip() if isinstance(pc.get("name"), str) else ""
        if not name or len(name) > 40 or any(ord(c) < 32 for c in name):
            raise TargetPcError("name must contain 1 to 40 characters")
        ip = pc.get("ip").strip() if isinstance(pc.get("ip"), str) else ""
        try:
            IPv4Address(ip)
        except ValueError:
            raise TargetPcError(f"invalid IP address for {name}") from None
        result.append({"id": pc_id, "name": name, "ip": ip})
    if len({pc["ip"] for pc in result}) != len(result):
        raise TargetPcError("the two PCs need different IP addresses")
    return result


class TargetPcStore:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        try:
            self._pcs = _validated(read_json(path, list, default=DEFAULT_PCS))
        except TargetPcError:
            self._pcs = [dict(pc) for pc in DEFAULT_PCS]

    async def get(self) -> List[dict]:
        async with self._lock:
            return [dict(pc) for pc in self._pcs]

    async def set(self, pcs) -> Dict[str, str]:
        """Save the PCs; returns {old ip: new ip} for every changed address."""
        new = _validated(pcs)
        async with self._lock:
            old = {pc["id"]: pc["ip"] for pc in self._pcs}
            write_json_atomic(self._path, new, ensure_ascii=False)
            self._pcs = new
            return {old[pc["id"]]: pc["ip"] for pc in new if old[pc["id"]] != pc["ip"]}


def retarget_file_action(value, moves: Dict[str, str]):
    """A program file action "file:<ip>|<path>" on a moved PC, pointed at its
    new address; anything else unchanged."""
    if not isinstance(value, str) or not value.startswith("file:"):
        return value
    target, sep, path = value[len("file:"):].partition("|")
    return f"file:{moves[target]}|{path}" if sep and target in moves else value
