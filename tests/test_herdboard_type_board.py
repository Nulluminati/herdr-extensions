import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard.board import AddScreen, DetailScreen, EditScreen  # noqa: E402
from test_herdboard_board import SIZE, BoardTestCase, settle  # noqa: E402


class TypeLookTests(BoardTestCase):
    async def test_a_typed_card_shows_its_type_before_the_tags(self):
        self.store.add(self.repo_path, "Fix login #type/bug #auth", "Todo")
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            widget = app.query_one(".card")
            lines = widget.plain.splitlines()
            self.assertEqual(lines[0], "Fix login")
            self.assertTrue(lines[1].startswith("bug "), lines[1])
            self.assertNotIn("type/bug", widget.plain)
            chips = [widget.rich_text.plain[span.start:span.end] for span in widget.rich_text.spans
                     if " on " in str(span.style)]
            self.assertEqual(chips, ["auth"])

    async def test_a_card_without_a_type_shows_no_badge(self):
        self.store.add(self.repo_path, "Fix login", "Todo")
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one(".card").plain, "Fix login")

    async def test_the_detail_shows_the_type(self):
        self.store.add(self.repo_path, "Fix login #type/feature #auth", "Todo")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("right", "enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            self.assertIn("Type", app.screen.query_one("#detail-meta").plain)
            self.assertIn("feature", app.screen.query_one("#detail-meta").plain)
            self.assertNotIn("type/feature", app.screen.query_one("#detail-chips").plain)

    async def test_hash_type_filters_by_type(self):
        self.store.add(self.repo_path, "Fix login #type/bug", "Todo")
        self.store.add(self.repo_path, "Add export #type/feature", "Todo")
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            await pilot.press("slash", *"#type/bug")
            await pilot.pause()
            visible = [card.title for column in app.columns for card in column.cards]
            self.assertEqual(visible, ["Fix login #type/bug"])


class TypeFormTests(BoardTestCase):
    async def test_add_with_a_type(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, AddScreen)
            screen.query_one("#card-title").value = "Fix login"
            screen.query_one("#card-type").value = "bug"
            screen.query_one("#card-tags").value = "auth"
            await pilot.press("ctrl+s")
            await settle(pilot)
            card = self.store.list_cards([self.repo_path])[0]
            self.assertEqual(card.title, "Fix login #type/bug #auth")

    async def test_the_type_list_comes_from_the_config(self):
        self.config.types = ["spike", "bug"]
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()
            select = app.screen.query_one("#card-type")
            self.assertEqual([value for _, value in select._options if isinstance(value, str)], ["spike", "bug"])

    async def test_edit_shows_and_changes_the_type(self):
        card = self.store.add(self.repo_path, "Fix login #type/bug #auth", "Backlog")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            screen = app.screen
            self.assertIsInstance(screen, EditScreen)
            self.assertEqual(screen.query_one("#card-type").value, "bug")
            self.assertEqual(screen.query_one("#card-tags").value, "auth")
            screen.query_one("#card-type").value = "chore"
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertEqual(self.store.show(card.ref).title, "Fix login #type/chore #auth")

    async def test_edit_clears_the_type(self):
        card = self.store.add(self.repo_path, "Fix login #type/bug", "Backlog")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            screen = app.screen
            screen.query_one("#card-type").clear()
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertEqual(self.store.show(card.ref).title, "Fix login")

    async def test_edit_keeps_a_type_that_the_config_no_longer_lists(self):
        card = self.store.add(self.repo_path, "Try it #type/spike", "Backlog")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#card-type").value, "spike")
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertEqual(self.store.show(card.ref).title, "Try it #type/spike")


if __name__ == "__main__":
    unittest.main()
