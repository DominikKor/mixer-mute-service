"""When a program row's "HH:MM" is: the occurrence closest to now (yesterday, today or
tomorrow), and the rows of a run stay on one day (app/program_items.py
scheduled_occurrence, js/program.js programDue). No flipping at 12 hours, no "always the
next 24 hours". The cue's text: "in 12 min", "12 min zu spät", "in 1:05 h", "1:05 h zu spät".

Run from mixer_mute_service:  python -m tests.test_program_time
"""

import asyncio
import json
import os
import re
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from app.program_items import ProgramItemStore, scheduled_occurrence

try:
    import quickjs
except ImportError:  # pragma: no cover - only where quickjs is not installed
    if os.environ.get("REQUIRE_TK") == "1":  # CI: never skip silently
        raise
    quickjs = None

PROGRAM_JS = (Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "program.js").read_text(encoding="utf-8")
TZ = timezone(timedelta(hours=1))


def at(day: int, hour: int, minute: int = 0, second: int = 0) -> datetime:
    return datetime(2026, 11, day, hour, minute, second, tzinfo=TZ)


def minutes_late(time_text: str, now: datetime, anchor=None) -> float:
    return (now - scheduled_occurrence(time_text, now, anchor)).total_seconds() / 60


class ClosestOccurrenceTest(unittest.TestCase):
    def test_the_examples(self):
        self.assertEqual(minutes_late("10:30", at(10, 11, 0)), 30)  # 30 min zu spät
        self.assertEqual(minutes_late("10:00", at(10, 9, 50)), -10)  # in 10 min
        self.assertEqual(minutes_late("00:10", at(10, 23, 50)), -20)  # in 20 min, tomorrow's
        self.assertEqual(minutes_late("23:50", at(10, 0, 10)), 20)  # 20 min zu spät, yesterday's

    def test_the_closest_one_wins_not_the_next_24_hours_or_a_wrap_at_noon(self):
        # 10:00 at 21:00 is 11 h late (today), not "in 13 h"
        self.assertEqual(minutes_late("10:00", at(10, 21, 0)), 11 * 60)
        # 10:00 at 23:00 is in 11 h (tomorrow): closer than 13 h late
        self.assertEqual(minutes_late("10:00", at(10, 23, 0)), -11 * 60)
        # 12:30 late is never "+12:30 → wrapped to early"; 10:00 at 22:30 is 12:30 late, the other
        # occurrence is 11:30 away: the closest is tomorrow's
        self.assertEqual(minutes_late("10:00", at(10, 22, 30)), -(11 * 60 + 30))
        # exactly 12 h away counts as today (late)
        self.assertEqual(minutes_late("10:00", at(10, 22, 0)), 12 * 60)

    def test_the_day_is_exact_around_midnight(self):
        self.assertEqual(scheduled_occurrence("00:10", at(10, 23, 50)).date().day, 11)
        self.assertEqual(scheduled_occurrence("23:50", at(11, 0, 10)).date().day, 10)
        self.assertEqual(scheduled_occurrence("23:50", at(10, 23, 55)).date().day, 10)
        self.assertEqual(scheduled_occurrence("00:00", at(10, 0, 0)).date().day, 10)

    def test_an_anchor_keeps_the_rows_of_a_run_on_its_day(self):
        # A rehearsal at 22:30 of a 10:00 program: the first row ran at 22:30 (12:30 late).
        anchor = at(10, 10, 0)
        # without the anchor the next row (10:10 at 22:40) would flip to "tomorrow, in 11:30 h"
        self.assertEqual(minutes_late("10:10", at(10, 22, 40)), -(11 * 60 + 30))
        self.assertEqual(minutes_late("10:10", at(10, 22, 40), anchor), 12 * 60 + 30)
        # the evening rows of the same program are on that day too
        self.assertEqual(minutes_late("23:50", at(10, 22, 40), anchor), -70)

    def test_a_run_past_midnight_stays_together(self):
        # a program with 23:30 and 23:50 rows, 23:50 started at 00:05 (15 min late, yesterday's)
        anchor = scheduled_occurrence("23:50", at(11, 0, 5))
        self.assertEqual(anchor, at(10, 23, 50))
        self.assertEqual(minutes_late("23:55", at(11, 0, 10), anchor), 15)  # the next row: yesterday's 23:55
        self.assertEqual(minutes_late("23:30", at(11, 0, 10), anchor), 40)

    def test_a_row_without_a_time_is_an_error_for_the_rule_and_no_delay_for_the_store(self):
        with self.assertRaises(ValueError):
            scheduled_occurrence("", at(10, 10))


class MarkPlayedTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.store = ProgramItemStore(Path(self.dir.name) / "program.json")

    async def row(self, time_text: str, title: str = "x") -> dict:
        return await self.store.create({"time": time_text, "title": title})

    async def test_single_rows_use_the_closest_occurrence(self):
        row = await self.row("10:30")
        played = await self.store.mark_played(row["id"], at(10, 11, 0))
        self.assertEqual(played["last_delay_seconds"], 30 * 60)

    async def test_a_row_just_after_midnight_is_in_the_future(self):
        row = await self.row("00:10")
        played = await self.store.mark_played(row["id"], at(10, 23, 50))
        self.assertEqual(played["last_delay_seconds"], -20 * 60)

    async def test_rows_in_order_across_midnight(self):
        first, second, third = [await self.row(t) for t in ("23:30", "23:50", "23:58")]
        self.assertEqual((await self.store.mark_played(first["id"], at(10, 23, 35)))["last_delay_seconds"], 5 * 60)
        self.assertEqual((await self.store.mark_played(second["id"], at(11, 0, 5)))["last_delay_seconds"], 15 * 60)
        # the last row is the neighbour's day (yesterday): 23:58 at 00:10 is 12 min late, not "in 23:48 h"
        self.assertEqual((await self.store.mark_played(third["id"], at(11, 0, 10)))["last_delay_seconds"], 12 * 60)

    async def test_a_rehearsal_at_another_time_of_day_does_not_flip(self):
        rows = [await self.row(t) for t in ("10:00", "10:10", "10:20")]
        delays = [(await self.store.mark_played(rows[i]["id"], at(10, 22, 30 + i * 5)))["last_delay_seconds"] for i in range(3)]
        # 10:00 at 22:30 is closest tomorrow (in 11:30); its neighbours stay on that day: 10:10 at 22:35, 10:20 at 22:40
        self.assertEqual(delays, [-(11 * 3600 + 30 * 60), -(11 * 3600 + 35 * 60), -(11 * 3600 + 40 * 60)])

    async def test_yesterdays_run_is_no_anchor(self):
        first = await self.row("10:00")
        second = await self.row("10:10")
        await self.store.mark_played(first["id"], at(9, 22, 0))  # a start on another day
        played = await self.store.mark_played(second["id"], at(10, 10, 15))
        self.assertEqual(played["last_delay_seconds"], 5 * 60)

    async def test_replaying_a_row_ignores_its_own_earlier_start(self):
        row = await self.row("10:00")
        await self.store.mark_played(row["id"], at(10, 22, 30))
        played = await self.store.mark_played(row["id"], at(10, 10, 5))
        self.assertEqual(played["last_delay_seconds"], 5 * 60)


@unittest.skipIf(quickjs is None, "quickjs not installed")
class CueTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        def body(name: str) -> str:
            return re.search(rf"function {name}\(.*?\n\}}\n", PROGRAM_JS, re.S).group(0)
        cls.js = quickjs.Context()
        cls.js.eval("let PROGRAM_ITEMS = []; let PROGRAM_STATUS = {};")
        cls.js.eval(body("playedToday"))
        for name in ("programDue", "programRunAnchor", "programCountdownText"):
            cls.js.eval(body(name))

    def text(self, time_text: str, now: tuple, anchor=None) -> str:
        y, mo, d, h, mi = now
        arg = "null" if anchor is None else f"new Date({anchor[0]}, {anchor[1] - 1}, {anchor[2]}, {anchor[3]}, {anchor[4]})"
        return self.js.eval(
            f"(() => {{ const now = new Date({y}, {mo - 1}, {d}, {h}, {mi}, 0); "
            f"const due = programDue({json.dumps(time_text)}, now, {arg}); "
            f"return programCountdownText(Math.round((due - now) / 1000)); }})()")

    def test_the_examples(self):
        self.assertEqual(self.text("10:30", (2026, 11, 10, 11, 0)), "30 min zu spät")
        self.assertEqual(self.text("10:00", (2026, 11, 10, 9, 50)), "in 10 min")
        self.assertEqual(self.text("00:10", (2026, 11, 10, 23, 50)), "in 20 min")
        self.assertEqual(self.text("23:50", (2026, 11, 11, 0, 10)), "20 min zu spät")

    def test_formats(self):
        for diff, text in ((-720, "12 min zu spät"), (720, "in 12 min"), (3900, "in 1:05 h"), (-3900, "1:05 h zu spät"),
                           (3600, "in 1:00 h"), (252, "in 4:12 min"), (-252, "4:12 min zu spät"), (0, "in 0:00 min"),
                           (599, "in 9:59 min"), (600, "in 10 min"), (40000, "in 11:06 h")):
            self.assertEqual(self.js.eval(f"programCountdownText({diff})"), text, diff)

    def test_no_flip_at_12_hours(self):
        self.assertEqual(self.text("10:00", (2026, 11, 10, 21, 0)), "11:00 h zu spät")
        self.assertEqual(self.text("10:00", (2026, 11, 10, 23, 0)), "in 11:00 h")
        self.assertEqual(self.text("10:00", (2026, 11, 10, 22, 0)), "12:00 h zu spät")

    def test_anchor_keeps_the_run_on_one_day(self):
        self.assertEqual(self.text("10:10", (2026, 11, 10, 22, 40)), "in 11:30 h")
        self.assertEqual(self.text("10:10", (2026, 11, 10, 22, 40), (2026, 11, 10, 10, 0)), "12:30 h zu spät")

    def test_run_anchor_comes_from_the_current_row(self):
        now = datetime.now().astimezone()
        played = now.replace(microsecond=0)
        self.js.eval(f"PROGRAM_ITEMS = [{{id: 'a', last_played_at: {json.dumps(played.isoformat())}, last_delay_seconds: 600}}];"
                     "PROGRAM_STATUS = {current_id: 'a'};")
        self.assertEqual(self.js.eval("programRunAnchor().getTime()"), (played.timestamp() - 600) * 1000)
        self.js.eval("PROGRAM_STATUS = {current_id: null};")
        self.assertIsNone(self.js.eval("programRunAnchor()"))
        self.js.eval("PROGRAM_ITEMS = [{id: 'a', last_played_at: '2020-01-01T10:00:00+01:00', last_delay_seconds: 0}]; PROGRAM_STATUS = {current_id: 'a'};")
        self.assertIsNone(self.js.eval("programRunAnchor()"))  # not today's

    def test_cue_has_no_countdown_for_a_row_without_a_time(self):
        self.assertIn("a row without a time", PROGRAM_JS)  # updateProgramCue clears the text for it
        self.assertRegex(PROGRAM_JS, r"if \(!/\^\\d\{2\}:\\d\{2\}\$/\.test\(next\.time \|\| \"\"\)\)")


if __name__ == "__main__":
    unittest.main(verbosity=2)
