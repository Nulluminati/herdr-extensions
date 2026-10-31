"""Display icons stay optional, keep labels, and fit narrow card layouts."""

import unittest

from rich.cells import cell_len

from herdboard import attachments, board
from support.herdboard_board import BoardTestCase, settle
from support.herdboard_icons import ATTACHMENT, CALENDAR, TYPE_ICONS


class IconBoardTests(BoardTestCase):
    async def test_modes_reach_cards_details_and_card_replacement_at_narrow_widths(self):
        item = self.store.add(self.repo_path, "Ship it", "Backlog", type="feature", tags=["api"], due="2026-11-01")
        source = self.root / "spec.md"
        source.write_text("# Spec\n")
        attachments.add(self.store, item.ref, source)
        stored = (self.repo_path / "board.toml").read_bytes()
        for width in (24, 80):
            for style in ("plain", "nerd"):
                with self.subTest(width=width, style=style):
                    self.config.icon_style = style
                    self.config.tag_style = "block"
                    app = self.make_app()
                    async with app.run_test(size=(width, 30)) as pilot:
                        await pilot.pause()
                        widget = app.query_one(".card")
                        self.assertIn(
                            f"{TYPE_ICONS['feature']} feature" if style == "nerd" else "feature", widget.plain
                        )
                        self.assertIn(f"{CALENDAR} Due Nov 1" if style == "nerd" else "Due Nov 1", widget.plain)
                        label = ATTACHMENT if style == "nerd" else "files"
                        self.assertIn(label, widget.plain)
                        first = widget.render_line(0).text.rstrip()
                        self.assertTrue(first.endswith(f"{label} {item.ref}"), first)
                        self.assertEqual(cell_len(first), widget.size.width)
                        await pilot.press("enter")
                        await settle(pilot)
                        screen = app.screen
                        self.assertIsInstance(screen, board.DetailScreen)
                        self.assertIn(
                            f"{TYPE_ICONS['feature']} feature" if style == "nerd" else "feature",
                            screen.query_one("#detail-meta").plain,
                        )
                        self.assertIn(
                            f"{CALENDAR} Due Nov 1" if style == "nerd" else "Due Nov 1",
                            screen.query_one("#detail-chips").plain,
                        )
                        self.assertEqual(
                            str(screen.query_one("#detail-files").border_title),
                            f"{ATTACHMENT} Attachments" if style == "nerd" else "Attachments",
                        )
                        await pilot.press("escape")
                        self.store.edit(item.ref, body=["Updated details"])
                        await app.reload(item.ref)
                        await pilot.pause()
                        self.assertIn("Updated details", app.query_one(".card").plain)
                        if style == "nerd":
                            self.assertIn(TYPE_ICONS["feature"], app.query_one(".card").plain)
                        self.store.edit(item.ref, body=[])
        self.assertEqual((self.repo_path / "board.toml").read_bytes(), stored)

    async def test_round_tags_stay_independent_in_plain_mode(self):
        self.config.icon_style = "plain"
        self.config.tag_style = "round"
        self.store.add(self.repo_path, "Ship it", "Backlog", tags=["api"])
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            self.assertIn(f"{board.ROUND_LEFT}api{board.ROUND_RIGHT}", app.query_one(".card").plain)


if __name__ == "__main__":
    unittest.main()
