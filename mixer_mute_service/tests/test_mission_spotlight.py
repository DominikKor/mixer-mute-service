"""Mission Spotlight profiles (app/mission_spotlight.py): the path is a folder
and the file of the current quarter week is picked by date. No device is
contacted (the agent call is faked)."""

import unittest
from datetime import date
from unittest import mock

from app import mission_spotlight
from app.config import DesktopProfileConfig
from app.desktop_profiles import ProfileEditError, validate_profile
from tests.helpers import app_client

DESKTOP = "desktop_display:\n  enabled: true\n  token_env: MS_TEST_TOKEN\n  profiles: {}\n"


class QuarterWeekTests(unittest.TestCase):
    def test_quarter_and_week(self):
        for day, expected in (
            (date(2026, 1, 1), (1, 1)), (date(2026, 1, 7), (1, 1)), (date(2026, 1, 8), (1, 2)),
            (date(2026, 3, 31), (1, 13)), (date(2026, 4, 1), (2, 1)), (date(2026, 7, 15), (3, 3)),
            (date(2026, 9, 30), (3, 14)), (date(2026, 12, 31), (4, 14)),
        ):
            self.assertEqual(mission_spotlight.quarter_and_week(day), expected, day)

    def test_path(self):
        self.assertEqual(mission_spotlight.spotlight_path("I:\\Spotlight\\", date(2026, 7, 2)),
                         "I:\\Spotlight\\2026_3. Quartal\\01*")
        self.assertEqual(mission_spotlight.spotlight_path("I:\\Spotlight", date(2026, 11, 14)),
                         "I:\\Spotlight\\2026_4. Quartal\\07*")
        self.assertEqual(mission_spotlight.spotlight_path("I:\\Spotlight", date(2026, 11, 26)),
                         "I:\\Spotlight\\2026_4. Quartal\\09*")
        self.assertEqual(mission_spotlight.spotlight_path("I:\\Spotlight", date(2027, 1, 3)),
                         "I:\\Spotlight\\2027_1. Quartal\\01*")
        self.assertEqual(mission_spotlight.spotlight_path("I:\\Spotlight", date(2026, 12, 31)),
                         "I:\\Spotlight\\2026_4. Quartal\\14*")
        plain = DesktopProfileConfig(title="Bild", kind="image", path="I:\\a.png")
        self.assertEqual(mission_spotlight.profile_path(plain), "I:\\a.png")

    def test_current_info_has_the_year(self):
        self.assertEqual(mission_spotlight.current(date(2026, 11, 26)),
                         {"year": 2026, "quarter": 4, "week": 9, "subfolder": "2026_4. Quartal", "file_prefix": "09"})
        self.assertEqual(mission_spotlight.current(date(2027, 1, 1))["subfolder"], "2027_1. Quartal")

    def test_validation(self):
        validate_profile(DesktopProfileConfig(title="MS", kind="file", path="I:\\Spotlight", mission_spotlight=True))
        for kind, path in (("program", "C:\\a.exe"), ("file", "I:\\Spot*"),
                           ("audio", "https://www.youtube.com/watch?v=dQw4w9WgXcQ")):
            with self.assertRaises(ProfileEditError, msg=kind):
                validate_profile(DesktopProfileConfig(title="MS", kind=kind, path=path, mission_spotlight=True))


class ApiTests(unittest.TestCase):
    def test_saved_listed_and_sent_with_the_week_file(self):
        with app_client(DESKTOP, env={"MS_TEST_TOKEN": "t"}) as (client, folder):
            body = {"id": "spotlight", "title": "Mission Spotlight", "kind": "file", "path": "I:\\Spotlight",
                    "target_ip": "192.168.188.11", "mission_spotlight": True}
            r = client.post("/desktop/profiles", json=body)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn("mission_spotlight: true", (folder / "config.yaml").read_text(encoding="utf-8"))
            listed = client.get("/desktop/profiles").json()
            today = mission_spotlight.current()
            self.assertEqual(listed["mission_spotlight"], today)
            self.assertTrue(listed["profiles"][0]["mission_spotlight"])
            expected = f"I:\\Spotlight\\{today['year']}_{today['quarter']}. Quartal\\{today['file_prefix']}*"
            self.assertEqual(listed["profiles"][0]["resolved_path"], expected)
            with mock.patch("app.display_agent.call", mock.AsyncMock(return_value={"ok": True})) as call:
                r = client.post("/desktop/spotlight/open")
            self.assertEqual(r.status_code, 200, r.text)
            payload = call.call_args.args[3]
            self.assertEqual(payload["path"], expected)
            self.assertNotIn("mission_spotlight", payload, "the agent refuses unknown keys")


if __name__ == "__main__":
    unittest.main()
