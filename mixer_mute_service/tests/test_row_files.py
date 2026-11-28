"""A program row's Desktop steps (app/row_files.py, app/api/program.py
_run_content, app/actions.py run_via).

Verifies: a key with another file ("via:<key>|<path>") runs with the key's PC,
monitor, full screen and position (audio: the key's fade) and the step's
file, under an id of its own per key and file; steps are validated on save;
the kind comes from the path; an older "file:" step runs on its PC as before;
the old Playback / Übersetzung / Liedtext fields fold into steps (the
production rows of 2026-11-26 as examples), once, also in the stores and
template song slots; the steps run in order with the beamer freeze where the
row puts it (live after the service's start), never sent while its state is
unknown (a Freeze that may not have arrived), skipped after a failed
step, a no-op when it already is; a picture the agent confirms ("shown") is
not waited for, others get the settle time before a freeze; the song step
runs where it stands, a row's song without a step last; "Stopp" stops what
the program started; "Shortcut laden" keeps the row's song step.

Display agents and the beamer are faked; nothing leaves the process.
"""

import asyncio
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers import app_client

from app import native_controls as nc
from app import row_files
from app.api import program as program_api
from app.program_items import ProgramItemStore, apply_shortcut

BEAMER_PC, STREAMING_PC = "192.168.188.11", "192.168.188.20"
PROFILES = (
    "desktop_display:\n  enabled: true\n  profiles:\n"
    f"    beamer2:\n      title: Beamer Folie\n      kind: file\n      path: 'C:\\Folien\\leer.pptx'\n"
    f"      target_ip: {BEAMER_PC}\n      monitor: 2\n      fullscreen: true\n      position: center\n"
    f"    monitor1:\n      title: Anzeige Monitor 1\n      kind: image\n      path: 'C:\\Bilder\\logo.png'\n"
    f"      target_ip: {BEAMER_PC}\n      monitor: 1\n      fullscreen: false\n"
    f"    musik:\n      title: Musik Streaming\n      kind: audio\n      path: 'C:\\Musik\\bg.mp3'\n"
    f"      target_ip: {STREAMING_PC}\n      fade: true\n      fade_seconds: 3\n"
)
ENV = {"DESKTOP_AGENT_TOKEN": "test-token"}
TRANSLATION = r"I:\Uebersetzung\Predigt.pdf"
SONG = {"title": "Lied", "titles": ["Lied"], "books": [], "copyright": "",
        "slides": [{"key": "1", "text": "Eins"}]}


def row(title, **extra):
    return {"time": "10:00", "title": title, "person": "", "group": "", **extra}


class Events:
    """Agents and beamer in one timeline: ("agent", pc, path, payload) and ("beamer", field)."""

    def __init__(self):
        self.log = []
        self.dead = set()
        self.shown = False

    def post(self, url, _token, payload, timeout):
        pc, path = url.removeprefix("http://").split(":", 1)
        path = "/" + path.split("/", 1)[1]
        self.log.append(("agent", pc, path, payload))
        if (pc, payload.get("path")) in self.dead:
            raise urllib.error.HTTPError(url, 503, "Configured target is not available", {}, None)
        return {"ok": True, **({"shown": True} if self.shown and path == "/display" else {})}

    def beamer(self, field, value):
        self.log.append(("beamer", field))

    def agent(self, path):
        return [(entry[1], entry[3]) for entry in self.log if entry[0] == "agent" and entry[2] == path]

    def order(self):
        return [entry[2] if entry[0] == "agent" else "freeze" for entry in self.log
                if entry[0] == "beamer" or entry[2] in ("/display", "/song", "/audio")]


class Fixture:
    def __init__(self, test, settle=0.0):
        self.events = Events()
        self.test = test
        self.settle = settle

    def __enter__(self):
        self.ctx = app_client(PROFILES, env=ENV)
        self.client, self.folder = self.ctx.__enter__()
        self.patches = [patch("app.display_agent._post", self.events.post),
                        patch.object(nc, "_post_beamer_form", self.events.beamer),
                        patch.object(program_api, "SETTLE_SECONDS", self.settle)]
        for p in self.patches:
            p.start()
        from app.main import app
        self.app = app
        app.state.songs.songs = {"lied.foil": SONG}
        return self

    def __exit__(self, *exc):
        for p in reversed(self.patches):
            p.stop()
        return self.ctx.__exit__(*exc)

    def create(self, **fields):
        r = self.client.post("/program/items", json=row("Punkt", **fields))
        self.test.assertEqual(r.status_code, 200, r.text)
        return r.json()["item"]

    def play(self, item):
        return self.client.post(f"/program/items/{item['id']}/play").json()

    def frozen(self, value):
        self.test.assertEqual(self.client.post("/beamer/state", json={"frozen": value}).status_code, 200)

    def freeze_times_out(self):
        """The Beamer-freeze key, and the beamer does not answer: the Freeze may
        or may not have arrived, so the state is unknown."""
        def no_answer(*_args):
            raise nc.NativeControlError("Beamer did not answer in time", maybe_sent=True)
        with patch.object(nc, "_post_beamer_form", no_answer), self.test.assertLogs("mixer", "WARNING"):
            self.client.post("/companion/buttons/beamer-freeze/press")
        self.test.assertIsNone(self.client.get("/beamer/state").json()["frozen"])


TRANSLATION_STEP = row_files.via_value("beamer2", TRANSLATION)


class RunViaTests(unittest.TestCase):
    def test_file_runs_with_the_keys_output_and_the_steps_path(self):
        with Fixture(self) as f:
            item = f.create(desktop=[TRANSLATION_STEP, row_files.via_value("musik", r"C:\Musik\Intro.mp3")])
            answer = f.play(item)
            self.assertTrue(answer["ok"], answer)
            (pc, shown), = f.events.agent("/display")
            self.assertEqual(pc, BEAMER_PC)
            self.assertEqual((shown["path"], shown["monitor"], shown["fullscreen"], shown["position"], shown["kind"]),
                             (TRANSLATION, 2, True, "center", "file"))
            self.assertEqual(shown["id"], row_files.start_id("beamer2", TRANSLATION))
            (pc, played), = f.events.agent("/audio")
            self.assertEqual((pc, played["path"], played["fade"]["seconds"]), (STREAMING_PC, r"C:\Musik\Intro.mp3", 3.0))
            self.assertNotEqual(row_files.start_id("beamer2", TRANSLATION), row_files.start_id("beamer2", r"I:\b.pdf"))

    def test_steps_are_checked_on_save(self):
        with Fixture(self) as f:
            for bad in (["via:beamer2"], ["via:beamer2|Folien\\a.pptx"], ["via:bad key|C:\\a.pptx"],
                        ["via:beamer2|\\\\server\\share\\Start.exe"], ["@nix"], ["file:127.0.0.1"], [3]):
                r = f.client.post("/program/items", json=row("x", desktop=bad))
                self.assertIn(r.status_code, (400, 422), bad)
            r = f.client.post("/program/items", json=row("x", desktop=[row_files.via_value("weg", TRANSLATION)]))
            self.assertEqual((r.status_code, r.json()["reason"]), (400, "unknown desktop profile weg"))
            item = f.create(desktop=["@freeze", "@unfreeze", "@freeze", "@song", "@song"], song="lied.foil")
            self.assertEqual(item["desktop"], ["@freeze", "@unfreeze", "@freeze", "@song"])  # the song once

    def test_kind_from_the_path(self):
        self.assertEqual([row_files.path_kind(p) for p in ("C:\\a.PNG", "C:\\a.mp3", "C:\\a.exe", "C:\\a.pptx",
                                                           "C:\\a.svg", "C:\\a.heic")],
                         ["image", "audio", "program", "file", "file", "file"])

    def test_older_file_step_keeps_running_on_its_pc(self):
        with Fixture(self) as f:
            item = f.create(desktop=[f"file:{BEAMER_PC}|{TRANSLATION}"])
            self.assertTrue(f.play(item)["ok"])
            (pc, shown), = f.events.agent("/display")
            # A document (app/documents.py): under that PC's document id, paged there.
            self.assertEqual((pc, shown["id"], shown["path"]), (BEAMER_PC, "doc-192-168-188-11", TRANSLATION))
            self.assertEqual(f.client.app.state.document_shown, (BEAMER_PC, "doc-192-168-188-11"))


# The production desktop profiles of 2026-11-26.
PRODUCTION = {
    "gemeindegaben": SimpleNamespace(target_ip=BEAMER_PC, kind="image", monitor=2),
    "Uhrzeit": SimpleNamespace(target_ip=BEAMER_PC, kind="file", monitor=2),
    "missionsgaben-qr": SimpleNamespace(target_ip=BEAMER_PC, kind="image", monitor=2),
    "hintergrundmusic": SimpleNamespace(target_ip=STREAMING_PC, kind="audio", monitor=1),
    "hintergrundmusic-kopie": SimpleNamespace(target_ip=BEAMER_PC, kind="audio", monitor=1),
    "missionspotlight": SimpleNamespace(target_ip=BEAMER_PC, kind="file", monitor=2),
}
MUSIC = r"I:\GoogleDrive\GHS_Lieder_Playback\001_Ich singe dir mit Herz und Mund.mp3"
PDF = r"I:\GoogleDrive\Gottesdienstablauf-20261126.pdf"


class MigrationTests(unittest.TestCase):
    def test_production_rows(self):
        musik = {"playback": f"file:127.0.0.1|{MUSIC}", "translation": f"file:{BEAMER_PC}|{PDF}", "lyrics": None,
                 "desktop": ["gemeindegaben", "missionsgaben-qr"], "song": None}
        notes = row_files.fold_legacy(musik, PRODUCTION, [STREAMING_PC])
        self.assertEqual(musik["desktop"], [f"via:hintergrundmusic|{MUSIC}", f"via:Uhrzeit|{PDF}",
                                            "gemeindegaben", "missionsgaben-qr"])
        self.assertEqual((musik["playback"], musik["translation"]), (None, None))
        self.assertEqual(len(notes), 2)
        bibel = {"lyrics": "desktop:Uhrzeit", "desktop": ["Uhrzeit"], "song": "lied.foil"}
        row_files.fold_legacy(bibel, PRODUCTION, [STREAMING_PC])
        self.assertEqual((bibel["desktop"], bibel["lyrics"]), (["Uhrzeit", "@song"], None))  # already a step
        # Idempotent: nothing more to do.
        self.assertEqual(row_files.fold_legacy(musik, PRODUCTION, [STREAMING_PC]), [])
        self.assertEqual(row_files.fold_legacy(bibel, PRODUCTION, [STREAMING_PC]), [])

    def test_music_prefers_a_key_without_fade(self):
        faded = SimpleNamespace(target_ip=STREAMING_PC, kind="audio", monitor=1, fade=True)
        plain = SimpleNamespace(target_ip=STREAMING_PC, kind="audio", monitor=1, fade=False)
        r = {"playback": f"file:{STREAMING_PC}|{MUSIC}", "desktop": []}
        row_files.fold_legacy(r, {"hintergrundmusic": faded, "playback": plain})
        self.assertEqual(r["desktop"], [f"via:playback|{MUSIC}"])
        r = {"playback": f"file:{STREAMING_PC}|{MUSIC}", "desktop": []}
        notes = row_files.fold_legacy(r, {"hintergrundmusic": faded})
        self.assertEqual(r["desktop"], [f"via:hintergrundmusic|{MUSIC}"])  # only a faded one: that one, and said so
        self.assertIn("fades in", notes[0])

    def test_keys_unknown_pcs_and_song_slots(self):
        r = {"translation": "native-cam1-preset-1", "playback": "file:192.168.188.30|C:\\x.mp3", "desktop": []}
        row_files.fold_legacy(r, PRODUCTION)
        # A key stays a key; a file on a PC without Desktop key stays a file step (runs as before).
        self.assertEqual(r["desktop"], ["file:192.168.188.30|C:\\x.mp3", "key:native-cam1-preset-1"])
        slot = {"desktop": ["gemeindegaben"], "song": None}
        row_files.fold_legacy(slot, PRODUCTION, song_slot=True)
        self.assertEqual(slot["desktop"], ["gemeindegaben", "@song"])

    def test_stores_are_migrated_once(self):
        with tempfile.TemporaryDirectory() as name:
            store = ProgramItemStore(Path(name) / "program_items.json")
            asyncio.run(store.create(row("Musikbeitrag 1", playback=f"file:127.0.0.1|{MUSIC}",
                                         translation=f"file:{BEAMER_PC}|{PDF}",
                                         desktop=["gemeindegaben", "missionsgaben-qr"])))
            state = SimpleNamespace(cfg=SimpleNamespace(desktop_display=SimpleNamespace(profiles=PRODUCTION)),
                                    program_items=store, program_templates=None, shortcuts=None, target_pcs=None)
            with self.assertLogs("mixer.row_files", "INFO") as logs:
                first = asyncio.run(row_files.migrate_stores(state))
            self.assertEqual(first["program"], 1)
            self.assertTrue(any("-> step" in line for line in logs.output))
            self.assertEqual(asyncio.run(row_files.migrate_stores(state))["program"], 0)
            saved = asyncio.run(ProgramItemStore(Path(name) / "program_items.json").list())[0]
            self.assertEqual(saved["desktop"][:2], [f"via:hintergrundmusic|{MUSIC}", f"via:Uhrzeit|{PDF}"])
            self.assertIsNone(saved["playback"])

    def test_an_older_page_saving_the_fields_gets_steps(self):
        with Fixture(self) as f:
            item = f.create(playback=f"file:{STREAMING_PC}|C:\\Musik\\a.mp3", lyrics="desktop:monitor1",
                            desktop=["monitor1"], song="lied.foil")
            self.assertEqual(item["desktop"], ["via:musik|C:\\Musik\\a.mp3", "monitor1", "@song"])
            self.assertEqual((item["playback"], item["lyrics"]), (None, None))

    def test_loading_a_shortcut_keeps_the_rows_song_step(self):
        result = apply_shortcut({"title": "x", "song": "lied.foil", "desktop": ["a", "@song"]},
                                {"desktop": ["b"], "group": ""})
        self.assertEqual(result["desktop"], ["b", "@song"])


class StepOrderTests(unittest.TestCase):
    def test_show_freeze_song(self):
        with Fixture(self) as f:
            # Live after the start: a freeze step works without setting the state by hand.
            self.assertIs(f.client.get("/beamer/state").json()["frozen"], False)
            item = f.create(desktop=[TRANSLATION_STEP, "@freeze", "@song"], song="lied.foil")
            answer = f.play(item)
            self.assertTrue(answer["ok"], answer)
            self.assertEqual(f.events.order(), ["/display", "freeze", "/song"])
            # Frozen now: a second freeze sends nothing (it is a toggle); unfreeze sends one.
            f.events.log.clear()
            self.assertTrue(f.play(item)["ok"])
            self.assertEqual(f.events.order(), ["/display", "/song"])
            thaw = f.create(desktop=["@unfreeze", "@unfreeze"])
            f.events.log.clear()
            self.assertTrue(f.play(thaw)["ok"])
            self.assertEqual(f.events.order(), ["freeze"])  # one Freeze press toggles it back; the second is a no-op
            self.assertIs(f.client.get("/beamer/state").json()["frozen"], False)

    def test_unknown_freeze_state_sends_nothing(self):
        with Fixture(self) as f:
            f.freeze_times_out()
            f.events.log.clear()
            item = f.create(desktop=[TRANSLATION_STEP, "@freeze", "@song"], song="lied.foil")
            with self.assertLogs("mixer.program", "WARNING"):
                answer = f.play(item)
            self.assertEqual(f.events.order(), ["/display", "/song"])
            self.assertIn("Beamer einfrieren: Beamer freeze state is unknown", answer["areas"]["desktop"]["reason"])
            self.assertTrue(answer["areas"]["song"]["ok"])

    def test_unknown_freeze_state_fails_at_once(self):
        with Fixture(self, settle=0.5) as f:
            f.freeze_times_out()
            item = f.create(desktop=[TRANSLATION_STEP, "@freeze"])
            started = time.monotonic()
            answer = f.play(item)
            self.assertLess(time.monotonic() - started, 0.4, "no settle wait for a freeze that cannot be sent")
            self.assertIn("freeze state is unknown", answer["areas"]["desktop"]["reason"])

    def test_power_on_or_off_makes_it_live_again(self):
        with Fixture(self) as f:
            for key in ("beamer-power-on", "beamer-power-off"):
                f.freeze_times_out()
                self.assertTrue(f.client.post(f"/companion/buttons/{key}/press").json()["ok"])
                self.assertIs(f.client.get("/beamer/state").json()["frozen"], False, key)

    def test_a_document_not_shown_in_time_skips_the_freeze(self):
        with Fixture(self) as f:
            f.frozen(False)
            real = f.events.post

            def post(url, token, payload, timeout):
                answer = real(url, token, payload, timeout)
                return {**answer, "shown": False} if url.endswith("/display") else answer
            with patch("app.display_agent._post", post):
                answer = f.play(f.create(desktop=[TRANSLATION_STEP, "@freeze", "@song"], song="lied.foil"))
            self.assertEqual(f.events.order(), ["/display", "/song"])
            self.assertIn("Beamer einfrieren: übersprungen", answer["areas"]["desktop"]["reason"])

    def test_failed_step_skips_the_freeze_right_after_it_only(self):
        with Fixture(self) as f:
            f.frozen(False)
            f.events.dead.add((BEAMER_PC, TRANSLATION))
            item = f.create(desktop=[TRANSLATION_STEP, "@freeze", "monitor1", "@song"], song="lied.foil")
            with self.assertLogs("mixer.program", "WARNING") as logs:
                answer = f.play(item)
            self.assertEqual(f.events.order(), ["/display", "/display", "/song"])  # no freeze, the rest runs
            reason = answer["areas"]["desktop"]["reason"]
            self.assertIn("Beamer einfrieren: übersprungen", reason)
            self.assertTrue(answer["areas"]["desktop"]["partial"])
            self.assertTrue(answer["areas"]["song"]["ok"])
            self.assertTrue(any("skipped" in line for line in logs.output))

    def test_song_step_where_it_stands_or_last(self):
        with Fixture(self) as f:
            first = f.create(desktop=["@song", "monitor1"], song="lied.foil")
            f.play(first)
            self.assertEqual(f.events.order(), ["/song", "/display"])
            f.events.log.clear()
            old = f.create(desktop=["monitor1"])
            f.app.state.program_items._items[-1]["song"] = "lied.foil"  # a song without its step (older data)
            f.play(old)
            self.assertEqual(f.events.order(), ["/display", "/song"])
            f.events.log.clear()
            nosong = f.create(desktop=["@song", "monitor1"])
            self.assertTrue(f.play(nosong)["ok"])
            self.assertEqual(f.events.order(), ["/display"])

    def test_a_confirmed_picture_is_not_waited_for(self):
        for shown, at_least, below in ((True, 0.0, 0.25), (False, 0.3, 10.0)):
            with Fixture(self, settle=0.3) as f:
                f.frozen(False)
                f.events.shown = shown
                item = f.create(desktop=[TRANSLATION_STEP, "@freeze"])
                started = time.monotonic()
                self.assertTrue(f.play(item)["ok"])
                took = time.monotonic() - started
                self.assertTrue(at_least <= took < below, (shown, took))

    def test_stop_ends_what_the_program_started(self):
        with Fixture(self) as f:
            item = f.create(desktop=[row_files.via_value("musik", r"C:\Musik\Intro.mp3"), "monitor1"])
            f.play(item)
            self.assertEqual(f.client.get("/program/status").json()["started"], 2)
            answer = f.client.post("/program/stop").json()
            self.assertEqual(answer, {"ok": True, "stopped": 2})
            stops = [(pc, path) for pc, path in ((e[1], e[2]) for e in f.events.log if e[0] == "agent")
                     if path == "/close"] + [(pc, "stop") for pc, p in f.events.agent("/audio") if p["action"] == "stop"]
            self.assertEqual(sorted(stops), [(BEAMER_PC, "/close"), (STREAMING_PC, "stop")])
            self.assertEqual(f.client.get("/program/status").json()["started"], 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
