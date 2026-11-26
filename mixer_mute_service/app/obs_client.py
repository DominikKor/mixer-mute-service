"""One lasting OBS WebSocket connection for the whole service.

Opening a connection for every request made OBS report "new WebSocket
connection" and "WebSocket client disconnected" on every key press and
every status poll. Now the service connects once, identifies, and keeps
the connection open:

- requests are matched to their answers by requestId, so threads and tasks
  can ask at the same time over the one connection;
- every call is bounded (``timeout``, connecting included): connecting runs
  in its own thread, callers only wait for it with their own deadline and
  never under a lock, so a slow OBS cannot hold up several callers one
  after the other, and close() never waits for a connect;
- a request OBS does not answer in time drops the connection (the next
  request connects anew), so a stuck OBS cannot hold up callers for good;
- when OBS is gone, connecting is retried with a growing pause (1 s up to
  15 s) instead of on every request; meanwhile requests fail at once with
  a clear reason. A connection that breaks (OBS restarted) is retried on
  the next request straight away.

Events are not subscribed: the service only asks (status is polled with a
short cache, see app/obs_status.py).

Callers run in worker threads (asyncio.to_thread); nothing here blocks the
event loop."""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import threading
import time
import uuid
from typing import Callable, List, Optional, Tuple

log = logging.getLogger("mixer.obs_client")

UNAVAILABLE = "OBS WebSocket unavailable"


class ObsError(RuntimeError):
    """OBS could not be asked (not reachable, refused the login, no answer)."""


class _Attempt:
    """One connect in progress; every caller that needs the connection waits
    for the same attempt (each with its own deadline)."""
    __slots__ = ("done", "ws", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.ws = None
        self.error: Optional[str] = None


class _Pending:
    __slots__ = ("event", "data", "error")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.data: Optional[dict] = None
        self.error: Optional[str] = None


def _password() -> str:
    return os.environ.get("OBS_WEBSOCKET_PASSWORD", "")


class ObsClient:
    def __init__(self, host: str, port: int, timeout: float = 3.0,
                 password: Callable[[], str] = _password,
                 retry_min: float = 1.0, retry_max: float = 15.0,
                 clock: Callable[[], float] = time.monotonic,
                 connect: Optional[Callable] = None) -> None:
        self._url = f"ws://{host}:{port}"
        self._timeout = timeout
        self._password = password
        self._retry_min = retry_min
        self._retry_max = retry_max
        self._clock = clock
        self._connect_fn = connect
        self._lock = threading.Lock()        # connection state and pending requests
        self._send_lock = threading.Lock()
        self._ws = None
        self._attempt: Optional[_Attempt] = None
        self._pending: dict = {}
        self._retry_at = 0.0
        self._retry_delay = retry_min
        self._closed = False
        self.connections = 0                 # connections opened so far

    # -- requests -----------------------------------------------------------

    def batch(self, requests: List[Tuple[str, Optional[dict]]]) -> list:
        """[(request_type, request_data), ...] -> their response data, None
        for a request OBS rejected. Raises ObsError."""
        deadline = time.monotonic() + self._timeout
        ws = self._connection(deadline)
        sent = []
        for request_type, request_data in requests:
            request_id = uuid.uuid4().hex
            pending = _Pending()
            with self._lock:
                self._pending[request_id] = pending
            sent.append((request_id, pending))
            try:
                with self._send_lock:
                    ws.send(json.dumps({"op": 6, "d": {
                        "requestType": request_type, "requestId": request_id,
                        "requestData": request_data or {}}}))
            except Exception as exc:  # noqa: BLE001 - any send failure means the connection is gone
                self._forget(sent)
                self._drop(ws, f"send failed: {exc}")
                raise ObsError(UNAVAILABLE) from exc
        answers = []
        for request_id, pending in sent:
            if not pending.event.wait(max(0.0, deadline - time.monotonic())):
                self._forget(sent)
                self._drop(ws, "no answer in time")
                raise ObsError("OBS did not answer in time")
            if pending.error:
                self._forget(sent)
                raise ObsError(pending.error)
            answers.append(pending.data)
        return answers

    def close(self) -> None:
        """Returns at once: a connect still in progress is dropped when it
        finishes, the open connection is closed in the background."""
        with self._lock:
            self._closed = True
            ws, self._ws = self._ws, None
            self._fail_pending("OBS connection closed")
            attempt, self._attempt = self._attempt, None
        if attempt is not None:
            attempt.error = "OBS connection closed"
            attempt.done.set()
        if ws is not None:
            self._close_in_background(ws)

    # -- connection -----------------------------------------------------------

    def _connection(self, deadline: float):
        """The open connection, or wait (until ``deadline`` at most) for the
        connect in progress, starting one if needed."""
        with self._lock:
            if self._closed:
                raise ObsError("OBS connection closed")
            if self._ws is not None:
                return self._ws
            attempt = self._attempt
            if attempt is None:
                wait = self._retry_at - self._clock()
                if wait > 0:
                    raise ObsError(f"{UNAVAILABLE} (next try in {wait:.0f} s)")
                attempt = self._attempt = _Attempt()
                threading.Thread(target=self._connect, args=(attempt,), name="obs-connect", daemon=True).start()
        if not attempt.done.wait(max(0.0, deadline - time.monotonic())):
            raise ObsError(f"{UNAVAILABLE} (no answer in time while connecting)")
        if attempt.error:
            raise ObsError(attempt.error)
        return attempt.ws

    def _connect(self, attempt: _Attempt) -> None:
        """Connect thread: open and identify, then hand the connection over
        (or drop it, if the client was closed meanwhile)."""
        ws, error = None, None
        try:
            ws = self._open()
        except ObsError as exc:
            error = exc
        drop = None
        with self._lock:
            if self._attempt is attempt:
                self._attempt = None
            if self._closed:
                drop, ws = ws, None
                error = error or ObsError("OBS connection closed")
            elif error is not None:
                if self._retry_delay == self._retry_min:
                    log.warning("OBS: cannot connect to %s (%s); retrying with a pause", self._url, error)
                self._retry_at = self._clock() + self._retry_delay
                self._retry_delay = min(self._retry_max, self._retry_delay * 2)
            else:
                if self._retry_delay != self._retry_min or self.connections:
                    log.info("OBS: connected to %s", self._url)
                self._retry_delay = self._retry_min
                self._ws = ws
                self.connections += 1
        if drop is not None:
            self._close_quietly(drop)
        if ws is not None:
            threading.Thread(target=self._read, args=(ws,), name="obs-reader", daemon=True).start()
        if not attempt.done.is_set():  # close() may have answered the waiters already
            attempt.ws, attempt.error = ws, (str(error) if error else None)
            attempt.done.set()

    def _open(self):
        """Connect and identify (same login as before: password from
        OBS_WEBSOCKET_PASSWORD, challenge/salt per the obs-websocket protocol),
        all within one ``timeout``."""
        connect = self._connect_fn
        if connect is None:
            from websockets.sync.client import connect
        deadline = time.monotonic() + self._timeout

        def left() -> float:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("OBS login took too long")
            return remaining

        try:
            ws = connect(self._url, open_timeout=self._timeout, close_timeout=1)
        except Exception as exc:  # noqa: BLE001
            raise ObsError(UNAVAILABLE) from exc
        try:
            hello = json.loads(ws.recv(timeout=left()))
            authentication = hello.get("d", {}).get("authentication")
            # No events: the service only asks.
            identify = {"op": 1, "d": {"rpcVersion": 1, "eventSubscriptions": 0}}
            if authentication:
                password = self._password()
                if not password:
                    raise ObsError("OBS WebSocket password is required")
                secret = base64.b64encode(hashlib.sha256((password + authentication["salt"]).encode()).digest()).decode()
                identify["d"]["authentication"] = base64.b64encode(
                    hashlib.sha256((secret + authentication["challenge"]).encode()).digest()
                ).decode()
            ws.send(json.dumps(identify))
            if json.loads(ws.recv(timeout=left())).get("op") != 2:
                raise ObsError("OBS WebSocket identification failed")
            return ws
        except ObsError:
            self._close_quietly(ws)
            raise
        except Exception as exc:  # noqa: BLE001 - timeouts, closed connection, bad JSON
            self._close_quietly(ws)
            raise ObsError(UNAVAILABLE) from exc

    def _read(self, ws) -> None:
        """The one reader of a connection: hands each answer to its request."""
        reason = "connection lost"
        try:
            while True:
                message = json.loads(ws.recv())
                if message.get("op") != 7:
                    continue
                data = message.get("d") or {}
                with self._lock:
                    pending = self._pending.pop(data.get("requestId"), None)
                if pending is None:
                    continue  # answer to a request that already gave up
                ok = (data.get("requestStatus") or {}).get("result")
                pending.data = (data.get("responseData") or {}) if ok else None
                pending.event.set()
        except Exception as exc:  # noqa: BLE001 - closed by OBS, by us, or broken
            reason = f"connection lost ({type(exc).__name__})"
        finally:
            self._drop(ws, reason)

    def _drop(self, ws, reason: str) -> None:
        """Forget a broken connection; its waiting requests fail. The next
        request connects again at once (OBS may just have restarted)."""
        with self._lock:
            if self._ws is not ws:
                return
            self._ws = None
            self._retry_at = 0.0
            self._fail_pending(f"{UNAVAILABLE} ({reason})")
        if not self._closed:
            log.warning("OBS: %s", reason)
        # Not in the caller's thread: closing waits up to close_timeout for
        # OBS to answer the close, and the caller must get its error now.
        self._close_in_background(ws)

    def _fail_pending(self, error: str) -> None:
        """The caller holds self._lock."""
        for pending in self._pending.values():
            pending.error = error
            pending.event.set()
        self._pending.clear()

    def _forget(self, sent) -> None:
        with self._lock:
            for request_id, _pending in sent:
                self._pending.pop(request_id, None)

    @classmethod
    def _close_in_background(cls, ws) -> None:
        threading.Thread(target=cls._close_quietly, args=(ws,), name="obs-close", daemon=True).start()

    @staticmethod
    def _close_quietly(ws) -> None:
        try:
            ws.close()
        except Exception:  # noqa: BLE001
            pass
