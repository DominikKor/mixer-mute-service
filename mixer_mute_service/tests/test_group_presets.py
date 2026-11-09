"""Saved fader values and descriptions per group live on the service
(app/group_presets.py), so Safari's 7-day storage wipe cannot lose them."""

import unittest

from tests.helpers import app_client

GROUPS = "groups:\n  Chor:\n    channels: [1, 2]\n"
HEX_0DB = "00803b44"  # 750.0 = 0 dB


class GroupPresetTests(unittest.TestCase):
    def test_save_rename_delete(self):
        with app_client(GROUPS) as (client, folder):
            self.assertEqual(client.get("/group-presets").json(), {})
            r = client.put("/group-presets/Chor/1", json={"fader_hex": HEX_0DB})
            self.assertEqual(r.status_code, 200)
            self.assertIsInstance(r.json()["set_at"], int)
            r = client.put("/group-presets/Chor/1", json={"description": "  Sopran  "})
            entry = client.get("/group-presets").json()["Chor"]["1"]
            self.assertEqual((entry["fader_hex"], entry["description"]), (HEX_0DB, "Sopran"))
            self.assertTrue((folder / "group_presets.json").exists())

            for body, status in (
                ({"fader_hex": "ffffffff"}, 400),       # NaN: never a fader position
                ({"fader_hex": "00c07a46"}, 400),       # 16048 > 1000
                ({"other": 1}, 400),
                ({"description": "x" * 201}, 400),
            ):
                self.assertEqual(client.put("/group-presets/Chor/1", json=body).status_code, status, body)
            self.assertEqual(client.put("/group-presets/Chor/5", json={"fader_hex": HEX_0DB}).status_code, 400)
            self.assertEqual(client.put("/group-presets/Nope/1", json={"fader_hex": HEX_0DB}).status_code, 404)

            client.post("/groups/Chor/rename", json={"name": "Chor neu"})
            self.assertEqual(list(client.get("/group-presets").json()), ["Chor neu"])
            # A channel removed from the group drops out of the view.
            client.put("/groups/Chor neu", json={"channels": [2]})
            self.assertEqual(client.get("/group-presets").json(), {})
            client.put("/groups/Chor neu", json={"channels": [1, 2]})
            self.assertIn("1", client.get("/group-presets").json()["Chor neu"])
            client.delete("/groups/Chor neu")
            client.post("/groups", json={"name": "Chor neu", "channels": [1]})
            self.assertEqual(client.get("/group-presets").json(), {})


if __name__ == "__main__":
    unittest.main()
