"""Requests from other web sites are refused (app/request_guard.py)."""

import unittest

from app.request_guard import host_allowed, refusal
from tests.helpers import app_client


class HostTests(unittest.TestCase):
    def test_allowed(self):
        for host in ("192.168.188.20:8765", "192.168.188.20", "127.0.0.1:8765", "[::1]:8765",
                     "localhost:8765", "streaming-pc:8765", "streaming-pc.local", "mixer.fritz.box:8765",
                     "testserver"):
            self.assertTrue(host_allowed(host), host)

    def test_refused(self):
        for host in ("evil.example.com", "attacker.com:8765", "", "192.168.188.20.nip.io:8765"):
            self.assertFalse(host_allowed(host), host)

    def test_configured_name(self):
        self.assertTrue(host_allowed("mixer.gemeinde.de:8765", ["Mixer.Gemeinde.de"]))


class RefusalTests(unittest.TestCase):
    HOST = {"host": "192.168.188.20:8765"}

    def test_own_page_and_programs_pass(self):
        self.assertIsNone(refusal("POST", {**self.HOST, "origin": "http://192.168.188.20:8765",
                                           "sec-fetch-site": "same-origin"}))
        self.assertIsNone(refusal("POST", self.HOST))  # curl, Companion: no Origin
        self.assertIsNone(refusal("GET", {**self.HOST, "origin": "http://evil.example.com"}))

    def test_other_sites_refused(self):
        for headers in (
            {"origin": "http://evil.example.com"},
            {"origin": "http://192.168.188.20:9999"},
            {"origin": "null"},
            {"sec-fetch-site": "cross-site"},
        ):
            self.assertIsNotNone(refusal("POST", {**self.HOST, **headers}), headers)
            self.assertIsNotNone(refusal("DELETE", {**self.HOST, **headers}), headers)


class MiddlewareTests(unittest.TestCase):
    def test_service_refuses_cross_site_mute(self):
        with app_client() as (client, _):
            r = client.post("/panic/disable", headers={"Origin": "http://evil.example.com"})
            self.assertEqual(r.status_code, 403)
            self.assertTrue(client.app.state.controller.enabled)
            r = client.get("/health", headers={"Host": "evil.example.com"})
            self.assertEqual(r.status_code, 403)
            self.assertIn("allowed_hosts", r.json()["reason"])
            r = client.post("/panic/disable", headers={"Origin": "http://testserver"})
            self.assertEqual(r.status_code, 200)
            client.post("/panic/enable")


if __name__ == "__main__":
    unittest.main()
