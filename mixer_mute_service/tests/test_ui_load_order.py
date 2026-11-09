"""The page's scripts (app/static/js/*.js) are classic scripts loaded one
after another. Inside one file a function may be called before its
declaration (hoisting); across files it may not: code that runs while a file
loads must only reach functions of that file or earlier ones. This test
follows every call made at load time (also through other functions) and
fails if one reaches a later file. Handlers given to addEventListener,
setTimeout, poll(), .then() and observers run later and are not followed."""

import re
import unittest
from pathlib import Path

try:
    import tree_sitter_javascript
    from tree_sitter import Language, Parser
except ImportError:  # pragma: no cover - CI installs it (requirements-dev.txt)
    Parser = None

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
DEFERRED_CALLS = {"addEventListener", "setTimeout", "setInterval", "poll", "requestAnimationFrame",
                  "then", "catch", "finally", "observe"}
DEFERRED_NEW = {"ResizeObserver", "MutationObserver", "IntersectionObserver", "Promise"}
FUNCTIONS = ("arrow_function", "function_expression", "function", "generator_function")


def script_order() -> list:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    return [src.split("?")[0] for src in re.findall(r'<script\s+src="([^"]+)"', html)]


def identifiers(node, at_load: bool, out: set) -> None:
    """Identifiers used by ``node``; with ``at_load``, code handed to deferred
    APIs and function values stored for later are left out."""
    if node.type == "identifier":
        out.add(node.text.decode())
        return
    if at_load and node.type == "call_expression":
        fn = node.child_by_field_name("function")
        prop = fn.child_by_field_name("property") if fn is not None and fn.type == "member_expression" else fn
        if prop is not None and prop.text.decode() in DEFERRED_CALLS:
            if fn.type == "member_expression":
                identifiers(fn.child_by_field_name("object"), at_load, out)
            return
    if at_load and node.type == "new_expression":
        ctor = node.child_by_field_name("constructor")
        if ctor is not None and ctor.text.decode() in DEFERRED_NEW:
            return
    if at_load and node.type in FUNCTIONS and node.parent is not None \
            and node.parent.type in ("variable_declarator", "pair", "assignment_expression"):
        return
    for child in node.children:
        identifiers(child, at_load, out)


def load_order_problems(order: list) -> list:
    parser = Parser(Language(tree_sitter_javascript.language()))
    trees, declared_in, bodies = [], {}, {}
    for index, name in enumerate(order):
        tree = parser.parse((STATIC / name).read_bytes())
        trees.append((name, tree))
        for node in tree.root_node.children:
            if node.type == "function_declaration":
                fname = node.child_by_field_name("name").text.decode()
                declared_in[fname] = index
                bodies[fname] = node
    refs = {}

    def body_refs(fname):
        if fname not in refs:
            refs[fname] = set()
            identifiers(bodies[fname], False, refs[fname])
        return refs[fname]

    problems = []
    for index, (name, tree) in enumerate(trees):
        if tree.root_node.has_error:
            problems.append(f"{name}: does not parse")
        for node in tree.root_node.children:
            if node.type in ("function_declaration", "comment"):
                continue
            start = set()
            identifiers(node, True, start)
            seen, stack = set(), [(ident, (ident,)) for ident in start]
            while stack:
                ident, path = stack.pop()
                if ident in seen or ident not in declared_in:
                    continue
                seen.add(ident)
                if declared_in[ident] > index:
                    problems.append(f"{name}:{node.start_point[0] + 1}: calls {ident}() of "
                                    f"{order[declared_in[ident]]} while loading ({' -> '.join(path)})")
                    continue
                stack.extend((r, path + (r,)) for r in body_refs(ident))
    return problems


@unittest.skipIf(Parser is None, "tree-sitter not installed (pip install -r requirements-dev.txt)")
class LoadOrderTests(unittest.TestCase):
    def test_page_scripts(self):
        order = script_order()
        self.assertGreater(len(order), 1)
        self.assertEqual(order[-1], "js/main.js")
        self.assertEqual(load_order_problems(order), [])

    def test_no_name_declared_in_two_files(self):
        # Classic scripts share one global scope: a second top-level let,
        # const or class of the same name makes the browser drop the whole
        # later file (two branches once both added ICON_DOTS).
        parser = Parser(Language(tree_sitter_javascript.language()))
        seen, clashes = {}, []
        for name in script_order():
            tree = parser.parse((STATIC / name).read_bytes())
            for node in tree.root_node.children:
                names = []
                if node.type in ("lexical_declaration", "variable_declaration"):
                    names = [d.child_by_field_name("name").text.decode() for d in node.named_children
                             if d.type == "variable_declarator" and d.child_by_field_name("name").type == "identifier"]
                elif node.type in ("function_declaration", "class_declaration"):
                    names = [node.child_by_field_name("name").text.decode()]
                for ident in names:
                    if ident in seen and seen[ident] != name:
                        clashes.append(f"{ident}: {seen[ident]} and {name}")
                    seen.setdefault(ident, name)
        self.assertEqual(clashes, [])

    def test_check_finds_a_wrong_order(self):
        order = script_order()
        wrong = [order[-1]] + order[:-1]   # main.js first
        self.assertTrue(load_order_problems(wrong))

    def test_build_id_placeholder_is_in_index_html(self):
        # build-release.ps1 replaces it there (and refuses to build without it).
        html = (STATIC / "index.html").read_text(encoding="utf-8")
        self.assertIn('<meta name="mixer-build" content="__BUILD_ID__"', html)
        for src in re.findall(r'(?:src|href)="((?:js|css)/[^"]+)"', html):
            self.assertTrue(src.endswith("?v=__BUILD_ID__"), src)


if __name__ == "__main__":
    unittest.main()
