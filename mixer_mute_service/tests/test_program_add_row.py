"""The dashed "+ Programmpunkt hinzufügen" row at the end of the program list
(app/static: index.html, js/program.js, css/app.css).

Verifies: it sits after the program table (outside the rows, which
renderProgramItems keeps in place by index); it and the head key "+
Programmpunkt" run the same code (startProgramDraft); renderProgramItems
shows or hides it (hidden while a new row is open) and with no rows it says
how else to start; it is hidden while arranging the sections; it is at least
as high as the tap rules ask (44 px).

Run from mixer_mute_service:  python -m tests.test_program_add_row
"""

import re
import unittest
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
HTML = (STATIC / "index.html").read_text(encoding="utf-8")
PROGRAM_JS = (STATIC / "js" / "program.js").read_text(encoding="utf-8")
CSS = (STATIC / "css" / "app.css").read_text(encoding="utf-8")


def function_body(source: str, name: str) -> str:
    return re.search(rf"function {name}\(.*?\n\}}\n", source, re.S).group(0)


class ProgramAddRowTest(unittest.TestCase):
    def test_after_the_table(self):
        table_end = HTML.index('<tbody id="program-items"></tbody>')
        add = HTML.index('id="program-add-row"')
        self.assertLess(table_end, add)
        self.assertLess(HTML.index("</table>", table_end), add)
        self.assertIn('id="program-add-row-hint"', HTML)
        self.assertRegex(HTML, r'<button type="button" id="program-add-row" class="ghost-add ghost-row program-add-row" data-ghost-add="program" hidden>')

    def test_same_code_as_the_head_key(self):
        self.assertIn('$("program-add-btn").addEventListener("click", startProgramDraft);', PROGRAM_JS)
        # The row is a placeholder (js/menus.js): "program" runs startProgramDraft.
        menus = (STATIC / "js" / "menus.js").read_text(encoding="utf-8")
        self.assertIn("program: () => startProgramDraft(),", menus)
        self.assertIn("PROGRAM_DRAFT = true;", function_body(PROGRAM_JS, "startProgramDraft"))

    def test_shown_by_the_list(self):
        self.assertIn("renderProgramAddRow();", function_body(PROGRAM_JS, "renderProgramItems"))
        body = function_body(PROGRAM_JS, "renderProgramAddRow")
        self.assertIn('$("program-add-row").hidden = PROGRAM_DRAFT;', body)
        self.assertIn('$("program-add-row-hint").hidden = PROGRAM_ITEMS.length > 0;', body)

    def test_style(self):
        # The shared placeholder look (dashed, see-through), as high as a closed row.
        shared = re.search(r"(?m)^button\.ghost-add \{(.*?)\}", CSS, re.S).group(1)
        self.assertIn("dashed", shared)
        self.assertIn("background: var(--ghost-bg)", shared)
        rule = re.search(r"button\.ghost-add\.program-add-row \{(.*?)\}", CSS, re.S).group(1)
        self.assertGreaterEqual(int(re.search(r"min-height: (\d+)px", rule).group(1)), 44)
        self.assertIn("body.arranging button.ghost-add { display: none; }", CSS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
