"""The lasting OBS connection (app/obs_client.py) against a fake OBS
WebSocket server on 127.0.0.1: one connection for many requests, requests
from several threads at once, OBS gone and back, no answer in time, login."""

import base64
import hashlib
import json
import random
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from websockets.sync.server import serve

from app import native_controls
from app.native_controls import NativeControlError
from app.obs_client import ObsClient, ObsError


class FakeObs:
    """obs-websocket 5 as far as the service uses it. Request types:
    Echo (answers its data), Slow (answers after a random delay, so answers
    come out of order), Silent (never answers), Fail (rejected)."""

    def __init__(self, port=0, password=None, login_delay=0.0):
        self.password = password
        self.login_delay = login_delay   # seconds before Hello and before Identified
        self.connections = 0
        self.identified = []
        self.open = []
        self.server = serve(self._handle, "127.0.0.1", port)
        self.port = self.server.socket.getsockname()[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        """OBS quits: no new connections, the open ones are closed."""
        self.server.shutdown()
        for ws in list(self.open):
            ws.close()
        self.thread.join(5)

    def _handle(self, ws):
        self.connections += 1
        self.open.append(ws)
        try:
            self._serve(ws)
        except Exception:  # noqa: BLE001 - the client went away
            pass
        finally:
            self.open.remove(ws)

    def _serve(self, ws):
        send_lock = threading.Lock()

        def send(message):
            with send_lock:
                try:
                    ws.send(json.dumps(message))
                except Exception:  # noqa: BLE001 - client gone
                    pass

        time.sleep(self.login_delay)
        hello = {"obsWebSocketVersion": "5.5.0", "rpcVersion": 1}
        if self.password:
            hello["authentication"] = {"challenge": "c" * 8, "salt": "s" * 8}
        send({"op": 0, "d": hello})
        identify = json.loads(ws.recv())["d"]
        if self.password:
            secret = base64.b64encode(hashlib.sha256((self.password + "s" * 8).encode()).digest()).decode()
            expected = base64.b64encode(hashlib.sha256((secret + "c" * 8).encode()).digest()).decode()
            if identify.get("authentication") != expected:
                ws.close(4009, "Authentication failed")
                return
        self.identified.append(identify)
        time.sleep(self.login_delay)
        send({"op": 2, "d": {"negotiatedRpcVersion": 1}})
        for raw in ws:
            request = json.loads(raw)["d"]
            kind = request["requestType"]
            answer = {"op": 7, "d": {"requestType": kind, "requestId": request["requestId"],
                                     "requestStatus": {"result": kind != "Fail", "code": 100},
                                     "responseData": {"echo": request.get("requestData")}}}
            if kind == "Silent":
                continue
            if kind == "Slow":
                threading.Timer(random.uniform(0, 0.05), send, args=(answer,)).start()
            else:
                send(answer)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


class ObsClientTests(unittest.TestCase):
    def setUp(self):
        self.obs = FakeObs()
        self.addCleanup(self.obs.stop)

    def client(self, **kwargs):
        client = ObsClient("127.0.0.1", self.obs.port, **{"timeout": 2.0, **kwargs})
        self.addCleanup(client.close)
        return client

    def test_one_connection_for_many_requests(self):
        client = self.client()
        for n in range(25):
            self.assertEqual(client.batch([("Echo", {"n": n})]), [{"echo": {"n": n}}])
        self.assertEqual(client.batch([("Echo", {"a": 1}), ("Fail", None), ("Echo", {"b": 2})]),
                         [{"echo": {"a": 1}}, None, {"echo": {"b": 2}}])
        self.assertEqual(self.obs.connections, 1)
        self.assertEqual(client.connections, 1)
        self.assertEqual(self.obs.identified, [{"rpcVersion": 1, "eventSubscriptions": 0}])

    def test_parallel_requests_get_their_own_answers(self):
        client = self.client()

        def ask(n):
            return client.batch([("Slow", {"n": n}), ("Slow", {"m": n})])

        with ThreadPoolExecutor(max_workers=12) as pool:
            answers = list(pool.map(ask, range(40)))
        self.assertEqual(answers, [[{"echo": {"n": n}}, {"echo": {"m": n}}] for n in range(40)])
        self.assertEqual(self.obs.connections, 1)

    def test_obs_gone_and_back(self):
        clock = Clock()
        attempts = []

        def connect(url, **kwargs):
            attempts.append(url)
            from websockets.sync.client import connect as real
            return real(url, **kwargs)

        client = self.client(clock=clock, connect=connect, retry_min=1.0, retry_max=8.0)
        self.assertEqual(client.batch([("Echo", 1)]), [{"echo": 1}])
        port = self.obs.port
        self.obs.stop()
        time.sleep(0.2)  # the reader notices the closed connection
        with self.assertRaises(ObsError):
            client.batch([("Echo", 2)])          # reconnects at once: OBS may just restart
        self.assertEqual(len(attempts), 2)
        for _ in range(5):                       # OBS still gone: no attempt on every poll
            with self.assertRaisesRegex(ObsError, "next try"):
                client.batch([("Echo", 3)])
        self.assertEqual(len(attempts), 2)
        clock.now += 1.0
        with self.assertRaises(ObsError):
            client.batch([("Echo", 4)])          # second try, the pause doubles
        self.assertEqual(len(attempts), 3)
        clock.now += 1.5
        with self.assertRaisesRegex(ObsError, "next try"):
            client.batch([("Echo", 5)])
        self.obs = FakeObs(port=port)            # OBS is back
        clock.now += 1.0
        self.assertEqual(client.batch([("Echo", 6)]), [{"echo": 6}])
        self.assertEqual(self.obs.connections, 1)
        self.assertEqual(client.batch([("Echo", 7)]), [{"echo": 7}])
        self.assertEqual(self.obs.connections, 1)

    def test_no_answer_in_time(self):
        client = self.client(timeout=0.3)
        started = time.monotonic()
        with self.assertRaisesRegex(ObsError, "in time"):
            client.batch([("Silent", None)])
        self.assertLess(time.monotonic() - started, 1.5)
        # The stuck connection was dropped; the next request works on a new one.
        self.assertEqual(client.batch([("Echo", 1)]), [{"echo": 1}])
        self.assertEqual(self.obs.connections, 2)

    def test_password_login(self):
        self.obs.stop()
        self.obs = FakeObs(password="geheim")
        self.assertEqual(self.client(password=lambda: "geheim").batch([("Echo", 1)]), [{"echo": 1}])
        with self.assertRaisesRegex(ObsError, "password is required"):
            ObsClient("127.0.0.1", self.obs.port, password=lambda: "").batch([("Echo", 1)])
        with self.assertRaises(ObsError):
            ObsClient("127.0.0.1", self.obs.port, password=lambda: "falsch").batch([("Echo", 1)])

    def test_closed_client_refuses(self):
        client = self.client()
        client.batch([("Echo", 1)])
        client.close()
        with self.assertRaises(ObsError):
            client.batch([("Echo", 2)])


class SlowObsTests(unittest.TestCase):
    """An OBS that answers, but slowly: no caller waits longer than its
    timeout (connecting included), however many ask at once, and close()
    does not wait for a connect in progress."""

    def _obs(self, login_delay):
        obs = FakeObs(login_delay=login_delay)
        self.addCleanup(obs.stop)
        return obs

    def _ask_at_once(self, client, callers):
        def ask(n):
            started = time.monotonic()
            try:
                return ("ok", client.batch([("Echo", {"n": n})]), time.monotonic() - started)
            except ObsError as exc:
                return ("error", str(exc), time.monotonic() - started)

        with ThreadPoolExecutor(max_workers=callers) as pool:
            return list(pool.map(ask, range(callers)))

    def test_callers_give_up_after_their_timeout(self):
        obs = self._obs(login_delay=0.8)          # login takes 1.6 s
        client = ObsClient("127.0.0.1", obs.port, timeout=1.0)
        self.addCleanup(client.close)
        results = self._ask_at_once(client, 4)
        self.assertTrue(all(kind == "error" for kind, _, _ in results), results)
        self.assertTrue(all(took < 1.4 for _, _, took in results), results)
        self.assertEqual(obs.connections, 1, "the callers share one connect")

    def test_slow_login_within_the_timeout_is_shared(self):
        obs = self._obs(login_delay=0.25)         # login takes 0.5 s
        client = ObsClient("127.0.0.1", obs.port, timeout=1.0)
        self.addCleanup(client.close)
        results = self._ask_at_once(client, 6)
        self.assertEqual([answer for _, answer, _ in results], [[{"echo": {"n": n}}] for n in range(6)])
        self.assertTrue(all(took < 1.0 for _, _, took in results), results)
        self.assertEqual(obs.connections, 1)

    def test_close_returns_at_once_while_connecting(self):
        obs = self._obs(login_delay=0.6)
        client = ObsClient("127.0.0.1", obs.port, timeout=3.0)
        outcome = []
        caller = threading.Thread(target=lambda: outcome.append(self._ask_at_once(client, 1)[0]))
        caller.start()
        time.sleep(0.2)                           # the connect is in progress
        started = time.monotonic()
        client.close()
        self.assertLess(time.monotonic() - started, 0.1)
        caller.join(0.5)
        self.assertFalse(caller.is_alive(), "the waiting caller is told at once")
        self.assertEqual(outcome[0][0], "error")
        time.sleep(1.5)                           # the login finishes: the socket is dropped
        self.assertEqual(client.connections, 0)
        self.assertEqual(obs.open, [])


class ServiceUsesOneConnectionTests(unittest.TestCase):
    def test_native_controls_requests_share_the_connection(self):
        obs = FakeObs()
        self.addCleanup(obs.stop)
        self.addCleanup(native_controls.close_obs_clients)
        for n in range(10):
            self.assertEqual(native_controls._obs_request("127.0.0.1", obs.port, "Echo", {"n": n}), {"echo": {"n": n}})
        with self.assertRaisesRegex(NativeControlError, "OBS rejected Fail"):
            native_controls._obs_request("127.0.0.1", obs.port, "Fail")
        self.assertEqual(obs.connections, 1)
        native_controls.close_obs_clients()
        native_controls._obs_request("127.0.0.1", obs.port, "Echo")
        self.assertEqual(obs.connections, 2, "closed at shutdown, a new one afterwards")


if __name__ == "__main__":
    unittest.main()
