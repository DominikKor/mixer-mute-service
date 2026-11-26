"""Every element the page's scripts look up by id exists.

The scripts (app/static/js/*.js) fetch elements with $("id") or
document.getElementById("id"). A key or menu removed from index.html while a
script still binds to it throws at load time and leaves the rest of the page
dead. This test finds every such id in the scripts and checks that index.html
has it, or that a script creates it (``x.id = "..."``). It also checks that
each section-head "⋮" (data-panel-menu) names a menu that exists, and that
every "⋮" key in index.html opens a menu.
"""

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
LOOKUP = re.compile(r'(?:\$|getElementById)\(\s*"([A-Za-z0-9_-]+)"\s*\)')
CREATED = re.compile(r'\.id\s*=\s*"([A-Za-z0-9_-]+)"')


class UiIdsTest(unittest.TestCase):
    def setUp(self):
        self.html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.scripts = {path.name: path.read_text(encoding="utf-8") for path in sorted((STATIC / "js").glob("*.js"))}
        self.ids = set(re.findall(r'\bid="([^"]+)"', self.html))
        for source in self.scripts.values():
            self.ids.update(CREATED.findall(source))

    def test_every_looked_up_id_exists(self):
        missing = sorted({f"{name}: {element_id}" for name, source in self.scripts.items()
                          for element_id in LOOKUP.findall(source) if element_id not in self.ids})
        self.assertEqual(missing, [])

    def test_panel_menus_exist(self):
        menus = re.findall(r'data-panel-menu="([^"]+)"', self.html)
        self.assertTrue(menus)
        for menu in menus:
            self.assertIn(menu, self.ids)
            self.assertRegex(self.html, rf'id="{menu}" class="tile-menu panel-menu"')

    def test_menu_keys_in_markup_open_a_menu(self):
        # A "⋮" key in index.html is either a section head's (data-panel-menu)
        # or bound by its id in a script.
        for tag in re.findall(r'<button[^>]*class="menu-btn"[^>]*>', self.html):
            if "data-panel-menu" in tag:
                continue
            element_id = re.search(r'\bid="([^"]+)"', tag)
            self.assertIsNotNone(element_id, tag)
            self.assertTrue(any(f'$("{element_id.group(1)}")' in source for source in self.scripts.values()), tag)


if __name__ == "__main__":
    unittest.main()
