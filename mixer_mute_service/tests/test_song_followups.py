"""Follow-ups to the song layout: a refused layout, the font-widths cache and
the live slide after a settings change.

- A current display agent that refuses a value (422) gets that slide once
  more without the layout, and nothing is remembered.
- The font widths the agents measured are kept for a few fonts per screen
  (the settings preview measures every font name typed), never dropping the
  saved font.
- Saving other settings shows the live slide again with them at once.
"""

import unittest
from unittest.mock import patch

from app import songs
from tests.test_song_uniform_size import SHORT, TWO_STANZAS, AgentStub, fake_request, service_with

IP = "192.168.188.11"


class RefusedLayoutTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_refused_layout_is_sent_once_more_without_it(self):
        service = service_with(SHORT, TWO_STANZAS)

        class Picky(AgentStub):
            async def __call__(self, cfg, ip, path, payload, timeout=3.0):
                if path == "/song" and "layout" in payload:
                    self.calls.append((ip, path, payload))
                    raise songs.AgentError("unprocessable", 502, agent_status=422,
                                           body={"ok": False, "reason": "invalid layout"})
                return await super().__call__(cfg, ip, path, payload, timeout)

        agent = Picky()
        request = fake_request(service)
        with patch("app.display_agent.call", agent):
            answer = await songs.show_slide(request, "a.foil", 1)
            self.assertTrue(answer["ok"])
            self.assertEqual(["layout" in p for p in agent.shows()], [True, False])
            self.assertTrue(service.senders[IP].up)          # the screen shows the slide
            self.assertNotIn((IP, "song_level"), service.agent_memory)
            await songs.song_next(request)                   # the next slide: with its layout first again
        self.assertEqual(["layout" in p for p in agent.shows()], [True, False, True, False])

    async def test_a_value_refused_without_layout_too_is_not_sent_again_and_again(self):
        service = service_with(SHORT)
        calls = []

        async def refuses(cfg, ip, path, payload, timeout=3.0):
            if path == "/song/metrics":
                return {"ok": True, "width": 1920, "height": 1080, "ref": 100, "widths": [60] * len(payload["chars"])}
            calls.append(payload)
            raise songs.AgentError("unprocessable", 502, agent_status=422)

        with patch("app.display_agent.call", refuses):
            answer = await songs.show_slide(fake_request(service), "a.foil", 1)
        self.assertEqual(answer.status_code, 502)
        self.assertEqual(["layout" in p for p in calls], [True, False])   # one retry, no older levels
        self.assertNotIn((IP, "song_level"), service.agent_memory)


class MetricsCacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_fonts_typed_in_the_preview_do_not_pile_up(self):
        service = service_with(SHORT)
        request = fake_request(service)
        target = service.settings.targets[0]
        saved = service.settings.font
        with patch("app.display_agent.call", AgentStub()):
            await songs._screen_for(request, target, service.settings, {"a"})
            for typed in ("C", "Ca", "Cal", "Cali", "Calib", "Calibr", "Calibri"):
                settings = service.settings.model_copy(update={"font": typed})
                await songs._screen_for(request, target, settings, {"a"})
        fonts = [key[3] for key in service.metrics if key[:3] == (target.ip, target.monitor, service.settings.fullscreen)]
        self.assertLessEqual(len(fonts), songs.METRICS_FONTS)
        self.assertIn(saved, fonts)                         # the saved font stays
        self.assertEqual(fonts[-1], "Calibri")              # the newest stays
        self.assertNotIn("C", fonts)

    async def test_a_used_font_is_kept_over_older_ones(self):
        service = service_with(SHORT)
        request = fake_request(service)
        target = service.settings.targets[0]
        agent = AgentStub()

        def with_font(font):
            return service.settings.model_copy(update={"font": font})

        with patch("app.display_agent.call", agent):
            for font in ("A", "B", "C"):
                await songs._screen_for(request, target, with_font(font), {"a"})
            await songs._screen_for(request, target, with_font("A"), {"a"})   # used again
            await songs._screen_for(request, target, with_font("D"), {"a"})
        fonts = {key[3] for key in service.metrics}
        self.assertIn("A", fonts)
        self.assertNotIn("B", fonts)
        measured = [p["font"] for _ip, path, p in agent.calls if path == "/song/metrics"]
        self.assertEqual(measured, ["A", "B", "C", "D"])      # A was not measured twice


class SettingsPushTests(unittest.IsolatedAsyncioTestCase):
    async def test_new_settings_reach_the_screen_without_a_press(self):
        service = service_with(SHORT, TWO_STANZAS, "Kurz")
        agent = AgentStub()
        request = fake_request(service)
        with patch("app.display_agent.call", agent):
            await songs.show_slide(request, "a.foil", 3)        # title, 1, 2.1, [2.2], 3
            before = len(agent.shows())
            answer = await songs.set_song_settings(request, {"font_size": 40})
            self.assertTrue(answer["ok"])
            self.assertEqual(len(agent.shows()), before + 1)
            sent = agent.shows()[-1]
            self.assertEqual(sent["text"], service.slides("a.foil")[2]["text"])   # verse 2, now one slide
            self.assertLessEqual(sent["layout"]["px"], 40)
            self.assertEqual(service.live["slide"], 2)
            # Saving the same settings again sends nothing.
            await songs.set_song_settings(request, {"font_size": 40})
            self.assertEqual(len(agent.shows()), before + 1)

    async def test_settings_that_do_not_change_the_slide_send_nothing(self):
        service = service_with(SHORT, TWO_STANZAS)
        agent = AgentStub()
        request = fake_request(service)
        with patch("app.display_agent.call", agent):
            await songs.show_slide(request, "a.foil", 1)
            before = len(agent.shows())
            await songs.set_song_settings(request, {"library_path": "D:\\Lieder"})
        self.assertEqual(len(agent.shows()), before)

    async def test_nothing_is_sent_while_blank_or_hidden(self):
        service = service_with(SHORT, TWO_STANZAS)
        agent = AgentStub()
        request = fake_request(service)
        with patch("app.display_agent.call", agent):
            await songs.show_slide(request, "a.foil", 1)
            await songs.song_blank(request)
            before = len(agent.shows())
            await songs.set_song_settings(request, {"font_size": 40})
            self.assertEqual(len(agent.shows()), before)
            await songs.song_hide(request)
            await songs.set_song_settings(request, {"font_size": 50})
            self.assertEqual(len(agent.shows()), before + 1)    # only the hide
        self.assertTrue(service.live["song_id"] is None)


if __name__ == "__main__":
    unittest.main()
