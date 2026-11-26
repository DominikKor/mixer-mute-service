"""Stage light keepalive (app/native_controls.py): like Companion's
generic-artnet module, the service resends its stage frame every second
once it drives the stage, because the node (or the fixtures behind it)
drops to black when frames stop. The sender is faked: nothing goes onto the
network.
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import native_controls as nc
from app.native_controls import NativeControlService


class Recorder:
    def __init__(self):
        self.frames = []
        self.fail = None

    def __call__(self, host, universe, values):
        if self.fail is not None:
            raise self.fail
        self.frames.append((host, universe, list(values)))


async def until(predicate, timeout=3.0):
    """Wait for an event instead of a fixed time (robust on a busy CI)."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not predicate():
        if loop.time() > end:
            raise AssertionError("condition not reached in time")
        await asyncio.sleep(0.005)


class _KeepaliveCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.state_path = Path(self.temp_dir.name) / "native-state.json"
        self.sent = Recorder()
        for target, value in (("_send_artnet_frame", self.sent),
                              ("ARTNET_KEEPALIVE_SECONDS", 0.05),
                              ("ARTNET_FRAME_SECONDS", 0.005)):
            patcher = patch.object(nc, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def service(self):
        controls = NativeControlService(self.state_path)
        controls.start_keepalive()
        self.addAsyncCleanup(controls.stop_keepalive)
        return controls


class KeepaliveTests(_KeepaliveCase):
    async def test_silent_until_the_first_stage_command(self):
        # A new install next to a still-used Companion must not fight it.
        controls = await self.service()
        await asyncio.sleep(0.2)
        self.assertEqual(self.sent.frames, [])
        self.assertFalse(controls.keepalive_status()["active"])

    async def test_frame_is_resent_after_a_command(self):
        controls = await self.service()
        await controls.set_light_settings({"fade_ms": 0})
        await controls.press({"type": "artnet-zone-toggle", "zone": "left"})
        await until(lambda: len(self.sent.frames) >= 4)
        frames = [values for _, _, values in self.sent.frames]
        self.assertGreaterEqual(len(frames), 4)
        self.assertTrue(all(values == frames[0] for values in frames))
        self.assertEqual(frames[0][0:3], [255, 255, 50])    # dimmer, warm, cold of the left zone
        self.assertTrue(all(host == "192.168.188.48" and universe == 0 for host, universe, _ in self.sent.frames))
        self.assertEqual(controls.keepalive_status(), {"active": True, "running": True, "error": None})

    async def test_restart_resumes_the_stage_at_once(self):
        controls = NativeControlService(self.state_path)
        await controls.set_light_settings({"fade_ms": 0})
        await controls.set_stage({"right": 120})
        self.assertTrue(json.loads(self.state_path.read_text())["stage_active"])
        self.sent.frames.clear()
        patcher = patch.object(nc, "ARTNET_KEEPALIVE_SECONDS", 10.0)  # only "at once" can pass
        patcher.start()
        self.addCleanup(patcher.stop)
        restarted = await self.service()
        for _ in range(100):  # the first frame goes out at once, not after a second
            if self.sent.frames:
                break
            await asyncio.sleep(0.005)
        values = self.sent.frames[0][2]
        self.assertEqual(values[12:15], [120, 255, 50])
        self.assertEqual(restarted.stage_state()["zones"]["right"], 120)

    async def test_no_old_frame_in_the_middle_of_a_fade(self):
        controls = await self.service()
        await controls.set_light_settings({"fade_ms": 0})
        await controls.set_stage({"left": 0})
        patcher = patch.object(nc, "ARTNET_KEEPALIVE_SECONDS", 0.001)  # would fire between every fade step
        patcher.start()
        self.addCleanup(patcher.stop)
        await controls.set_light_settings({"fade_ms": 400})
        self.sent.frames.clear()
        await controls.set_stage({"left": 200})
        levels = [values[0] for _, _, values in self.sent.frames]
        self.assertGreaterEqual(len(levels), 10)           # 10 fade steps (+ keepalive after it)
        self.assertEqual(levels, sorted(levels))         # never back to the start value
        self.assertEqual(levels[-1], 200)

    async def test_send_failure_is_reported_and_retried(self):
        controls = await self.service()
        await controls.set_light_settings({"fade_ms": 0})
        await controls.set_stage({"left": 80})
        attempts = []
        self.sent.fail = OSError("network down")
        original = nc._send_artnet_frame

        def counting(*args):
            attempts.append(args)
            return original(*args)

        with patch.object(nc, "_send_artnet_frame", counting), \
                self.assertLogs("mixer.native_controls", "WARNING") as logs:
            await until(lambda: len(attempts) >= 3)        # retried, not given up
        self.assertEqual(len(logs.records), 1)            # logged once, not every second
        self.assertIn("network down", controls.keepalive_status()["error"])
        self.sent.fail = None
        await until(lambda: controls.keepalive_status()["error"] is None)
        await until(lambda: self.sent.frames and self.sent.frames[-1][2][0] == 80)

    async def test_failed_fade_resends_the_last_good_state(self):
        controls = await self.service()
        await controls.set_light_settings({"fade_ms": 0})
        await controls.set_stage({"left": 60})
        self.sent.fail = OSError("network down")
        with self.assertRaises(OSError):
            await controls.set_stage({"left": 250})
        self.sent.fail = None
        await until(lambda: len(self.sent.frames) >= 2)
        self.assertEqual(self.sent.frames[-1][2][0], 60)  # what the page shows
        self.assertEqual(controls.stage_state()["zones"]["left"], 60)

    async def test_stop_ends_the_sending(self):
        controls = await self.service()
        await controls.set_light_settings({"fade_ms": 0})
        await controls.set_stage({"left": 10})
        await controls.stop_keepalive()
        count = len(self.sent.frames)
        await asyncio.sleep(0.15)
        self.assertEqual(len(self.sent.frames), count)
        self.assertFalse(controls.keepalive_status()["running"])


class KeepaliveRobustnessTests(_KeepaliveCase):
    """An unexpected error must neither end the keepalive (stage dark for
    good) nor break the service shutdown."""

    async def test_unexpected_error_is_logged_and_the_loop_goes_on(self):
        controls = await self.service()
        await controls.set_light_settings({"fade_ms": 0})
        await controls.set_stage({"left": 40})
        real_tick, calls = controls.keepalive_tick, []

        async def flaky():
            calls.append(1)
            if len(calls) <= 3:
                raise RuntimeError("boom")
            return await real_tick()

        controls.keepalive_tick = flaky
        with self.assertLogs("mixer.native_controls", "ERROR") as logs:
            await until(lambda: len(calls) >= 3)
        self.assertEqual(len(logs.records), 1)
        self.assertIn("boom", controls.keepalive_status()["error"])
        self.sent.frames.clear()
        await until(lambda: len(self.sent.frames) >= 2)   # sends again
        self.assertTrue(controls.keepalive_status()["running"])
        self.assertIsNone(controls.keepalive_status()["error"])

    async def test_stop_survives_a_crashed_task(self):
        controls = NativeControlService(self.state_path)

        async def crash():
            raise RuntimeError("crashed")

        controls._keepalive_task = asyncio.get_running_loop().create_task(crash())
        await asyncio.sleep(0)
        with self.assertLogs("mixer.native_controls", "ERROR"):
            await controls.stop_keepalive()               # must not raise
        self.assertIsNone(controls._keepalive_task)

    async def test_idle_loop_sleeps_a_full_interval(self):
        controls = NativeControlService(self.state_path)
        real_tick, calls = controls.keepalive_tick, []

        async def counted():
            calls.append(1)
            return await real_tick()

        controls.keepalive_tick = counted
        controls.start_keepalive()
        self.addAsyncCleanup(controls.stop_keepalive)
        await asyncio.sleep(0.2)                          # 4 intervals of 0.05 s
        self.assertLessEqual(len(calls), 5)               # not every 5 ms
        self.assertEqual(self.sent.frames, [])

    async def test_invalid_saved_frame_means_no_blackout_after_restart(self):
        self.state_path.write_text(json.dumps({"stage_active": True, "dmx": [0] * 10}), encoding="utf-8")
        with self.assertLogs("mixer.native_controls", "WARNING"):
            controls = NativeControlService(self.state_path)
        self.assertFalse(controls.keepalive_status()["active"])
        controls.start_keepalive()
        self.addAsyncCleanup(controls.stop_keepalive)
        await asyncio.sleep(0.15)
        self.assertEqual(self.sent.frames, [])            # no all-zero frame
        await controls.set_light_settings({"fade_ms": 0})
        await controls.set_stage({"left": 90})            # the next command takes over
        await until(lambda: len(self.sent.frames) >= 2)
        self.assertTrue(all(values[0] == 90 for _, _, values in self.sent.frames))


class CompanionParityTests(unittest.TestCase):
    """The stage as Companion drives it (generic-artnet connection "artnet"
    and the custom variables ChBueHe*/ChWBue*/ChKBue* in the export)."""

    def test_node_and_channels(self):
        self.assertEqual(nc.ARTNET_HOST, "192.168.188.48")
        self.assertEqual(nc.ARTNET_UNIVERSE, 0)
        self.assertEqual(nc.ARTNET_PORT, 6454)
        # Channels 1-based like Companion's set_complex ("1,255" is byte 0).
        self.assertEqual(nc.ARTNET_ZONES, {
            "left": {"white": 1, "warm": 2, "cold": 3},
            "center-left": {"white": 5, "warm": 6, "cold": 7},
            "center-right": {"white": 9, "warm": 10, "cold": 11},
            "right": {"white": 13, "warm": 14, "cold": 15},
        })
        self.assertEqual(nc.ARTNET_KEEPALIVE_SECONDS, 1.0)   # timer_slow 1000
        self.assertEqual(nc.ARTNET_FRAME_SECONDS, 0.04)      # timer_fast 40

    def test_default_levels(self):
        # ValWarmGesamt 255, ValKaltGesamt 50, StartHelligkeit 255, Steps 50, FadeTimeBue 1000
        with tempfile.TemporaryDirectory() as folder:
            controls = NativeControlService(Path(folder) / "state.json")
        self.assertEqual(controls._levels, {"start": 255, "steps": 50, "fade_ms": 1000})
        self.assertEqual(controls.stage_state()["fixtures"]["left"], {"on": False, "level": 0, "warm": 255, "cold": 50})


if __name__ == "__main__":
    unittest.main()
