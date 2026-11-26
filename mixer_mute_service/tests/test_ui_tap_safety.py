"""Taps that must not reach the mixer, OBS or the beamer by accident.

1. A tap outside an open "⋮" menu only closes it (js/menus.js): a
   transparent backdrop right under the menu takes it, so no key, fader or
   slider below gets any event (a slider's jump is the browser's own default
   action, so it can only be stopped by the tap never reaching it). The
   backdrop lies under the header, so Panic works with the first tap while
   a menu is open. The stacking is checked in app.css; the scripts' part
   runs in quickjs with a small fake DOM whose hit() follows that stacking.
2. Steuerung in "Bearbeiten" (js/controls.js): a tap on a key never presses
   it (no request); it opens the key's menu instead.

Skips without quickjs; with REQUIRE_TK=1 (CI) a missing quickjs is an error.
"""

import json
import os
import re
import unittest
from pathlib import Path

try:
    import quickjs
except ImportError:  # pragma: no cover - only where quickjs is not installed
    if os.environ.get("REQUIRE_TK") == "1":  # CI: never skip silently
        raise
    quickjs = None

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"
JS = STATIC / "js"

# A fake DOM: elements know their parent, children, data, classes and
# listeners; events bubble through the ancestors.
FAKE_DOM = r"""
var LISTENERS = [], ELEMENTS = {}, STUB_IDS = false;
function listen(owner, type, fn, capture) { LISTENERS.push({ owner: owner, type: type, fn: fn, capture: !!capture }); }
function El(name, parent) {
  this.name = name; this.parent = null; this.children = []; this.dataset = {}; this.attrs = {};
  this.hidden = false; this.isConnected = true; this.classes = {}; this.style = {};
  var self = this;
  this.classList = {
    contains: function (c) { return !!self.classes[c]; },
    add: function (c) { self.classes[c] = true; }, remove: function (c) { delete self.classes[c]; },
    toggle: function (c, on) { if (on) self.classes[c] = true; else delete self.classes[c]; },
  };
  if (parent) parent.appendChild(this);
}
Object.defineProperty(El.prototype, "id", {
  get: function () { return this._id; }, set: function (v) { this._id = v; ELEMENTS[v] = this; } });
Object.defineProperty(El.prototype, "className", {
  get: function () { return Object.keys(this.classes).join(" "); },
  set: function (v) { var self = this; self.classes = {}; v.split(" ").forEach(function (c) { if (c) self.classes[c] = true; }); } });
Object.defineProperty(El.prototype, "nextSibling", { get: function () {
  if (!this.parent) return null;
  var list = this.parent.children; return list[list.indexOf(this) + 1] || null; } });
El.prototype.contains = function (other) { for (var e = other; e; e = e.parent) if (e === this) return true; return false; };
El.prototype.closest = function (sel) { for (var e = this; e; e = e.parent) if (e.matches(sel)) return e; return null; };
El.prototype.matches = function (sel) {
  var parts = sel.split(",");
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i].trim(), m;
    if ((m = /^(?:button)?\[data-([a-z-]+)\]$/.exec(p))) {
      var key = m[1].replace(/-([a-z])/g, function (_, c) { return c.toUpperCase(); });
      if (key in this.dataset) return true;
    } else if (p.charAt(0) === "." && this.classes[p.slice(1)]) return true;
    else if (p === "button" && this.button) return true;
    else if (p === "dialog[open]" && this.dialog) return true;
  }
  return false;
};
El.prototype.setAttribute = function (k, v) { this.attrs[k] = String(v); };
El.prototype.getAttribute = function (k) { return k in this.attrs ? this.attrs[k] : null; };
El.prototype.addEventListener = function (type, fn, capture) { listen(this, type, fn, capture); };
El.prototype.getBoundingClientRect = function () { return { top: 10, left: 10, right: 54, bottom: 54, width: 44, height: 44 }; };
El.prototype.querySelector = function () { return null; };
El.prototype.querySelectorAll = function () { return []; };
El.prototype.focus = function () {};
El.prototype.appendChild = function (c) { return this.insertBefore(c, null); };
El.prototype.insertBefore = function (c, ref) {
  if (c.parent) c.parent.children.splice(c.parent.children.indexOf(c), 1);
  c.parent = this;
  var i = ref ? this.children.indexOf(ref) : -1;
  if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
  return c;
};
El.prototype.replaceChildren = function () { this.children = []; };
El.prototype.append = function () {};

var BODY = new El("body");
function $(id) {
  if (ELEMENTS[id] || !STUB_IDS) return ELEMENTS[id] || null;
  var stub = new El(id, BODY); stub.id = id; return stub;
}
var document = {
  body: BODY,
  addEventListener: function (type, fn, capture) { listen(document, type, fn, capture); },
  querySelectorAll: function () { return []; },
  querySelector: function () { return null; },
  createElement: function (tag) { var e = new El(tag); if (tag === "button") e.button = true; return e; },
};
var window = { addEventListener: function (type, fn, capture) { listen(window, type, fn, capture); } };
var innerWidth = 1376, innerHeight = 1032;
var CSS = { escape: function (s) { return s; } };
function escapeAttr(s) { return s; }
function setTimeout() { return 0; }
function clearTimeout() {}

// Dispatch like a browser: window and document capture, then the target and
// its ancestors (bubbling), then document and window.
function fire(type, target) {
  var ev = { type: type, target: target, stopped: false, prevented: false,
    stopPropagation: function () { this.stopped = true; }, stopImmediatePropagation: function () { this.stopped = true; },
    preventDefault: function () { this.prevented = true; } };
  var order = [[window, true], [document, true]];
  for (var e = target; e; e = e.parent) order.push([e, false]);
  order.push([document, false], [window, false]);
  for (var i = 0; i < order.length && !ev.stopped; i++) {
    var owner = order[i][0], capture = order[i][1];
    LISTENERS.forEach(function (l) {
      if (!ev.stopped && l.type === type && l.owner === owner && (l.capture === capture || owner === target)) l.fn(ev);
    });
  }
  return ev;
}
"""

# The page as app.css stacks it: header (z 10) over the menu backdrop (z 9)
# over the page; the menu over everything. hit(el) is the element a tap
# aimed at el really lands on.
PAGE = r"""
var HEADER = new El("header", BODY);
var PAGE = new El("page", BODY);
function hit(el) {
  var backdrop = ELEMENTS["menu-backdrop"];
  if (!backdrop || backdrop.hidden || HEADER.contains(el) || (OPEN_MENU && OPEN_MENU.menu.contains(el))) return el;
  return backdrop;
}
function tap(el) {
  var target = hit(el);
  fire("pointerdown", target); fire("pointerup", target); fire("click", target);
  return target;
}
var LOG = [];
var panic = new El("panic", HEADER); panic.button = true;
panic.addEventListener("click", function () { LOG.push("panic"); });
// A mute group's fader: a slider that sends on change (as groups.js does).
var slider = new El("fader", PAGE);
["pointerdown", "input", "change", "click"].forEach(function (t) {
  slider.addEventListener(t, function () { LOG.push("fader " + t); });
});
var key = new El("mute", PAGE); key.button = true;
key.addEventListener("click", function () { LOG.push("mute"); });
var trigger = new El("dots", PAGE); trigger.button = true; trigger.dataset.menuKey = "person:1";
var other = new El("dots2", PAGE); other.button = true; other.dataset.menuKey = "person:2";
"""


def z_index(rule: str) -> int:
    return int(re.search(r"z-index:\s*(\d+)", rule).group(1))


class MenuBackdropCssTests(unittest.TestCase):
    def test_backdrop_covers_the_page_under_the_header(self):
        css = (STATIC / "css" / "app.css").read_text(encoding="utf-8")
        backdrop = re.search(r"(?m)^\.menu-backdrop \{([^}]*)\}", css).group(1)
        self.assertIn("position: fixed", backdrop)
        self.assertIn("inset: 0", backdrop)
        header = re.search(r"(?ms)^header \{(.*?)\}", css).group(1)
        menu = re.search(r"(?ms)^\.tile-menu \{(.*?)\}", css).group(1)
        self.assertLess(z_index(backdrop), z_index(header))   # Panic stays on top
        self.assertLess(z_index(backdrop), z_index(menu))


@unittest.skipIf(quickjs is None, "needs quickjs")
class MenuBackdropTests(unittest.TestCase):
    def setUp(self):
        self.js = quickjs.Context()
        self.js.eval(FAKE_DOM)
        self.js.eval((JS / "menus.js").read_text(encoding="utf-8"))
        self.js.eval(PAGE)

    def val(self, expression):
        return json.loads(self.js.eval(f"JSON.stringify({expression})"))

    def open_menu(self):
        self.js.eval('openActionMenu(trigger, [{ label: "Löschen", run: function () { LOG.push("delete"); } }])')
        self.assertTrue(self.val('OPEN_MENU !== null && !ELEMENTS["menu-backdrop"].hidden'))
        # Right under the menu, in the same place.
        self.assertTrue(self.val('ELEMENTS["menu-backdrop"].nextSibling === ELEMENTS["action-menu"]'))

    def test_slider_tap_with_a_menu_open_sends_nothing(self):
        self.open_menu()
        self.assertEqual(self.val("tap(slider).id"), "menu-backdrop")
        self.assertEqual(self.val("LOG"), [])
        self.assertTrue(self.val('OPEN_MENU === null && ELEMENTS["menu-backdrop"].hidden'))
        # The next tap reaches the fader as usual.
        self.js.eval("tap(slider)")
        self.assertEqual(self.val("LOG"), ["fader pointerdown", "fader click"])

    def test_key_tap_with_a_menu_open_only_closes_it(self):
        self.open_menu()
        self.js.eval("tap(key)")
        self.assertEqual(self.val("LOG"), [])
        self.assertTrue(self.val("OPEN_MENU === null"))
        self.js.eval("tap(key)")
        self.assertEqual(self.val("LOG"), ["mute"])

    def test_panic_fires_on_the_first_tap_with_a_menu_open(self):
        self.open_menu()
        self.js.eval("tap(panic)")
        self.assertEqual(self.val("LOG"), ["panic"])
        self.assertTrue(self.val('OPEN_MENU === null && ELEMENTS["menu-backdrop"].hidden'))

    def test_menu_items_still_work(self):
        self.open_menu()
        self.js.eval('tap(ELEMENTS["action-menu"].children[0])')
        self.assertEqual(self.val("LOG"), ["delete"])
        self.assertTrue(self.val('OPEN_MENU === null && ELEMENTS["menu-backdrop"].hidden'))

    def test_another_menu_key_first_closes_then_opens(self):
        self.js.eval('other.addEventListener("click", function () { toggleActionMenu(other, [{ label: "x", run: function () {} }]); });')
        self.open_menu()
        self.js.eval("tap(other)")
        self.assertTrue(self.val("OPEN_MENU === null"))
        self.js.eval("tap(other)")
        self.assertTrue(self.val("OPEN_MENU !== null && OPEN_MENU.trigger === other"))

    def test_menu_in_a_dialog_brings_the_backdrop_along(self):
        self.js.eval('var dlg = new El("dialog", BODY); dlg.dialog = true; var inner = new El("dots3", dlg); inner.button = true;')
        self.js.eval('openActionMenu(inner, [{ label: "x", run: function () {} }])')
        self.assertTrue(self.val('ELEMENTS["menu-backdrop"].parent === dlg && ELEMENTS["action-menu"].parent === dlg'))
        self.assertTrue(self.val('ELEMENTS["menu-backdrop"].nextSibling === ELEMENTS["action-menu"]'))

    def test_nothing_in_the_way_without_a_menu(self):
        self.js.eval("tap(key); tap(slider); tap(panic)")
        self.assertEqual(self.val("LOG"), ["mute", "fader pointerdown", "fader click", "panic"])


@unittest.skipIf(quickjs is None, "needs quickjs")
class CompanionEditingTests(unittest.TestCase):
    def setUp(self):
        self.js = quickjs.Context()
        self.js.eval(FAKE_DOM)
        self.js.eval("""
          STUB_IDS = true;
          var REQUESTS = [], MENUS = [];
          function api(method, path) { REQUESTS.push(path); return Promise.resolve({ ok: true, status: 200, data: {} }); }
          function toggleActionMenu(trigger, items) { MENUS.push(trigger.name); }
          function toast() {} function refresh() {} function loadCompanionButtons() {}
          function renderCameraControls() {} function confirmDelete() { return Promise.resolve(false); }
        """)
        self.js.eval((JS / "controls.js").read_text(encoding="utf-8"))
        self.js.eval("""
          COMPANION_DATA = { categories: [{ id: "obs", name: "OBS", visible: true }],
            buttons: [{ id: "cam1-live", label: "Cam 1 Live", category_id: "obs", kind: "native" }], companion_targets: [] };
          var grid = $("companion-categories");
          var item = new El("item", grid); item.classes["companion-button-item"] = true;
          var press = new El("press", item); press.button = true; press.dataset.companionPress = "cam1-live";
          press.textContent = "Cam 1 Live";
          var dots = new El("dots", item); dots.button = true; dots.dataset.companionMenu = "cam1-live";
          item.querySelector = function () { return dots; };
        """)

    def tap(self):
        self.js.eval('fire("click", press)')
        return json.loads(self.js.eval("JSON.stringify([REQUESTS, MENUS])"))

    def test_editing_key_opens_its_menu_and_never_presses(self):
        self.js.eval('$("companion-categories").classList.add("editing")')
        requests, menus = self.tap()
        self.assertEqual(requests, [])
        self.assertEqual(menus, ["dots"])

    def test_key_presses_outside_editing(self):
        requests, menus = self.tap()
        self.assertEqual(requests, ["/companion/buttons/cam1-live/press"])
        self.assertEqual(menus, [])


if __name__ == "__main__":
    unittest.main()
