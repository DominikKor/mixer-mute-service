"""Keeps web pages from other sites away from the API.

The service has no login (it is only reachable in the church LAN), so any
web page that a browser in that LAN opens could otherwise send requests to
it: a hidden form could mute the mixer or power off the PCs (cross-site
request forgery), and a page on a domain that resolves to the streaming PC
could even read the answers (DNS rebinding).

Two checks, before any route runs:
- Host: the address in the browser must be an IP address, "localhost", a
  local name (no dot, or .local/.lan/.home/.internal/.fritz.box/...) or a
  name listed in server.allowed_hosts in config.yaml.
- Changing requests (POST, PUT, PATCH, DELETE) sent by a browser must come
  from this service's own page: same Origin as Host, and not "cross-site".
  Programs (curl, Companion, the display agent) send neither header and
  are not affected.
"""

from __future__ import annotations

import logging
from ipaddress import ip_address
from typing import Iterable, Mapping, Optional
from urllib.parse import urlsplit

log = logging.getLogger("mixer.request_guard")

UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".internal", ".localhost", ".home.arpa", ".fritz.box", ".localdomain")


def _hostname(host_header: str) -> str:
    """The name or address of a Host header, without port, lower case."""
    host = host_header.strip().lower()
    if host.startswith("["):  # [::1]:8765
        return host[1:].split("]", 1)[0]
    return host.rsplit(":", 1)[0] if host.count(":") == 1 else host


def host_allowed(host_header: str, extra_hosts: Iterable[str] = ()) -> bool:
    name = _hostname(host_header)
    if not name:
        return False
    try:
        ip_address(name)
        return True
    except ValueError:
        pass
    if name == "localhost" or "." not in name.rstrip("."):
        return True
    if name.rstrip(".").endswith(LOCAL_SUFFIXES):
        return True
    return name in {h.strip().lower() for h in extra_hosts}


def refusal(method: str, headers: Mapping[str, str], extra_hosts: Iterable[str] = ()) -> Optional[str]:
    """Why this request is refused, or None. ``headers`` with lower-case keys."""
    host = headers.get("host", "")
    if not host_allowed(host, extra_hosts):
        return (f"host {_hostname(host)!r} not allowed; open the page by IP address "
                "or add the name to server.allowed_hosts in config.yaml")
    if method.upper() not in UNSAFE_METHODS:
        return None
    if headers.get("sec-fetch-site", "") == "cross-site":
        return "request from another web site refused"
    origin = headers.get("origin")
    if origin is None:
        return None
    if origin == "null" or urlsplit(origin).netloc.lower() != host.strip().lower():
        return "request from another web site refused"
    return None
