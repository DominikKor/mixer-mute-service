"""Streaming PC / Anzeige PC: the list, its checks, and that a changed address
is carried over to desktop keys (config.yaml), the song display, the all-off
selection and file actions in program rows and templates, also when the two
addresses are swapped."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

DISPLAY, STREAMING, NEW = "192.168.188.11", "192.168.188.20", "192.168.188.12"
FILE_ON_DISPLAY = f"file:{DISPLAY}|I:\\Musik\\Intro.mp3"


def pcs(streaming=STREAMING, display=DISPLAY, names=("Streaming PC", "Anzeige PC")):
    return {"pcs": [{"id": "streaming", "name": names[0], "ip": streaming},
                    {"id": "display", "name": names[1], "ip": display}]}


class TargetPcRouteTests(unittest.TestCase):
    def test_list_checks_and_moving_an_address(self):
        with tempfile.TemporaryDirectory() as directory:
            data = Path(directory)
            config_path = data / "config.yaml"
            config_path.write_text(
                "mixer:\n  ip: 127.0.0.1\n  port: 9\n  max_channels: 8\n"
                "server:\n  host: 127.0.0.1\n  port: 8765\n"
                "desktop_display:\n  enabled: false\n  profiles:\n"
                "    qr:\n      title: QR  # on the display PC\n      kind: image\n"
                f"      path: 'C:\\qr.png'\n      target_ip: \"{DISPLAY}\"\n"
                "    obs:\n      title: OBS\n      kind: file\n"
                f"      path: 'C:\\x.txt'\n      target_ip: \"{STREAMING}\"\n",
                encoding="utf-8",
            )
            with patch.dict(os.environ, {"MIXER_CONFIG": str(config_path)}):
                from fastapi.testclient import TestClient
                from app.main import app

                with TestClient(app) as client:
                    got = client.get("/target-pcs").json()
                    self.assertEqual([(p["name"], p["ip"]) for p in got["pcs"]],
                                     [("Streaming PC", STREAMING), ("Anzeige PC", DISPLAY)])

                    # Checks: names, valid and distinct addresses, both PCs.
                    self.assertEqual(client.put("/target-pcs", json=pcs(display="300.1.1.1")).status_code, 400)
                    self.assertEqual(client.put("/target-pcs", json=pcs(display=STREAMING)).status_code, 400)
                    self.assertEqual(client.put("/target-pcs", json=pcs(names=("", "Anzeige PC"))).status_code, 400)
                    self.assertEqual(client.put("/target-pcs", json={"pcs": pcs()["pcs"][:1]}).status_code, 400)
                    # The agent token never goes to the internet.
                    self.assertEqual(client.put("/target-pcs", json=pcs(display="8.8.8.8")).status_code, 400)

                    # Things that use the Anzeige PC by its address.
                    client.put("/songs/settings", json={"target_ip": DISPLAY})
                    row = client.post("/program/items", json={
                        "time": "10:00", "title": "Intro", "group": "", "playback": FILE_ON_DISPLAY})
                    self.assertEqual(row.status_code, 200, row.text)
                    saved = client.post("/program/templates", json={"name": "Sonntag", "slots": []})
                    self.assertEqual(saved.status_code, 200, saved.text)
                    client.app.state.power_control._targets = {DISPLAY}

                    # Renaming alone moves nothing.
                    r = client.put("/target-pcs", json=pcs(names=("Stream", "Beamer-PC")))
                    self.assertEqual(r.status_code, 200)
                    self.assertEqual(r.json()["moved"], {"problems": []})

                    # A new address for the Anzeige PC is carried over everywhere.
                    r = client.put("/target-pcs", json=pcs(display=NEW))
                    self.assertEqual(r.status_code, 200, r.text)
                    moved = r.json()["moved"]
                    self.assertEqual((moved["profiles"], moved["songs"], moved["power"],
                                      moved["program_rows"], moved["template_rows"], moved["problems"]),
                                     (1, True, True, 1, 1, []))
                    profiles = {p["id"]: p["target_ip"] for p in client.get("/desktop/profiles").json()["profiles"]}
                    self.assertEqual(profiles, {"qr": NEW, "obs": STREAMING})
                    text = config_path.read_text(encoding="utf-8")
                    self.assertIn("# on the display PC", text)  # config.yaml edited in place
                    self.assertEqual(yaml.safe_load(text)["desktop_display"]["profiles"]["qr"]["target_ip"], NEW)
                    self.assertEqual(client.get("/songs/settings").json()["target_ip"], NEW)
                    self.assertEqual(client.get("/program/items").json()[0]["playback"],
                                     f"file:{NEW}|I:\\Musik\\Intro.mp3")
                    template = client.get("/program/templates").json()["templates"][0]
                    self.assertEqual(template["rows"][0]["playback"], f"file:{NEW}|I:\\Musik\\Intro.mp3")
                    power = json.loads((data / "power_off_config.json").read_text(encoding="utf-8"))
                    self.assertEqual(power["targets"], [NEW])

                    # Swapping the two addresses swaps every use, nothing merges.
                    r = client.put("/target-pcs", json=pcs(streaming=NEW, display=STREAMING))
                    self.assertEqual(r.json()["moved"]["problems"], [])
                    profiles = {p["id"]: p["target_ip"] for p in client.get("/desktop/profiles").json()["profiles"]}
                    self.assertEqual(profiles, {"qr": STREAMING, "obs": NEW})
                    self.assertEqual(client.get("/songs/settings").json()["target_ip"], STREAMING)

                    stored = json.loads((data / "target_pcs.json").read_text(encoding="utf-8"))
                    self.assertEqual([p["ip"] for p in stored], [NEW, STREAMING])


if __name__ == "__main__":
    unittest.main()
