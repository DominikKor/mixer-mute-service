"""YouTube links in audio profiles (app/youtube.py, app/desktop_profiles.py):
accepted only for audio, saved like a path, and sent to the display agent
unchanged when the profile plays. The agent side is in test_agent_audio.py.
No device is contacted (the agent call is faked)."""

import unittest
from unittest import mock

from app.config import DesktopProfileConfig
from app.desktop_profiles import ProfileEditError, validate_profile
from app.youtube import is_youtube_url
from tests.helpers import app_client

URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
DESKTOP = "desktop_display:\n  enabled: true\n  token_env: YT_TEST_TOKEN\n  profiles: {}\n"


class YoutubeProfileTests(unittest.TestCase):
    def test_detection(self):
        self.assertTrue(is_youtube_url("https://youtu.be/dQw4w9WgXcQ"))
        self.assertTrue(is_youtube_url("https://music.youtube.com/watch?v=dQw4w9WgXcQ&list=RD1"))
        self.assertFalse(is_youtube_url("https://www.youtube.com.evil.example/watch?v=dQw4w9WgXcQ"))
        self.assertFalse(is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ\n"))
        self.assertFalse(is_youtube_url(r"C:\Musik\a.mp3"))

    def test_only_audio_profiles_take_a_link(self):
        validate_profile(DesktopProfileConfig(title="Lied", kind="audio", path=URL, fade=True))
        for kind in ("image", "file", "program"):
            with self.assertRaises(ProfileEditError, msg=kind):
                validate_profile(DesktopProfileConfig(title="X", kind=kind, path=URL))
        with self.assertRaises(ProfileEditError):  # any other web address is no path either
            validate_profile(DesktopProfileConfig(title="X", kind="audio", path="https://example.com/a.mp3"))

    def test_saved_and_played_through_the_agent(self):
        with app_client(DESKTOP, env={"YT_TEST_TOKEN": "t"}) as (client, folder):
            body = {"id": "lobpreis", "title": "Lobpreis", "kind": "audio", "path": URL,
                    "target_ip": "192.168.188.11", "fade": True, "fade_seconds": 3}
            r = client.post("/desktop/profiles", json=body)
            self.assertEqual(r.status_code, 200, r.text)
            self.assertIn(URL, (folder / "config.yaml").read_text(encoding="utf-8"))
            self.assertEqual(client.post("/desktop/profiles", json={**body, "id": "bild", "kind": "image"}).status_code, 400)
            with mock.patch("app.display_agent.call", mock.AsyncMock(return_value={"ok": True, "playing": True})) as call:
                r = client.post("/desktop/lobpreis/audio/play")
            self.assertEqual(r.status_code, 200, r.text)
            payload = call.call_args.args[3]
            self.assertEqual((payload["action"], payload["path"]), ("play", URL))
            self.assertEqual(payload["fade"]["seconds"], 3)


if __name__ == "__main__":
    unittest.main()
