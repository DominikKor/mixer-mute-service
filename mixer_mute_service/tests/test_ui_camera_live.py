"""Live and Bibel keys on the camera cards (js/cameras.js): shortcuts to the
Steuerung keys "Cam N Live" and "Bibel CamN". They press the same key IDs
through the same route and take their colour from the same OBS status as
Steuerung (js/controls.js applyObsStatus: red on air, green in preview).

The functions run in quickjs with a few fakes; no service, no OBS.
Skips without quickjs; with REQUIRE_TK=1 (CI) a missing quickjs is an error.
"""

import json
import os
import re
import unittest
from pathlib import Path

from app.companion_buttons import COMPANION_BUTTONS, COMPANION_BUTTONS_BY_ID, NOT_IN_STEUERUNG_IDS

try:
    import quickjs
except ImportError:  # pragma: no cover - only where quickjs is not installed
    if os.environ.get("REQUIRE_TK") == "1":  # CI: never skip silently
        raise
    quickjs = None

JS = Path(__file__).resolve().parents[1] / "app" / "static" / "js"
CSS = Path(__file__).resolve().parents[1] / "app" / "static" / "css" / "app.css"


def js_function(source: str, name: str) -> str:
    """One top-level function of a classic script, up to its closing brace."""
    match = re.search(rf"(?m)^(?:async )?function {name}\(", source)
    line = source[match.start():source.index("\n", match.start())]
    if line.count("{") and line.count("{") == line.count("}"):
        return line + "\n"  # a one-line function
    end = source.index("\n}\n", match.start())
    return source[match.start():end + 3]


def js_const(source: str, name: str) -> str:
    match = re.search(rf"(?ms)^const {name} = .*?;\n", source)
    return match.group(0)


FAKES = r"""
var REQUESTS = [], TOASTS = [], REFRESHES = 0, KEYS = [], OBS_STATUS = null, ANSWER = { ok: true, status: 200, data: { ok: true } };
function api(method, path) { REQUESTS.push(method + " " + path); return Promise.resolve(ANSWER); }
function toast(text, ok) { TOASTS.push([text, ok]); }
function refreshObsStatus() { REFRESHES += 1; }
function escapeAttr(s) { return String(s).replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/</g, "&lt;"); }
function FakeKey(attrs) {
  var self = this; this.dataset = attrs; this.classes = {}; this.attrs = {}; this.disabled = false; this.textContent = attrs.label || "";
  this.classList = { toggle: function (c, on) { if (on) self.classes[c] = true; else delete self.classes[c]; },
                     contains: function (c) { return !!self.classes[c]; } };
  this.setAttribute = function (k, v) { self.attrs[k] = v; };
  this.removeAttribute = function (k) { delete self.attrs[k]; };
}
var document = { querySelectorAll: function (sel) {
  if (sel.indexOf("button.companion-key") !== 0) return [];
  if (sel.indexOf("[data-obs-scene]") >= 0) return KEYS.filter(function (k) { return k.companion && "obsScene" in k.dataset; });
  if (sel.indexOf("[data-obs-overlay-scene]") >= 0) return KEYS.filter(function (k) { return k.companion && "obsOverlayScene" in k.dataset; });
  return [];
} };
"""


def steuerung_buttons(drop=()):
    """The catalog keys a fresh Steuerung has, as GET /companion/buttons lists them."""
    return [b for b in COMPANION_BUTTONS if b["id"] not in NOT_IN_STEUERUNG_IDS and b["id"] not in drop]


class CatalogTests(unittest.TestCase):
    def test_the_keys_are_the_steuerung_keys(self):
        for cam, scene in (("cam1", "Cam 1"), ("cam2", "Cam 2")):
            live = COMPANION_BUTTONS_BY_ID[f"native-obs-{cam}-live"]["native_action"]
            bible = COMPANION_BUTTONS_BY_ID[f"native-obs-bible-{cam}"]["native_action"]
            self.assertEqual((live["type"], live["scene"]), ("obs-live", scene))
            self.assertEqual((bible["type"], bible["scene"], bible["source"]), ("obs-overlay-toggle", scene, "BibleShow"))
            self.assertNotIn(f"native-obs-{cam}-live", NOT_IN_STEUERUNG_IDS)
            self.assertNotIn(f"native-obs-bible-{cam}", NOT_IN_STEUERUNG_IDS)

    def test_pad_layout(self):
        css = CSS.read_text(encoding="utf-8")
        pad = re.search(r"(?ms)^\.camera-ptz \{(.*?)\n\}", css).group(1)
        # Two rows like a keyboard's arrow keys; Live and Bibel line up with them.
        self.assertIn('grid-template-rows: repeat(2, ', pad)
        self.assertIn('"zout up zin live" "left down right bible"', pad)
        self.assertIn('.camera-ptz [data-direction="in"] { grid-area: zin; }', css)
        self.assertIn('.camera-ptz [data-direction="out"] { grid-area: zout; }', css)


@unittest.skipIf(quickjs is None, "needs quickjs")
class CameraObsKeyTests(unittest.TestCase):
    def setUp(self):
        cameras = (JS / "cameras.js").read_text(encoding="utf-8")
        controls = (JS / "controls.js").read_text(encoding="utf-8")
        core = (JS / "core.js").read_text(encoding="utf-8")
        self.js = quickjs.Context()
        self.js.eval(FAKES)
        self.js.eval(js_function(core, "escapeHtml"))
        for name in ("obsKeyRole", "applyObsStatus"):
            self.js.eval(js_function(controls, name))
        self.js.eval(js_const(cameras, "CAMERA_OBS_KEYS").replace("const ", "var ", 1))
        self.js.eval(js_const(cameras, "CAMERA_OBS_BUSY").replace("const ", "var ", 1))
        for name in ("cameraObsKeys", "cameraObsKeyHtml", "pressCameraObsKey"):
            self.js.eval(js_function(cameras, name))
        self.data(steuerung_buttons())

    def data(self, buttons):
        self.js.eval(f"var COMPANION_DATA = {json.dumps({'buttons': buttons, 'companion_targets': []})};")

    def run_jobs(self):
        while self.js.execute_pending_job():
            pass

    def value(self, expression):
        return json.loads(self.js.eval(f"JSON.stringify({expression})"))

    def test_keys_of_each_camera(self):
        self.assertEqual(self.value('cameraObsKeys("cam1")'), [
            {"kind": "live", "id": "native-obs-cam1-live", "label": "Cam 1 Live", "scene": "Cam 1", "overlay": None},
            {"kind": "bible", "id": "native-obs-bible-cam1", "label": "Bibel Cam1 Beamer", "scene": None,
             "overlay": {"scene": "Cam 1", "source": "BibleShow"}},
        ])
        self.assertEqual([k["id"] for k in self.value('cameraObsKeys("cam2")')],
                         ["native-obs-cam2-live", "native-obs-bible-cam2"])
        self.assertEqual(self.value('cameraObsKeys("cam3")'), [])

    def test_key_removed_from_steuerung_is_not_shown(self):
        self.data(steuerung_buttons(drop={"native-obs-cam1-live"}))
        self.assertEqual([k["id"] for k in self.value('cameraObsKeys("cam1")')], ["native-obs-bible-cam1"])

    def test_renamed_key_keeps_its_label(self):
        buttons = steuerung_buttons()
        for b in buttons:
            if b["id"] == "native-obs-cam2-live":
                b["label"] = "Kamera <2> live"
        self.data(buttons)
        html = self.value('cameraObsKeyHtml(cameraObsKeys("cam2")[0])')
        self.assertIn("Kamera &lt;2&gt; live</button>", html)

    def test_markup_carries_the_key_id_and_the_status_role(self):
        live, bible = (self.value(f'cameraObsKeyHtml(cameraObsKeys("cam1")[{i}])') for i in (0, 1))
        # applyObsStatus colours "button.companion-key[data-obs-...]".
        for html in (live, bible):
            self.assertRegex(html, r'class="companion-key native camera-obs-key ')
        self.assertIn('data-camera-obs-press="native-obs-cam1-live"', live)
        self.assertIn('data-obs-scene="Cam 1"', live)
        self.assertIn('data-camera-obs-press="native-obs-bible-cam1"', bible)
        self.assertIn('data-obs-overlay-scene="Cam 1" data-obs-overlay-source="BibleShow"', bible)
        self.assertNotIn("data-obs-scene", bible)

    def test_press_uses_the_steuerung_route(self):
        for key_id in ("native-obs-cam2-live", "native-obs-bible-cam1"):
            self.js.eval(f'var key = new FakeKey({{ cameraObsPress: "{key_id}", label: "X" }}); pressCameraObsKey(key);')
            self.run_jobs()
        self.assertEqual(self.value("REQUESTS"), [
            "POST /companion/buttons/native-obs-cam2-live/press",
            "POST /companion/buttons/native-obs-bible-cam1/press",
        ])
        self.assertEqual(self.value("REFRESHES"), 2)   # the cut shows at once
        self.assertFalse(self.value("key.disabled"))

    def test_refused_press_says_why(self):
        self.js.eval('ANSWER = { ok: false, status: 503, data: { ok: false, reason: "OBS not reachable" } };'
                     'var key = new FakeKey({ cameraObsPress: "native-obs-cam1-live", label: "Cam 1 Live" }); pressCameraObsKey(key);')
        self.run_jobs()
        self.assertEqual(self.value("TOASTS"), [["Cam 1 Live: OBS not reachable", False]])
        self.assertEqual(self.value("REFRESHES"), 0)

    def colours(self, status):
        # The camera keys as rendered (their data-* attributes), coloured by
        # the same function and status as Steuerung.
        self.js.eval("""
          KEYS = [];
          ["cam1", "cam2"].forEach(function (cam) { cameraObsKeys(cam).forEach(function (k) {
            var data = { cameraObsPress: k.id };
            if (k.scene) data.obsScene = k.scene;
            if (k.overlay) { data.obsOverlayScene = k.overlay.scene; data.obsOverlaySource = k.overlay.source; }
            var key = new FakeKey(data); key.companion = true; KEYS.push(key);
          }); });
        """)
        self.js.eval(f"OBS_STATUS = {json.dumps(status)}; applyObsStatus();")
        return dict(self.value("""KEYS.map(function (k) { return [k.dataset.cameraObsPress,
          k.classes["obs-program"] ? "red" : k.classes["obs-preview"] ? "green" : "-"]; })"""))

    def test_live_colours_come_from_the_obs_status(self):
        overlays = [{"scene": "Cam 1", "source": "BibleShow", "shown": False},
                    {"scene": "Cam 2", "source": "BibleShow", "shown": True}]
        self.assertEqual(self.colours({"ok": True, "program": "Cam 1", "preview": "Cam 2", "overlays": overlays}), {
            "native-obs-cam1-live": "red", "native-obs-cam2-live": "green",
            "native-obs-bible-cam1": "-", "native-obs-bible-cam2": "green",   # shown, scene not on air
        })
        overlays[1]["shown"], overlays[0]["shown"] = False, True
        self.assertEqual(self.colours({"ok": True, "program": "Cam 1", "preview": None, "overlays": overlays}), {
            "native-obs-cam1-live": "red", "native-obs-cam2-live": "-",
            "native-obs-bible-cam1": "red", "native-obs-bible-cam2": "-",
        })
        # OBS does not answer: no colour at all.
        self.assertEqual(set(self.colours({"ok": False}).values()), {"-"})


if __name__ == "__main__":
    unittest.main()
