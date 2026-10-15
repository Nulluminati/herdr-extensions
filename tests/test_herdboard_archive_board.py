import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard import start  # noqa: E402
from herdboard.board import AddScreen, ConfirmScreen, HelpScreen  # noqa: E402
from test_herdboard_board import SIZE, BoardTestCase, settle  # noqa: E402


class ArchiveBoardTests(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.open = self.store.add(self.repo_path, "open card", "Todo")
        self.done = self.store.done(self.store.add(self.repo_path, "done card", "Todo").ref)
        self.store.done(self.store.add(self.repo_path, "done too", "Todo").ref)

    def titles(self, path):
        return [card.title for card in self.store.list_cards([path])]

    def archived(self, path):
        return [card.title for card in self.store.archived_cards([path])]

    async def test_the_archive_column_is_hidden_until_z(self):
        self.store.archive(self.done.ref)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertNotIn("Archive 1", self.headers(app))
            await pilot.press("z")
            await settle(pilot)
            self.assertEqual(self.headers(app)[-1], "Archive 1")
            self.assertIn("done card", "\n".join(self.cards(app)))
            self.assertIn("archive shown", app.query_one("#title-bar").plain)
            self.assertIn("archive", app.query(".column")[-1].classes)
            await pilot.press("z")
            await settle(pilot)
            self.assertNotIn("Archive 1", self.headers(app))

    async def test_an_empty_archive_shows_as_an_empty_column(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("z")
            await settle(pilot)
            self.assertEqual(self.headers(app)[-1], "Archive 0")

    async def test_A_archives_the_card_and_unarchives_an_archived_card(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("right")
            self.assertIn("open card", self.selected(app)[0])
            await pilot.press("A")
            await settle(pilot)
            self.assertEqual(self.archived(self.repo_path), ["open card"])
            await pilot.press("z")
            await settle(pilot)
            app.col = len(app.columns) - 1
            app.row = 0
            await pilot.press("A")
            await settle(pilot)
            self.assertEqual(self.archived(self.repo_path), [])
            card = next(c for c in self.store.list_cards([self.repo_path]) if c.title == "open card")
            self.assertEqual(card.column, "Done")

    async def test_A_on_a_sub_card_asks_before_it_unarchives_the_parent(self):
        parent = self.store.add(self.repo_path, "parent card", "Todo")
        child = self.store.add(self.repo_path, "child card", "Todo", parent=parent.ref)
        self.store.archive(child.ref)
        self.store.archive(parent.ref)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("z")
            await settle(pilot)
            app.col = len(app.columns) - 1
            app.row = [c.title for c in app.columns[app.col].cards].index(child.title)
            await pilot.press("A")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            self.assertIn(parent.ref, app.screen.query_one("#confirm-prompt").plain)
            await pilot.press("n")
            await settle(pilot)
            self.assertIn("child card", "\n".join(self.archived(self.repo_path)))
            await pilot.press("A")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            self.assertNotIn("child card", "\n".join(self.archived(self.repo_path)))
            self.assertNotIn("parent card", self.archived(self.repo_path))

    async def test_a_in_the_archive_column_adds_nothing(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("z")
            await settle(pilot)
            app.col = len(app.columns) - 1
            await pilot.press("a")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, AddScreen)

    async def test_A_refuses_an_active_card(self):
        app = self.make_app()
        with mock.patch.object(start, "archive_blocker", return_value="its agent still runs") as blocker:
            async with app.run_test(size=SIZE) as pilot:
                await pilot.pause()
                await pilot.press("right", "A")
                await settle(pilot)
                self.assertEqual(self.archived(self.repo_path), [])
        blocker.assert_called_once_with(self.open.ref)

    async def test_a_move_right_from_the_last_column_archives(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("z")
            await settle(pilot)
            app.col = [c.name for c in app.columns].index("Done")
            app.row = 0
            await pilot.press("L")
            await settle(pilot)
            self.assertEqual(self.archived(self.repo_path), ["done card"])
            self.assertIn("done card", self.selected(app)[0])
            await pilot.press("H")
            await settle(pilot)
            self.assertEqual(self.archived(self.repo_path), ["done card"])

    async def test_D_archives_the_done_column_after_y(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("D")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            self.assertIn("2 cards", app.screen.query_one("#confirm-prompt").plain)
            await pilot.press("n")
            await settle(pilot)
            self.assertEqual(self.archived(self.repo_path), [])
            await pilot.press("D")
            await pilot.pause()
            await pilot.press("y")
            await settle(pilot)
            self.assertEqual(self.archived(self.repo_path), ["done card", "done too"])
            self.assertEqual(self.titles(self.repo_path), ["open card"])

    async def test_D_skips_a_board_without_a_done_column(self):
        other = self.root / "todos/projects/no-done.md"
        other.write_text("---\n\nkanban-plugin: board\n\n---\n\n## Todo\n\n- [ ] x\n\n")
        app = self.make_app("all")
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("D")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            self.assertIn("2 cards", app.screen.query_one("#confirm-prompt").plain)
            await pilot.press("y")
            await settle(pilot)
            self.assertEqual(self.archived(self.repo_path), ["done card", "done too"])

    async def test_D_without_done_cards(self):
        self.store.archive_done(self.repo_path)
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("D")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, ConfirmScreen)

    async def test_help_lists_the_archive_keys(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertIsInstance(app.screen, HelpScreen)
            keys = [w.plain for w in app.screen.query(".help-key")]
            for key in ("A", "D", "z"):
                self.assertIn(key, keys)
            whats = [w.plain for w in app.screen.query(".help-what")]
            self.assertTrue(any("archive the todo" in what for what in whats))


if __name__ == "__main__":
    unittest.main()
