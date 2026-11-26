"""Arrangement of the sections on the page (app/static/js/app.js), run in
quickjs: a gallery where the user sets only order and width (⅓, ½, ⅔,
whole) and every section drops into the place of its width where it can start
highest (masonry). Checked on many random arrangements: nothing overlaps,
every section is as high as its columns allow, remembered places are kept
(always during a service, otherwise unless another place is much higher),
the arrows and the width key always do something and can always be undone
(no "row" to get stuck in), a section whose content needs more room gets
the next wider width (its choice stays), and saved rows from earlier versions are taken
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
CONSTANTS = ("SECTION_GRID_COLUMNS", "SECTION_GRID_ROW_PX", "SECTION_WIDTHS", "SECTION_STICK_PX",
             "SECTION_SETTLE_MS", "SECTION_SETTLE_MAX_MS")
FUNCTIONS = ("sectionSpan", "sectionMinSpan", "sectionStarts", "layoutSections", "sectionOrderAfterMove", "nextSectionWidth",
             "sectionArrangementFromPrefs", "sectionWidthIn", "sectionPlacesMissing", "sectionPackRule",
             "sectionSettleDone",
             "keptInOrder")
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

    def layout(self, order, widths, heights, view="three", places=None, keep=False, min_span=None, stick=None):
        stick = self.stick if stick is None else stick
        return self.call(f"layoutSections({json.dumps(order)}, s => ({json.dumps(widths)})[s], "
                         f"s => ({json.dumps(heights)})[s], {{ view: {json.dumps(view)}, "
                         f"places: {json.dumps(places or {})}, stick: {stick}, keep: {json.dumps(keep)}, "
                         f"minSpan: {json.dumps(min_span or {})} }})")

    def move(self, order, section, direction, widths, heights, view="three"):
        return self.call(f"sectionOrderAfterMove({json.dumps(order)}, {json.dumps(section)}, {json.dumps(direction)}, "
                         f"next => layoutSections(next, s => ({json.dumps(widths)})[s], "
                         f"s => ({json.dumps(heights)})[s], {{ view: {json.dumps(view)} }}))")

    def spans(self, view):
        return {w: self.call(f"sectionSpan({w}, {json.dumps(view)})") for w in (2, 3, 4, 6)}

    def assert_gallery(self, order, widths, heights, view, layout, min_span=None):
        """No overlaps, allowed places, and every section as high as its columns allow."""
        min_span = min_span or {}
        skyline = [0] * 6
        for section in order:
            box = layout[section]
            self.assertEqual(box["span"], self.call(f"sectionSpan({widths[section]}, {json.dumps(view)}, "
                                                    f"{min_span.get(section, 0)})"))
            self.assertGreaterEqual(box["span"], min_span.get(section, 0))
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

    def test_contents_change_never_moves_a_column_outside_arranging(self):
        # During a service (no arrange action, "Anordnen" off, view settled)
        # packing keeps every section's columns, whatever the contents do:
        # Channels Kompakt <-> Voll, an empty Channels body for a moment, a
        # long program, a section folded.
        self.assertEqual(self.call("sectionPackRule({})"), {"stick": "keep", "save": True})
        rng = random.Random(1032)
        for case in range(120):
            order = SECTIONS[:]
            rng.shuffle(order)
            widths = {s: rng.choice((2, 3, 4, 6)) for s in order}
            heights = {s: rng.randint(20, 700) for s in order}
            view = ("three", "two")[case % 2]
            places = {s: box["start"] for s, box in self.layout(order, widths, heights, view).items()}
            for _ in range(3):
                changed = {s: rng.choice((h, rng.randint(10, 60), rng.randint(500, 1400))) for s, h in heights.items()}
                kept = self.layout(order, widths, changed, view, places, keep=True)
                self.assertEqual({s: kept[s]["start"] for s in order}, places, (order, changed))
                self.assert_gallery(order, widths, changed, view, kept)
        # The reviewer's case: Channels Voll -> Kompakt -> Voll moves nobody.
        order, widths = ["channels", "program", "companion"], {"channels": 3, "program": 3, "companion": 3}
        voll = {"channels": 900, "program": 200, "companion": 300}
        places = {s: b["start"] for s, b in self.layout(order, widths, voll).items()}
        for heights in ({**voll, "channels": 120}, {**voll, "channels": 0 + 1}, voll):
            kept = self.layout(order, widths, heights, places=places, keep=True)
            self.assertEqual({s: kept[s]["start"] for s in order}, places)

    def test_a_view_settles_on_its_first_full_state(self):
        # Loading: Channels still short, Kameras and Licht would go left under it.
        order = ["channels", "program", "cameras", "lights"]
        widths = {s: 3 for s in order}
        early = self.layout(order, widths, {"channels": 40, "program": 300, "cameras": 30, "lights": 200})
        self.assertEqual((early["cameras"]["start"], early["lights"]["start"]), (0, 0))
        # Still settling ("stick"), the loaded heights move them; nothing is saved meanwhile.
        loaded = self.layout(order, widths, {"channels": 1350, "program": 300, "cameras": 30, "lights": 200},
                             places={s: early[s]["start"] for s in order})
        self.assertEqual((loaded["cameras"]["start"], loaded["lights"]["start"]), (3, 3))
        rule = lambda **kw: self.call(f"sectionPackRule({json.dumps(kw)})")  # noqa: E731
        self.assertEqual(rule(settling=True), {"stick": "stick", "save": False})
        self.assertEqual(rule(free=True), {"stick": "stick", "save": True})              # arrange action
        self.assertEqual(rule(arranging=True), {"stick": "stick", "save": True})
        self.assertEqual(rule(dragging=True, free=True), {"stick": "keep", "save": False})
        # Done: the first mixer state, then quiet for SECTION_SETTLE_MS; at most SECTION_SETTLE_MAX_MS.
        quiet, most = self.call("[SECTION_SETTLE_MS, SECTION_SETTLE_MAX_MS]")
        done = lambda **kw: self.call(f"sectionSettleDone({json.dumps(kw)})")  # noqa: E731
        self.assertFalse(done(now=quiet + 10, start=0, lastChange=0, stateSeen=False))   # no mixer state yet
        self.assertFalse(done(now=quiet + 10, start=0, lastChange=20, stateSeen=True))   # heights still change
        self.assertTrue(done(now=quiet + 10, start=0, lastChange=10, stateSeen=True))
        self.assertTrue(done(now=most, start=0, lastChange=most - 1, stateSeen=False))
        # Which views settle: those lacking a saved place for any section.
        missing = lambda places: self.call(f"sectionPlacesMissing({json.dumps(order)}, {json.dumps(places)})")  # noqa: E731
        self.assertTrue(missing(None))
        self.assertTrue(missing({"channels": 0, "program": 3, "cameras": 0}))
        self.assertFalse(missing({s: early[s]["start"] for s in order}))

    def test_two_column_view_keeps_its_own_width(self):
        widths, view_widths = {"desktop": 6, "songs": 2}, {"two": {"songs": 6}}
        width_in = lambda view, s: self.call(  # noqa: E731
            f"sectionWidthIn({json.dumps(widths)}, {json.dumps(view_widths)}, {json.dumps(view)}, {json.dumps(s)})")
        self.assertEqual((width_in("two", "songs"), width_in("three", "songs")), (6, 2))
        self.assertEqual(width_in("two", "desktop"), 6)
        # iPad upright: "ganz" -> half -> "ganz" is 6 -> 3 -> 6 in the view, landscape untouched.
        seen, width = [], 6
        for _ in range(2):
            width = self.call(f"sectionSpan(nextSectionWidth({width}, 'two'), 'two')")
            seen.append(width)
        self.assertEqual(seen, [3, 6])
        got = self.call(f"sectionArrangementFromPrefs({json.dumps({'sectionArrangement': {'order': ['songs'], 'widths': {'songs': 2}, 'viewWidths': {'two': {'songs': 6, 'desktop': 4, 'nope': 3}}}})}, "
                        f"{json.dumps(SECTIONS)})")
        self.assertEqual(got["viewWidths"], {"two": {"songs": 6}})       # 4 is no width of that view
        self.assertEqual(got["widths"]["songs"], 2)

    def test_content_too_wide_gets_the_next_wider_width(self):
        span = lambda w, view, m: self.call(f"sectionSpan({w}, {json.dumps(view)}, {m})")  # noqa: E731
        self.assertEqual([span(2, "three", m) for m in (0, 2, 3, 4, 5, 6)], [2, 2, 3, 4, 6, 6])
        self.assertEqual([span(4, "three", m) for m in (0, 3, 5)], [4, 4, 6])
        self.assertEqual([span(2, "two", m) for m in (0, 3, 4)], [3, 3, 6])
        self.assertEqual(span(3, "one", 0), 6)
        # 1440 px page (1392 inside, 18 px gaps): a third is 452 px, Licht needs 470.
        col = (1392 - 5 * 18) / 6
        self.assertEqual(self.call(f"sectionMinSpan(470, {col}, 18)"), 3)
        self.assertEqual(self.call(f"sectionMinSpan(452, {col}, 18)"), 2)
        self.assertEqual(self.call(f"sectionMinSpan(0, {col}, 18)"), 0)
        self.assertEqual(self.call(f"sectionMinSpan(5000, {col}, 18)"), 6)
        # In the gallery: Licht takes half, the others stay thirds and still fill up.
        order = ["program", "lights", "channels", "groups"]
        widths = {s: 2 for s in order}
        heights = {"program": 300, "lights": 200, "channels": 80, "groups": 60}
        layout = self.layout(order, widths, heights, min_span={"lights": 3})
        self.assertEqual((layout["lights"]["start"], layout["lights"]["span"]), (3, 3))
        self.assertEqual([layout[s]["span"] for s in ("program", "channels", "groups")], [2, 2, 2])
        self.assert_gallery(order, widths, heights, "three", layout, {"lights": 3})
        # The width key skips what looks the same and is off when nothing changes.
        self.assertEqual(self.call("nextSectionWidth(2, 'three', 3)"), 4)
        self.assertEqual(self.call("nextSectionWidth(4, 'three', 3)"), 6)
        self.assertEqual(self.call("nextSectionWidth(6, 'three', 3)"), 2)
        self.assertIsNone(self.call("nextSectionWidth(2, 'three', 6)"))

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
        # Unknown and repeated names do not count; a longer row is split into threes.
        rows = [["channels", "nope"], ["program", "songs", "lights", "groups"], ["desktop", "channels", "cameras"]]
        got = self.call(f"sectionArrangementFromPrefs({json.dumps({'sectionRows': rows})}, {json.dumps(known)})")
        self.assertEqual([(s, got["widths"][s]) for s in got["order"][:7]],
                         [("channels", 6), ("program", 2), ("songs", 2), ("lights", 2), ("groups", 6),
                          ("desktop", 3), ("cameras", 3)])
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
            min_span = {s: rng.randint(1, 6) for s in rng.sample(order, rng.randint(0, 3))}
            widened = self.layout(order, widths, heights, view, min_span=min_span)
            self.assert_gallery(order, widths, heights, view, widened, min_span)
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
