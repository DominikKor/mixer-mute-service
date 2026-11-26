"""Arrangement of the sections on the page (app/static/js/app.js), run in
quickjs: a gallery where the user sets only order and width (⅓, ½, ⅔,
whole) and every section drops into the place of its width where it can start
highest (masonry). Checked on many random arrangements: nothing overlaps,
every section is as high as its columns allow, remembered places are kept
(always during a service, otherwise unless another place is much higher),
the arrows and the width key always do something and can always be undone
(no "row" to get stuck in), and saved rows from earlier versions are taken
over. Skips without quickjs; with REQUIRE_TK=1 (CI) a missing quickjs is an
error.
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
CONSTANTS = ("SECTION_GRID_COLUMNS", "SECTION_GRID_ROW_PX", "SECTION_WIDTHS", "SECTION_STICK_PX")
FUNCTIONS = ("sectionSpan", "sectionStarts", "layoutSections", "sectionOrderAfterMove", "nextSectionWidth",
             "sectionArrangementFromPrefs", "keptInOrder")
SECTIONS = ["desktop", "cameras", "channels", "program", "songs", "lights", "groups", "companion"]
VIEWS = ("three", "two", "one")


@unittest.skipIf(quickjs is None, "needs quickjs")
class SectionLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = APP_JS.read_text(encoding="utf-8")
        parts = [re.search(rf"const {name} = .*?;", source).group(0) for name in CONSTANTS]
        parts += [re.search(rf"function {name}\(.*?\n\}}\n", source, re.S).group(0) for name in FUNCTIONS]
        cls.js = quickjs.Context()
        cls.js.eval("\n".join(parts))
        cls.stick = json.loads(cls.js.eval("JSON.stringify(SECTION_STICK_PX / SECTION_GRID_ROW_PX)"))

    def call(self, expression):
        return json.loads(self.js.eval(f"JSON.stringify({expression})"))

    def layout(self, order, widths, heights, view="three", places=None, keep=False):
        return self.call(f"layoutSections({json.dumps(order)}, s => ({json.dumps(widths)})[s], "
                         f"s => ({json.dumps(heights)})[s], {{ view: {json.dumps(view)}, "
                         f"places: {json.dumps(places or {})}, stick: {self.stick}, keep: {json.dumps(keep)} }})")

    def move(self, order, section, direction, widths, heights, view="three"):
        return self.call(f"sectionOrderAfterMove({json.dumps(order)}, {json.dumps(section)}, {json.dumps(direction)}, "
                         f"next => layoutSections(next, s => ({json.dumps(widths)})[s], "
                         f"s => ({json.dumps(heights)})[s], {{ view: {json.dumps(view)} }}))")

    def spans(self, view):
        return {w: self.call(f"sectionSpan({w}, {json.dumps(view)})") for w in (2, 3, 4, 6)}

    def assert_gallery(self, order, widths, heights, view, layout):
        """No overlaps, allowed places, and every section as high as its columns allow."""
        spans = self.spans(view)
        skyline = [0] * 6
        for section in order:
            box = layout[section]
            self.assertEqual(box["span"], spans[widths[section]])
            self.assertIn(box["start"], self.call(f"sectionStarts({box['span']})"))
            columns = range(box["start"], box["start"] + box["span"])
            self.assertEqual(box["top"], max(skyline[c] for c in columns), (section, layout))
            self.assertEqual(box["bottom"] - box["top"], heights[section])
            for c in columns:
                skyline[c] = box["bottom"]
        boxes = list(layout.values())
        for i, a in enumerate(boxes):
            for b in boxes[i + 1:]:
                columns = a["start"] < b["start"] + b["span"] and b["start"] < a["start"] + a["span"]
                self.assertFalse(columns and a["top"] < b["bottom"] and b["top"] < a["bottom"], (a, b))

    # -- the user's gallery -----------------------------------------------------

    def test_short_sections_stack_next_to_a_tall_one(self):
        order = ["program", "channels", "groups", "songs", "lights"]
        widths = {s: 3 for s in order}
        heights = {"program": 600, "channels": 120, "groups": 60, "songs": 70, "lights": 300}
        layout = self.layout(order, widths, heights)
        self.assertEqual([layout[s]["start"] for s in order], [0, 3, 3, 3, 3])
        self.assertEqual([layout[s]["top"] for s in ("channels", "groups", "songs", "lights")], [0, 120, 180, 250])
        self.assert_gallery(order, widths, heights, "three", layout)

    def test_thirds_and_two_thirds_fill_up(self):
        order = ["program", "channels", "groups", "songs", "desktop"]
        widths = {"program": 4, "channels": 2, "groups": 2, "songs": 2, "desktop": 6}
        heights = {"program": 500, "channels": 100, "groups": 100, "songs": 100, "desktop": 80}
        layout = self.layout(order, widths, heights)
        self.assertEqual((layout["program"]["start"], layout["channels"]["start"]), (0, 4))
        self.assertEqual([layout[s]["top"] for s in ("channels", "groups", "songs")], [0, 100, 200])
        self.assertEqual(layout["desktop"]["top"], 500)          # whole width: under everything

    def test_views(self):
        self.assertEqual(self.spans("three"), {2: 2, 3: 3, 4: 4, 6: 6})
        self.assertEqual(self.spans("two"), {2: 3, 3: 3, 4: 6, 6: 6})   # iPad upright: halves and whole
        self.assertEqual(self.spans("one"), {2: 6, 3: 6, 4: 6, 6: 6})
        order = ["a", "b", "c"]
        layout = self.layout(order, {"a": 2, "b": 3, "c": 4}, {"a": 5, "b": 7, "c": 9}, "one")
        self.assertEqual([(layout[s]["start"], layout[s]["top"]) for s in order], [(0, 0), (0, 5), (0, 12)])

    def test_remembered_places_keep_the_page_calm(self):
        order, widths = ["program", "channels", "songs"], {"program": 3, "channels": 3, "songs": 3}
        heights = {"program": 300, "channels": 100, "songs": 50}
        self.assertEqual(self.layout(order, widths, heights)["songs"]["start"], 3)
        # Songs was left (under Program); right is 200 rows higher: it moves...
        self.assertEqual(self.layout(order, widths, heights, places={"songs": 0})["songs"]["start"], 3)
        # ...but not during a service (keep), and not for a little.
        self.assertEqual(self.layout(order, widths, heights, places={"songs": 0}, keep=True)["songs"]["start"], 0)
        heights["program"] = 100 + self.stick
        self.assertEqual(self.layout(order, widths, heights, places={"songs": 0})["songs"]["start"], 0)
        # A place that does not fit the width any more is not kept.
        self.assertEqual(self.layout(order, {**widths, "songs": 4}, heights, places={"songs": 3},
                                     keep=True)["songs"]["start"], 0)

    def test_width_key_and_arrows_always_get_out_again(self):
        self.assertEqual([self.call(f"nextSectionWidth({w}, 'three')") for w in (2, 3, 4, 6)], [3, 4, 6, 2])
        self.assertEqual([self.call(f"nextSectionWidth({w}, 'two')") for w in (2, 3, 4, 6)], [4, 4, 2, 2])
        self.assertIsNone(self.call("nextSectionWidth(3, 'one')"))
        # The old trap: three side by side in the middle of the page.
        order = ["desktop", "cameras", "channels", "program", "songs", "lights"]
        widths = {**{s: 6 for s in order}, "channels": 2, "program": 2, "songs": 2}
        heights = {s: 100 for s in order}
        moved = self.move(order, "songs", "left", widths, heights)
        self.assertLess(moved.index("songs"), order.index("songs"))
        self.assertEqual(self.call("nextSectionWidth(2, 'three')"), 3)   # and its width can change

    def test_saved_rows_are_taken_over(self):
        known = SECTIONS
        rows = [["channels", "program"], ["lights", "songs", "groups"], ["desktop"], ["nope"], [], "x"]
        got = self.call(f"sectionArrangementFromPrefs({json.dumps({'sectionRows': rows})}, {json.dumps(known)})")
        self.assertEqual(got["order"], ["channels", "program", "lights", "songs", "groups", "desktop", "cameras",
                                        "companion"])
        self.assertEqual([got["widths"][s] for s in got["order"]], [3, 3, 2, 2, 2, 6, 6, 6])
        got = self.call(f"sectionArrangementFromPrefs({json.dumps({'sectionOrder': ['songs', 'desktop']})}, "
                        f"{json.dumps(known)})")
        self.assertEqual(got["order"][:3], ["songs", "desktop", "cameras"])
        self.assertEqual(set(got["widths"].values()), {6})
        saved = {"sectionArrangement": {"order": ["program", "program", "songs"], "widths": {"program": 4, "songs": 5}},
                 "sectionRows": [["desktop", "cameras"]]}
        got = self.call(f"sectionArrangementFromPrefs({json.dumps(saved)}, {json.dumps(known)})")
        self.assertEqual(got["order"][:3], ["program", "songs", "desktop"])
        self.assertEqual((got["widths"]["program"], got["widths"]["songs"]), (4, 6))   # 5 is no width
        self.assertEqual(self.call(f"sectionArrangementFromPrefs({{}}, {json.dumps(known)})")["order"], known)

    def test_only_out_of_order_sections_move_in_the_page(self):
        a = ["desktop", "cameras", "channels", "program"]
        kept = self.call(f"[...keptInOrder({json.dumps(a)}, {json.dumps(['desktop', 'program', 'channels', 'cameras'])})]")
        self.assertEqual(len(kept), 2)
        self.assertIn("desktop", kept)
        self.assertEqual(len(self.call(f"[...keptInOrder({json.dumps(a)}, "
                                       f"{json.dumps(['cameras', 'desktop', 'channels', 'program'])})]")), 3)

    # -- many arrangements --------------------------------------------------------

    def test_random_galleries(self):
        rng = random.Random(6400)
        checked = 0
        for case in range(150):
            order = SECTIONS[:]
            rng.shuffle(order)
            widths = {s: rng.choice((2, 3, 4, 6)) for s in order}
            heights = {s: rng.choice((rng.randint(20, 90), rng.randint(90, 250), rng.randint(250, 700)))
                       for s in order}
            view = VIEWS[case % 3]
            free = self.layout(order, widths, heights, view)
            self.assert_gallery(order, widths, heights, view, free)
            # Without remembered places each one takes the highest place (left on a tie).
            skyline = [0] * 6
            for section in order:
                span = free[section]["span"]
                tops = {s: max(skyline[s:s + span]) for s in self.call(f"sectionStarts({span})")}
                best = min(tops.values())
                self.assertEqual(free[section]["start"], min(s for s, t in tops.items() if t == best))
                for c in range(free[section]["start"], free[section]["start"] + span):
                    skyline[c] = free[section]["bottom"]
            # Remembered places, contents grown: kept during a service, still a gallery.
            places = {s: rng.choice(self.call(f"sectionStarts({free[s]['span']})")) for s in order}
            grown = {s: h + rng.randint(0, 200) for s, h in heights.items()}
            kept = self.layout(order, widths, grown, view, places, keep=True)
            self.assertEqual({s: kept[s]["start"] for s in order}, places)
            self.assert_gallery(order, widths, grown, view, kept)
            self.assert_gallery(order, widths, grown, view, self.layout(order, widths, grown, view, places))
            # Every arrow moves its section and changes the page; at the ends they are off.
            for section in order:
                for direction in ("left", "right"):
                    moved = self.move(order, section, direction, widths, heights, view)
                    at = order.index(section)
                    if moved is None:
                        if view == "one":
                            self.assertEqual(at, 0 if direction == "left" else len(order) - 1)
                        continue
                    self.assertEqual(sorted(moved), sorted(order))
                    self.assertTrue(moved.index(section) < at if direction == "left" else moved.index(section) > at)
                    self.assertNotEqual(self.layout(moved, widths, heights, view), free)
                    checked += 1
            # The width key comes back round (as seen in the view) in at most four presses.
            spans = self.spans(view)
            width = widths[order[0]]
            seen = [spans[width]]
            for _ in range(4):
                width = self.call(f"nextSectionWidth({width}, {json.dumps(view)})")
                if width is None:
                    break
                self.assertNotEqual(spans[width], seen[-1])          # every press changes the page
                seen.append(spans[width])
                if seen[-1] == seen[0]:
                    break
            self.assertTrue(view == "one" or (len(seen) > 2 and seen[-1] == seen[0]), seen)
        self.assertGreater(checked, 1000)


if __name__ == "__main__":
    unittest.main()
