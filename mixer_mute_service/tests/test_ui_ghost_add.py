"""Every "+ … hinzufügen" placeholder opens its create flow.

The placeholders (index.html and the scripts' ghostAddButton / ghostAddHtml
calls) name their flow with data-ghost-add; js/menus.js maps each name to a
function in GHOST_ADD_FLOWS. This checks, from the source alone:
- every placeholder's name has a flow, and every flow is used;
- every flow calls a function that a script declares at top level;
- the flows reach the create flows they stand for;
- the placeholders are drawn by the shared class (ghost-add) only.
"""

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"

EXPECTED = {
    "profile": "openProfileDialog",        # Desktop-Taste
    "shortcut": "openShortcutDialog",      # Shortcut (Desktop)
    "program": "startProgramDraft",        # Programmpunkt
    "person": "openPersonCreateRow",       # Mute Gruppe
    "preset": "openLightSceneCreate",      # Licht-Preset
    "category": "openCompanionCategoryDialog",  # Steuerung: Kategorie
    "key": "openCompanionButtonDialog",    # Steuerung: Taste
}


class GhostAddTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.scripts = {p.name: p.read_text(encoding="utf-8") for p in sorted((STATIC / "js").glob("*.js"))}
        menus = cls.scripts["menus.js"]
        block = re.search(r"const GHOST_ADD_FLOWS = \{(.*?)\n\};", menus, re.S)
        assert block, "GHOST_ADD_FLOWS not found in js/menus.js"
        cls.flows = dict(re.findall(r"^\s*(\w+):\s*(?:\(\)|\w+)\s*=>\s*(\w+)\(", block.group(1), re.M))
        cls.declared = {name for source in cls.scripts.values()
                        for name in re.findall(r"^(?:async )?function (\w+)\(", source, re.M)}
        used = set(re.findall(r'data-ghost-add="(\w+)"', cls.html))
        for source in cls.scripts.values():
            used.update(re.findall(r'ghostAdd(?:Button|Html)\(\s*"(\w+)"', source))
        cls.used = used

    def test_each_placeholder_has_its_flow(self):
        self.assertEqual(self.flows, EXPECTED)
        self.assertEqual(self.used, set(EXPECTED))

    def test_flows_call_declared_functions(self):
        for kind, function in self.flows.items():
            with self.subTest(kind=kind):
                self.assertIn(function, self.declared)

    def test_one_shared_look(self):
        # The old one-off add tiles are gone; every placeholder carries ghost-add.
        self.assertNotIn("display-add", self.html)
        for name, source in self.scripts.items():
            self.assertNotIn('"display-add"', source, name)
        for tag in re.findall(r"<button[^>]*data-ghost-add=[^>]*>", self.html):
            self.assertIn("ghost-add", re.search(r'class="([^"]*)"', tag).group(1).split(), tag)
        css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
        self.assertRegex(css, r"(?m)^button\.ghost-add \{")
        self.assertRegex(css, r"body\.arranging button\.ghost-add \{ display: none; \}")


if __name__ == "__main__":
    unittest.main()
