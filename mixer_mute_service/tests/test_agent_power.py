"""Tests for the authenticated desktop-agent shutdown command sequence."""

import os
import io
import json
import unittest
from unittest.mock import patch

try:
    import tkinter  # noqa: F401
except ImportError:
    tkinter = None

if tkinter is not None:
    from desktop_agent import show_image_agent as agent


@unittest.skipIf(tkinter is None, "this Python has no tkinter; the desktop agent needs it")
class AgentShutdownTests(unittest.TestCase):
    def test_shutdown_runs_requested_commands_without_shell(self):
        with patch.dict(os.environ, {"USERNAME": "TargetUser"}), \
                patch.object(agent.subprocess, "Popen") as popen:
            agent._shutdown_agent_pc()

        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args_list[0].args[0], ["shutdown.exe", "/s", "/t", "10"])
        self.assertEqual(popen.call_args_list[1].args[0], [
            "taskkill.exe", "/F", "/FI", "USERNAME eq TargetUser", "/FI", "STATUS eq RUNNING",
        ])
        self.assertNotIn("shell", popen.call_args_list[0].kwargs)
        self.assertNotIn("shell", popen.call_args_list[1].kwargs)

    def test_power_handler_sends_ack_before_scheduling_shutdown(self):
        class FakeHandler:
            def __init__(self):
                self.wfile = io.BytesIO()
                self.status = None
                self.headers = {}

            def send_response(self, status):
                self.status = status

            def send_header(self, name, value):
                self.headers[name] = value

            def end_headers(self):
                pass

        handler = FakeHandler()
        with patch.object(agent, "_schedule_shutdown", side_effect=lambda: self.assertIn(
            b"shutdown_scheduled", handler.wfile.getvalue()
        )) as schedule:
            agent.DisplayHandler._handle_power(handler, {"action": "shutdown"})

        self.assertEqual(handler.status, 202)
        self.assertEqual(json.loads(handler.wfile.getvalue()), {
            "ok": True, "shutdown_scheduled": True, "delay_seconds": 10,
        })
        schedule.assert_called_once_with()

    def test_shutdown_is_delayed_until_after_http_response(self):
        with patch.object(agent.threading, "Timer") as timer:
            agent._schedule_shutdown()

        timer.assert_called_once_with(2, agent._shutdown_agent_pc)
        timer.return_value.start.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()