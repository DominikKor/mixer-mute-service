"""Test package: a network guard for every test run, and one runner for all
test modules.

Network guard
-------------
The service runs in the church LAN next to a live mixing console, lights,
cameras and PCs. A test must never reach any of them: before this guard,
tests that built the app with a default config opened a real control session
to the console (192.168.188.30:6400). Every ``python -m tests.<name>`` and
every ``unittest discover`` imports this package first, so this patch is in
place before any test code runs. Connections and datagrams to anything but
this machine fail with OSError and are reported on stderr with the marker
``NETWORK GUARD``; the runner below turns such a report into a failure.

One runner
----------
``python -m unittest discover -s tests -t . -v`` (from mixer_mute_service)
runs every ``test*.py`` module plus ``selftest.py``, each in its own Python
process exactly as ``python -m tests.<name>`` would. The modules come in two
styles (unittest classes and scripts with check() counters that run at import
or in main()); running each in a fresh process supports both, keeps
module-level environment changes and monkeypatches from leaking into other
modules, and reports every failing module instead of stopping at the first.
"""

from __future__ import annotations

import ipaddress
import os
import socket
import subprocess
import sys
import unittest
from pathlib import Path

GUARD_MARKER = "NETWORK GUARD"


def _is_local(address) -> bool:
    if not isinstance(address, tuple) or not address:
        return True  # AF_UNIX paths and the like
    host = address[0]
    if host in ("localhost", "", "0.0.0.0", "::"):
        return True
    try:
        return ipaddress.ip_address(host.split("%")[0]).is_loopback
    except ValueError:
        return False  # an unresolved host name: never local in tests


def _guarded(original):
    def guarded(self, address, *args, **kwargs):
        # sendto(data, address) / sendto(data, flags, address): address is last
        target = args[-1] if original.__name__ == "sendto" and args else address
        if not _is_local(target):
            print(f"{GUARD_MARKER}: blocked {original.__name__} to {target!r}", file=sys.stderr, flush=True)
            raise OSError(f"tests must not reach the network ({target!r})")
        return original(self, address, *args, **kwargs)
    guarded.__name__ = original.__name__
    return guarded


def _install_guard() -> None:
    if getattr(socket.socket, "_mixer_test_guard", False):
        return
    socket.socket.connect = _guarded(socket.socket.connect)
    socket.socket.connect_ex = _guarded(socket.socket.connect_ex)
    socket.socket.sendto = _guarded(socket.socket.sendto)
    socket.socket._mixer_test_guard = True


_install_guard()


# -- one runner ----------------------------------------------------------------

_TESTS_DIR = Path(__file__).resolve().parent
_SERVICE_DIR = _TESTS_DIR.parent
_MODULE_TIMEOUT = 600


def _module_names():
    names = sorted(p.stem for p in _TESTS_DIR.glob("test*.py"))
    return names + ["selftest"]


def _module_case(name: str):
    def run(self):
        env = dict(os.environ)
        result = subprocess.run(
            [sys.executable, "-m", f"tests.{name}"],
            cwd=_SERVICE_DIR, env=env, capture_output=True, text=True,
            timeout=_MODULE_TIMEOUT,
        )
        output = result.stdout + result.stderr
        if result.returncode != 0:
            self.fail(f"tests.{name} exited with {result.returncode}\n{output[-6000:]}")
        if GUARD_MARKER in output:
            lines = [line for line in output.splitlines() if GUARD_MARKER in line]
            self.fail(f"tests.{name} tried to reach the network:\n" + "\n".join(lines[:20]))

    run.__name__ = f"test_{name}"
    run.__doc__ = f"python -m tests.{name}"
    return type(f"Module_{name}", (unittest.TestCase,), {run.__name__: run})


def load_tests(loader, standard_tests, pattern):
    """Used by ``unittest discover``: one test case per module, each run in
    its own process (see the module docstring)."""
    suite = unittest.TestSuite()
    for name in _module_names():
        suite.addTests(loader.loadTestsFromTestCase(_module_case(name)))
    return suite
