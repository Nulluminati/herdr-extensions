"""Card rows, column colors and card styles on the board."""

import unittest


from support.herdboard_board import BoardTestCase, settle


class CardStyleBoardTests(BoardTestCase):
    async def test_columns_get_their_color_class_and_cards_their_style(self):
        self.store.add(self.repo_path, "Ship it", "Done")
        self.config.card_style = "compact"
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            columns = list(app.query(".column"))
            self.assertEqual([next(c for c in w.classes if c.startswith("col-")) for w in columns],
                             ["col-0", "col-1", "col-2", "col-3", "col-done"])
            widget = app.query_one(".card")
            self.assertTrue(widget.has_class("compact") and widget.has_class("done"))

    async def test_a_card_style_change_redraws_cards_with_the_same_text(self):
        self.store.add(self.repo_path, "Ship it", "Todo")
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one(".card").has_class("compact"))
            self.config.card_style = "compact"
            await app.reload()
            await settle(pilot)
            self.assertTrue(app.query_one(".card").has_class("compact"))

    async def test_cards_show_agent_logos_when_the_app_has_them(self):
        self.store.add(self.repo_path, "Ship it", "Todo")
        app = self.make_app()
        app.agent_logos = True
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            self.assertTrue(app.query_one(".card").agent_logos)

    async def test_the_current_column_header_is_not_filled(self):
        self.store.add(self.repo_path, "Ship it", "Todo")
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            header = app.query_one(".column.current .column-header")
            self.assertEqual(header.styles.background.a, 0)
            self.assertEqual(header.styles.color.ansi, 4)
            self.assertTrue(header.styles.text_style.underline)

    async def test_the_card_scrollbar_has_the_column_color(self):
        for _ in range(12):
            self.store.add(self.repo_path, "Ship it", "Todo")
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            cards = app.query_one(".col-1 .cards")
            magenta = 5
            self.assertEqual(cards.styles.scrollbar_color.ansi, magenta)
            self.assertEqual(cards.styles.scrollbar_color_hover.ansi, magenta)
            self.assertEqual(cards.styles.scrollbar_color_active.ansi, magenta)

    async def test_full_cards_have_a_border_in_the_column_color(self):
        self.store.add(self.repo_path, "first", "Todo")
        self.store.add(self.repo_path, "second", "Todo")
        app = self.make_app()
        async with app.run_test(size=(150, 30)) as pilot:
            await pilot.pause()
            card_widget = next(w for w in app.query(".card") if not w.has_class("selected"))
            magenta = 5
            self.assertEqual(card_widget.styles.border_top[0], "round")
            self.assertEqual(card_widget.styles.border_top[1].ansi, magenta)


if __name__ == "__main__":
    unittest.main()
