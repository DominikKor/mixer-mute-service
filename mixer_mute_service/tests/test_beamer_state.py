"""HTTP behavior for the remembered Beamer hide state.

The beamer cannot be asked for its state and AV mute is a toggle, so the
service remembers it. A beamer starts un-hidden: on every service start and
after the service powers the beamer on or off, the remembered state becomes
visible, without sending anything. The form POST is faked: nothing goes onto
the network.
"""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import native_controls as nc

AVMUTE = nc.BEAMER_COMMAND_FORMS["avmute"]
POWER_ON = nc.BEAMER_COMMAND_FORMS["power-on"]
POWER_OFF = nc.BEAMER_COMMAND_FORMS["power-off"]


class FakeBeamer:
    def __init__(self):
        self.forms = []
        self.fail = None

    def __call__(self, field, value):
        if self.fail is not None:
            raise self.fail
        self.forms.append((field, value))


class BeamerStateRouteTests(unittest.TestCase):
    def setUp(self):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        self.directory = Path(temp_dir.name)
        self.config_path = self.directory / "config.yaml"
        self.config_path.write_text(
            "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
            "server:\n  host: 127.0.0.1\n  port: 8765\n",
            encoding="utf-8",
        )
        self.state_path = self.directory / "native_control_state.json"
        self.beamer = FakeBeamer()
        for patcher in (patch.dict(os.environ, {"MIXER_CONFIG": str(self.config_path)}),
                        patch.object(nc, "_post_beamer_form", self.beamer)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def remembered(self, hidden):
        self.state_path.write_text(json.dumps({"beamer_hidden": hidden}), encoding="utf-8")

    def persisted(self):
        return json.loads(self.state_path.read_text(encoding="utf-8"))["beamer_hidden"]

    def client(self):
        from fastapi.testclient import TestClient
        from app.main import app

        return TestClient(app)

    def press(self, client, key):
        return client.post(f"/companion/buttons/{key}/press")

    def hidden(self, client):
        return client.get("/beamer/state").json()["hidden"]

    def assert_keys_after_a_reset(self, client):
        """Visible: Unhide sends nothing, Hide sends exactly one toggle."""
        sent = len(self.beamer.forms)
        unhide = self.press(client, "beamer-unhide")
        self.assertEqual(unhide.status_code, 200)
        self.assertFalse(unhide.json()["changed"])
        self.assertEqual(len(self.beamer.forms), sent, "Unhide on a visible beamer sends nothing")
        hide = self.press(client, "beamer-hide")
        self.assertEqual(hide.status_code, 200)
        self.assertTrue(hide.json()["changed"])
        self.assertEqual(self.beamer.forms[sent:], [AVMUTE], "Hide sends exactly one toggle")
        self.assertTrue(self.hidden(client))

    def test_service_start_remembers_a_visible_beamer_and_sends_nothing(self):
        for saved in (True, None, False):
            with self.subTest(saved=saved):
                self.beamer.forms.clear()
                if saved is None:
                    self.state_path.unlink(missing_ok=True)
                else:
                    self.remembered(saved)
                with self.client() as client:
                    self.assertIs(self.hidden(client), False)
                    self.assertFalse(self.persisted())
                    self.assertEqual(self.beamer.forms, [], "nothing goes to the beamer at the start")
                    self.assert_keys_after_a_reset(client)

    def test_declared_state_and_hide_unhide_keys(self):
        with self.client() as client:
            declared = client.post("/beamer/state", json={"hidden": True})
            self.assertEqual(declared.status_code, 200)
            self.assertTrue(declared.json()["hidden"])
            self.assertEqual(self.beamer.forms, [], "declaring sends nothing")

            already_hidden = self.press(client, "beamer-hide")
            self.assertEqual(already_hidden.status_code, 200)
            self.assertFalse(already_hidden.json()["changed"])
            self.assertEqual(self.beamer.forms, [])

            unhide = self.press(client, "beamer-unhide")
            self.assertEqual(unhide.status_code, 200)
            self.assertTrue(unhide.json()["changed"])
            self.assertEqual(self.beamer.forms, [AVMUTE])
            self.assertFalse(self.hidden(client))
        self.assertFalse(self.persisted())

    def test_power_on_and_off_remember_a_visible_beamer(self):
        for key, form in (("beamer-power-on", POWER_ON), ("beamer-power-off", POWER_OFF)):
            with self.subTest(key=key), self.client() as client:
                self.beamer.forms.clear()
                client.post("/beamer/state", json={"hidden": True})
                result = self.press(client, key)
                self.assertEqual(result.status_code, 200)
                self.assertEqual(self.beamer.forms, [form], "only the power command, no toggle")
                self.assertIs(self.hidden(client), False)
                self.assertFalse(self.persisted())
                self.assert_keys_after_a_reset(client)

    def test_failed_power_on_keeps_the_state(self):
        with self.client() as client:
            client.post("/beamer/state", json={"hidden": True})
            self.beamer.fail = nc.NativeControlError("Beamer HTTP control unavailable")
            result = self.press(client, "beamer-power-on")
            self.assertEqual(result.status_code, 503)
            self.assertTrue(self.hidden(client), "the beamer may still be hidden")

    def test_alles_ausschalten_remembers_a_visible_beamer(self):
        with self.client() as client:
            client.post("/beamer/state", json={"hidden": True})
            saved = client.put("/power/off-config", json={"target_ips": [], "beamer": True})
            self.assertEqual(saved.status_code, 200)
            off = client.post("/power/off")
            self.assertEqual(off.status_code, 200)
            self.assertEqual(self.beamer.forms, [POWER_OFF])
            self.assertIs(self.hidden(client), False)


if __name__ == "__main__":
    unittest.main()
