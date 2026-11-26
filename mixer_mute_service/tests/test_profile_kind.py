"""A desktop profile's kind comes from its path (app/profile_kind.py), and the
editor shows the same kind live (app/static/js/desktop.js, profileKindFor).

The rules are checked on the service; the page's functions run in quickjs on
the same cases and must agree. The JS part skips without quickjs; with
REQUIRE_TK=1 (CI) a missing quickjs is an error.
"""

import json
import os
import re
import unittest
from pathlib import Path

from app.profile_kind import detect_kind, profile_kind

try:
    import quickjs
except ImportError:  # pragma: no cover - only where quickjs is not installed
    if os.environ.get("REQUIRE_TK") == "1":  # CI: never skip silently
        raise
    quickjs = None

DESKTOP_JS = Path(__file__).resolve().parent.parent / "app" / "static" / "js" / "desktop.js"

# (path, mission_spotlight, detected kind or None)
CASES = [
    (r"I:\Bilder\Gemeindegaben QR.png", False, "image"),
    (r"I:\Bilder\Foto.JPG", False, "image"),
    (r"I:\Bilder\Logo_*.webp", False, "image"),
    (r"I:\Musik\Hintergrundmusik.mp3", False, "audio"),
    (r"I:\Musik\Ansage_*.WAV", False, "audio"),
    ("https://youtu.be/dQw4w9WgXcQ", False, "audio"),
    ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", False, "audio"),
    (r"C:\Program Files\obs-studio\bin\64bit\obs64.exe", False, "program"),
    (r"C:\Program Files\obs-studio*\bin\64bit\obs64.exe", False, "program"),
    (r"C:\Users\Public\Desktop\OBS.lnk", False, "file"),
    (r"C:\Skripte\start.bat", False, "file"),
    (r"C:\Skripte\start.cmd", False, "file"),
    (r"D:\Videos\Intro.mp4", False, "file"),
    (r"D:\Videos\Intro.mov", False, "file"),
    (r"D:\Folien\Predigt.pptx", False, "file"),
    (r"D:\Ablauf.pdf", False, "file"),
    (r"C:\Links\Stream.url", False, "file"),
    (r"D:\Daten\unbekannt.xyz", False, "file"),
    (r"I:\Mission Spotlight", True, None),          # a folder: a new profile becomes a file
    (r"I:\Mission Spotlight\.hidden", True, None),
    ("https://youtu.be/dQw4w9WgXcQ", True, "audio"),   # a link stays audio (the service refuses it with Spotlight)
    (r"C:\Program Files\obs-studio\bin\64bit\obs64*", False, None),
    (r"C:\Ordner\ohne Endung", False, None),
    (r"C:\Ordner\.hidden", False, None),
    ("", False, None),
]


class DetectKindTests(unittest.TestCase):
    def test_cases(self):
        for path, spotlight, kind in CASES:
            with self.subTest(path=path, spotlight=spotlight):
                self.assertEqual(detect_kind(path, spotlight), kind)

    def test_new_profile(self):
        self.assertEqual(profile_kind(r"D:\Videos\Intro.mp4"), "file")
        self.assertEqual(profile_kind(r"C:\Apps\obs64*"), "file")                  # says nothing: file
        self.assertEqual(profile_kind(r"C:\Apps\obs64*", sent="program"), "program")  # an old page's kind counts there
        self.assertEqual(profile_kind(r"D:\Videos\Intro.mp4", sent="image"), "file")  # but never against the path
        self.assertEqual(profile_kind(r"I:\Musik\Lied.mp3", sent="script"), "audio")

    def test_edited_profile(self):
        stored = {"path": r"I:\Mission Spotlight", "kind": "audio", "mission_spotlight": True}
        # Unchanged path and Spotlight: the stored kind stays, whatever is sent.
        self.assertEqual(profile_kind(r"I:\Mission Spotlight", True, stored=stored), "audio")
        self.assertEqual(profile_kind(r"I:\Mission Spotlight", True, sent="image", stored=stored), "audio")
        # A new path (or Spotlight switched) re-derives it.
        self.assertEqual(profile_kind(r"I:\Musik\Lied.png", False, stored=stored), "image")
        self.assertEqual(profile_kind(r"I:\Mission Spotlight", False, stored=stored), "audio")  # folder says nothing: kept
        self.assertEqual(profile_kind(r"I:\Spotlight2", True, stored=stored), "audio")         # Spotlight audio stays audio
        self.assertEqual(profile_kind(r"I:\Spotlight", True), "file")                         # a new Spotlight profile
        self.assertEqual(profile_kind(r"I:\Spotlight", True, sent="audio"), "audio")          # an old page's kind
        program = {"path": r"C:\Apps\obs64*", "kind": "program", "mission_spotlight": False}
        self.assertEqual(profile_kind(r"C:\Apps\obs64*", True, stored=program), "file")       # a program cannot be one
        stored = {"path": r"C:\Apps\obs64*", "kind": "program", "mission_spotlight": False}
        self.assertEqual(profile_kind(r"C:\Apps2\obs64*", stored=stored), "program")


@unittest.skipIf(quickjs is None, "needs quickjs")
class PageAgreesTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        source = DESKTOP_JS.read_text(encoding="utf-8")
        consts = ("YOUTUBE_URL_RE", "PROFILE_IMAGE_EXT", "PROFILE_AUDIO_EXT", "PROFILE_PROGRAM_EXT",
                  "PROFILE_FILE_LABELS", "PROFILE_KIND_LABELS")
        parts = [re.search(rf"(?ms)^const {name} = .*?;$", source).group(0) for name in consts]
        parts += [re.search(rf"(?ms)^function {name}\(.*?\n\}}\n", source).group(0)
                  for name in ("isYoutubeUrl", "profileKindOf", "profileKindFor")]
        cls.js = quickjs.Context()
        cls.js.eval("\n".join(parts))

    def call(self, expression):
        return json.loads(self.js.eval(f"JSON.stringify({expression})"))

    def test_same_kind_as_the_service(self):
        for path, spotlight, kind in CASES:
            with self.subTest(path=path, spotlight=spotlight):
                found = self.call(f"profileKindOf({json.dumps(path)}, {json.dumps(spotlight)})")
                self.assertEqual(found["kind"] if found else None, kind)
                shown = self.call(f"profileKindFor({json.dumps(path)}, {json.dumps(spotlight)}, null)")
                self.assertEqual(shown["kind"], profile_kind(path, spotlight))

    def test_edited_profile_like_the_service(self):
        stored = {"path": r"I:\Mission Spotlight", "kind": "audio", "mission_spotlight": True}
        program = {"path": r"C:\Apps\obs64*", "kind": "program", "mission_spotlight": False}
        for path, spotlight, old in ((r"I:\Mission Spotlight", True, stored), (r"I:\Musik\Lied.png", False, stored),
                                     (r"I:\Mission Spotlight", False, stored), (r"C:\x\y*", False, stored),
                                     (r"I:\Spotlight2", True, stored), (r"C:\Apps\obs64*", True, program),
                                     (r"I:\Spotlight", True, None)):
            with self.subTest(path=path, spotlight=spotlight):
                shown = self.call(f"profileKindFor({json.dumps(path)}, {json.dumps(spotlight)}, {json.dumps(old)})")
                self.assertEqual(shown["kind"], profile_kind(path, spotlight, stored=old))

    def test_labels(self):
        self.assertEqual(self.call(r'profileKindOf("D:\\Videos\\Intro.mp4", false).label'), "Video")
        self.assertEqual(self.call(r'profileKindOf("https://youtu.be/dQw4w9WgXcQ", false).label'), "YouTube-Audio")


if __name__ == "__main__":
    unittest.main()
