"""Arrangement of the sections on the page (app/static/js/app.js:
layoutSectionRows, sectionRowsAfterMove, keptInOrder), run in quickjs.

The rows set width and column of every section; sections move up into the
space under shorter ones (masonry) but never change column, a row stays level
when its sections start less than SECTION_LEVEL_PX apart, and every arrow
moves its section visibly in its direction: checked on many random
arrangements and heights. Skips without quickjs; with
REQUIRE_TK=1 (CI) a missing quickjs is an error.
"""

import json
import os
import random
import re
import unittest
from pathlib import Path

try:
    import quickjs
except ImportError:  # pragma: no cover - only where quickjs is not installed
    if os.environ.get("REQUIRE_TK") == "1":  # CI: never skip silently
        raise
    quickjs = None

APP_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "app.js"
CONSTANTS = ("SECTION_ROW_MAX", "SECTION_GRID_COLUMNS", "SECTION_GRID_ROW_PX", "SECTION_LEVEL_PX")
FUNCTIONS = ("layoutSectionRows", "sectionNeighbour", "sectionRowsAfterMove", "keptInOrder")
SECTIONS = ["desktop", "cameras", "channels", "program", "songs", "lights", "groups", "companion"]
DIRS = ("left", "up", "down", "right")


@unittest.skipIf(quickjs is None, "needs quickjs")
class SectionLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = APP_JS.read_text(encoding="utf-8")
        parts = [re.search(rf"const {name} = .*?;", source).group(0) for name in CONSTANTS]
        parts += [re.search(rf"function {name}\(.*?\n\}}\n", source, re.S).group(0) for name in FUNCTIONS]
        cls.js = quickjs.Context()
        cls.js.eval("\n".join(parts))
        cls.level = json.loads(cls.js.eval("JSON.stringify(SECTION_LEVEL_PX / SECTION_GRID_ROW_PX)"))

    def call(self, expression):
        return json.loads(self.js.eval(f"JSON.stringify({expression})"))

    def layout(self, rows, heights, single=False):
        return self.call(f"layoutSectionRows({json.dumps(rows)}, s => ({json.dumps(heights)})[s], "
                         f"{{ single: {json.dumps(single)}, level: {self.level} }})")

    def move(self, rows, section, direction, layout):
        return self.call(f"sectionRowsAfterMove({json.dumps(rows)}, {json.dumps(section)}, "
                         f"{json.dumps(direction)}, {json.dumps(layout)})")

    def assert_no_overlap(self, layout):
        boxes = list(layout.values())
        for i, a in enumerate(boxes):
            self.assertTrue(0 <= a["start"] and a["start"] + a["span"] <= 6 and a["bottom"] > a["top"] >= 0)
            for b in boxes[i + 1:]:
                columns = a["start"] < b["start"] + b["span"] and b["start"] < a["start"] + a["span"]
                self.assertFalse(columns and a["top"] < b["bottom"] and b["top"] < a["bottom"], (a, b))

    # -- the reviewer's cases ------------------------------------------------

    def test_short_section_gets_the_next_ones_under_it(self):
        rows = [["channels", "program"], ["groups", "lights"], ["songs", "companion"]]
        h = {"channels": 130, "program": 660, "groups": 60, "lights": 400, "songs": 70, "companion": 500}
        layout = self.layout(rows, h)
        self.assertEqual(layout["groups"]["top"], 130)            # right under Channels, not under Programm
        self.assertEqual(layout["songs"]["top"], 190)
        self.assertEqual((layout["groups"]["start"], layout["lights"]["start"]), (0, 3))
        self.assert_no_overlap(layout)

    def test_a_pair_stays_level_and_in_its_columns(self):
        # The left column is 28 px shorter: Channels stays right of Program, level with it.
        rows = [["desktop", "cameras"], ["program", "channels"]]
        layout = self.layout(rows, {"desktop": 86, "cameras": 100, "program": 600, "channels": 120})
        self.assertEqual((layout["program"]["start"], layout["channels"]["start"]), (0, 3))
        self.assertEqual(layout["program"]["top"], layout["channels"]["top"])
        self.assertEqual(layout["program"]["top"], 100)

    def test_thirds_under_halves_only_the_straddling_one_waits(self):
        # Songs straddles the step between the halves: it starts under Program;
        # Groups still moves up under Channels, so no half column stays empty.
        rows = [["program", "channels"], ["lights", "songs", "groups"]]
        layout = self.layout(rows, {"program": 650, "channels": 125, "lights": 300, "songs": 60, "groups": 40})
        self.assertEqual([layout[s]["top"] for s in ("lights", "songs", "groups")], [650, 650, 125])
        self.assertEqual([layout[s]["start"] for s in ("lights", "songs", "groups")], [0, 2, 4])
        rows = [["channels", "program"], ["lights", "songs", "groups"]]
        layout = self.layout(rows, {"program": 650, "channels": 125, "lights": 300, "songs": 60, "groups": 40})
        self.assertEqual([layout[s]["top"] for s in ("lights", "songs", "groups")], [125, 650, 650])

    def test_up_moves_up_and_leaves_the_other_column_alone(self):
        rows = [["program", "channels"], ["lights", "songs"]]
        h = {"program": 660, "channels": 130, "lights": 400, "songs": 300}
        before = self.layout(rows, h)
        moved = self.move(rows, "songs", "up", before)
        self.assertEqual(moved, [["program", "songs"], ["lights", "channels"]])
        after = self.layout(moved, h)
        self.assertLess(after["songs"]["top"], before["songs"]["top"])
        self.assertEqual(after["lights"], before["lights"])

    def test_edges_give_the_whole_width(self):
        rows = [["program", "channels"], ["lights"]]
        layout = self.layout(rows, {"program": 600, "channels": 100, "lights": 80})
        self.assertEqual(self.move(rows, "channels", "up", layout), [["channels"], ["program"], ["lights"]])
        self.assertIsNone(self.move(rows, "lights", "down", layout))   # alone in the last row
        self.assertEqual(self.move(rows, "lights", "right", layout), [["program", "channels", "lights"]])
        self.assertIsNone(self.move(rows, "program", "left", layout))

    def test_only_out_of_order_sections_move_in_the_page(self):
        a = ["desktop", "cameras", "channels", "program"]
        kept = self.call(f"[...keptInOrder({json.dumps(a)}, {json.dumps(['desktop', 'program', 'channels', 'cameras'])})]")
        self.assertEqual(len(kept), 2)                            # a swap moves two sections
        self.assertIn("desktop", kept)
        self.assertEqual(len(self.call(f"[...keptInOrder({json.dumps(a)}, "
                                       f"{json.dumps(['cameras', 'desktop', 'channels', 'program'])})]")), 3)

    # -- every arrow, many arrangements ----------------------------------------

    def test_every_arrow_moves_visibly_its_way(self):
        rng = random.Random(6400)
        checked = 0
        for case in range(300):
            names = SECTIONS[:]
            rng.shuffle(names)
            rows, i = [], 0
            while i < len(names):
                k = rng.choice((1, 2, 2, 3))
                rows.append(names[i:i + k])
                i += k
            heights = {s: rng.choice((rng.randint(10, 90), rng.randint(90, 250), rng.randint(250, 700)))
                       for s in names}
            single = case % 5 == 0
            before = self.layout(rows, heights, single)
            self.assert_no_overlap(before)
            for row in rows:  # a row keeps its order from left to right, in its own columns
                span = 6 if single else 6 // len(row)
                self.assertEqual([before[s]["start"] for s in row],
                                 [0] * len(row) if single else [j * span for j in range(len(row))])
            for section in names:
                for direction in DIRS:
                    moved = self.move(rows, section, direction, before)
                    if moved is None:
                        continue
                    self.assertEqual(sorted(sum(moved, [])), sorted(names))
                    self.assertTrue(all(0 < len(r) <= 3 for r in moved))
                    after = self.layout(moved, heights, single)
                    self.assert_no_overlap(after)
                    b, a = before[section], after[section]
                    where = (rows, section, direction, single)
                    if direction == "up":
                        self.assertTrue(a["top"] < b["top"] or (a["top"] == b["top"] and a["span"] > b["span"]), where)
                    elif direction == "down":
                        self.assertTrue(a["top"] > b["top"] or (a["top"] == b["top"] and a["span"] > b["span"]), where)
                    elif not single:
                        centre = lambda box: box["start"] + box["span"] / 2  # noqa: E731
                        self.assertTrue(centre(a) < centre(b) if direction == "left" else centre(a) > centre(b),
                                        where)
                        if [section] in rows:  # joins the row above: never lower than before
                            self.assertLessEqual(a["top"], b["top"], where)
                    checked += 1
        self.assertGreater(checked, 3000)


if __name__ == "__main__":
    unittest.main()
