"""Taps that must not reach the mixer, OBS or the beamer by accident.

1. A tap outside an open "⋮" menu only closes it (js/menus.js): its
   pointerdown is stopped before any other handler, and the click that
   follows it is swallowed. With no menu open nothing is swallowed, and the
   swallowing never sticks (it ends with the click, a cancelled touch, a new
   pointerdown or a timer).
2. Steuerung in "Bearbeiten" (js/controls.js): a tap on a key never presses
   it (no request); it opens the key's menu instead.

The scripts run in quickjs with a minimal fake DOM. Skips without quickjs;
with REQUIRE_TK=1 (CI) a missing quickjs is an error.
"""

import json
import os
import unittest
from pathlib import Path

try:
    import quickjs
except ImportError:  # pragma: no cover - only where quickjs is not installed
    if os.environ.get("REQUIRE_TK") == "1":  # CI: never skip silently
        raise
    quickjs = None

JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js"

# A fake DOM: elements know their parent, data, classes and listeners;
# events record whether they were stopped or prevented. Timers run when the
# test says so.
FAKE_DOM = r"""
var LISTENERS = [];
function listen(owner, type, fn, capture) { LISTENERS.push({ owner: owner, type: type, fn: fn, capture: !!capture }); }
function El(name, parent) {
  this.name = name; this.parent = parent || null; this.dataset = {}; this.attrs = {};
  this.hidden = false; this.isConnected = true; this.classes = {}; this.children = [];
  var self = this;
  this.classList = {
    contains: function (c) { return !!self.classes[c]; },
    add: function (c) { self.classes[c] = true; }, remove: function (c) { delete self.classes[c]; },
    toggle: function (c, on) { if (on) self.classes[c] = true; else delete self.classes[c]; },
  };
  this.style = {};
}
El.prototype.contains = function (other) { for (var e = other; e; e = e.parent) if (e === this) return true; return false; };
El.prototype.closest = function (sel) {
  for (var e = this; e; e = e.parent) if (e.matches(sel)) return e;
  return null;
};
El.prototype.matches = function (sel) {
  var parts = sel.split(",");
  for (var i = 0; i < parts.length; i++) {
    var p = parts[i].trim(), m;
    if ((m = /^\[data-([a-z-]+)\]$/.exec(p)) || (m = /^button\[data-([a-z-]+)\]$/.exec(p))) {
      var key = m[1].replace(/-([a-z])/g, function (_, c) { return c.toUpperCase(); });
      if (key in this.dataset) return true;
    } else if (p.charAt(0) === "." && this.classes[p.slice(1)]) return true;
    else if (p === "button" && this.button) return true;
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
El.prototype.appendChild = function (c) { c.parent = this; this.children.push(c); return c; };
El.prototype.replaceChildren = function () { this.children = []; };
El.prototype.append = function () {};

var BODY = new El("body");
var ELEMENTS = {};
function $(id) { return ELEMENTS[id] || (ELEMENTS[id] = new El(id, BODY)); }
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
var TIMERS = {}, TIMER_ID = 0;
function setTimeout(fn) { TIMER_ID++; TIMERS[TIMER_ID] = fn; return TIMER_ID; }
function clearTimeout(id) { delete TIMERS[id]; }
function runTimers() { var t = TIMERS; TIMERS = {}; for (var k in t) t[k](); }

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


@unittest.skipIf(quickjs is None, "needs quickjs")
class MenuOutsideTapTests(unittest.TestCase):
    def setUp(self):
        self.js = quickjs.Context()
        self.js.eval(FAKE_DOM)
        self.js.eval((JS / "menus.js").read_text(encoding="utf-8"))
        # A page key that would press something, and a "⋮" with its open menu.
        self.js.eval("""
          var PRESSED = 0;
          var key = new El("key", BODY); key.button = true;
          key.addEventListener("pointerdown", function () { PRESSED++; });
          key.addEventListener("click", function () { PRESSED++; });
          var trigger = new El("dots", BODY); trigger.button = true; trigger.dataset.menuKey = "person:1";
          var menu = new El("menu", BODY); menu.hidden = true;
        """)

    def val(self, expression):
        return json.loads(self.js.eval(f"JSON.stringify({expression})"))

    def open_menu(self):
        self.js.eval("openTileMenu(trigger, menu)")
        self.assertTrue(self.val("OPEN_MENU !== null"))

    def test_outside_tap_only_closes_the_menu(self):
        self.open_menu()
        down = self.val('(function () { var e = fire("pointerdown", key); return [e.stopped, OPEN_MENU === null, menu.hidden]; })()')
        self.assertEqual(down, [True, True, True])
        self.js.eval('fire("pointerup", key)')
        click = self.val('(function () { var e = fire("click", key); return [e.prevented, e.stopped]; })()')
        self.assertEqual(click, [True, True])
        self.assertEqual(self.val("PRESSED"), 0)
        # The next tap works normally.
        self.js.eval('fire("pointerdown", key); fire("pointerup", key); fire("click", key)')
        self.assertEqual(self.val("PRESSED"), 2)

    def test_nothing_swallowed_without_an_open_menu(self):
        self.js.eval('fire("pointerdown", key); fire("pointerup", key); fire("click", key)')
        self.assertEqual(self.val("PRESSED"), 2)

    def test_swallowing_never_sticks(self):
        # The touch became a scroll: no click follows; the next tap works.
        self.open_menu()
        self.js.eval('fire("pointerdown", key); fire("pointercancel", key)')
        self.js.eval('fire("pointerdown", key); fire("pointerup", key); fire("click", key)')
        self.assertEqual(self.val("PRESSED"), 2)
        # No click at all after the finger lifted: the timer ends it.
        self.open_menu()
        self.js.eval('fire("pointerdown", key); fire("pointerup", key); runTimers()')
        self.js.eval('PRESSED = 0; fire("click", key)')
        self.assertEqual(self.val("PRESSED"), 1)

    def test_tap_on_its_own_key_and_inside_the_menu_are_not_swallowed(self):
        self.open_menu()
        self.assertFalse(self.val('fire("pointerdown", trigger).stopped'))
        self.assertTrue(self.val("OPEN_MENU !== null"))
        self.js.eval('var item = new El("item", menu); item.button = true;')
        self.assertFalse(self.val('fire("pointerdown", item).stopped'))
        self.assertFalse(self.val('fire("click", item).prevented'))


@unittest.skipIf(quickjs is None, "needs quickjs")
class CompanionEditingTests(unittest.TestCase):
    def setUp(self):
        self.js = quickjs.Context()
        self.js.eval(FAKE_DOM)
        self.js.eval("""
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
