import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

from app.companion_buttons import COMPANION_BUTTONS, CompanionButtonStore
from app.program_items import ProgramItemStore


def row(time, title, group="Chor"):
    return {"time": time, "title": title, "person": "", "group": group}


class ProgramStoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.path = Path(self.temp_dir.name) / "program.json"
        self.store = ProgramItemStore(self.path)

    async def test_status_counts_only_todays_starts(self):
        first = await self.store.create(row("10:00", "Lied"))
        second = await self.store.create(row("10:10", "Predigt"))
        now = datetime(2026, 11, 8, 10, 5).astimezone()
        await self.store.mark_played(first["id"], now - timedelta(days=7))
        status = await self.store.status(now)
        self.assertEqual(status, {"current_id": None, "next_id": first["id"]})

        await self.store.mark_played(first["id"], now)
        status = await self.store.status(now)
        self.assertEqual(status, {"current_id": first["id"], "next_id": second["id"]})

        await self.store.mark_played(second["id"], now + timedelta(minutes=5))
        status = await self.store.status(now)
        self.assertEqual(status, {"current_id": second["id"], "next_id": None})

    async def test_order_by_time_keeps_insertion_order_on_ties(self):
        a = await self.store.create(row("10:00", "A"))
        b = await self.store.create(row("09:00", "B"))
        c = await self.store.create(row("10:00", "C"))
        dup = await self.store.duplicate(a["id"])
        titles = [item["id"] for item in await self.store.list()]
        self.assertEqual(titles, [b["id"], a["id"], dup["id"], c["id"]])
        # Order survives a restart.
        reloaded = ProgramItemStore(self.path)
        self.assertEqual([i["id"] for i in await reloaded.list()], titles)

    async def test_group_is_optional(self):
        item = await self.store.create(row("10:00", "Nur Kamera", group=None))
        self.assertEqual(item["group"], "")

    async def test_rename_group(self):
        await self.store.create(row("10:00", "A", group="Alt"))
        await self.store.create(row("10:05", "B", group="Andere"))
        self.assertEqual(await self.store.rename_group("Alt", "Neu"), 1)
        self.assertEqual([i["group"] for i in await self.store.list()], ["Neu", "Andere"])


class MacroRenameTests(unittest.IsolatedAsyncioTestCase):
    async def test_renamed_macro_group_survives_catalog_update(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "buttons.json"
            store = CompanionButtonStore(path, COMPANION_BUTTONS)
            self.assertGreaterEqual(await store.rename_group("Predigt", "Predigt neu"), 1)
            data = json.loads(path.read_text(encoding="utf-8"))
            # Pretend an older catalog version: the update rewrites native
            # actions, but must keep the renamed group.
            data["native_catalog_version"] = 3
            path.write_text(json.dumps(data), encoding="utf-8")
            reloaded = CompanionButtonStore(path, COMPANION_BUTTONS)
            macro = await reloaded.get_button("native-overall-predigt")
            self.assertEqual(macro["native_action"]["group"], "Predigt neu")


if __name__ == "__main__":
    unittest.main()
