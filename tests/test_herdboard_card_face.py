"""Card rows, column colors and card styles on the board."""

from io import StringIO
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from rich.console import Console  # noqa: E402
from herdboard import board  # noqa: E402
from herdboard.board import ColumnData  # noqa: E402
from herdboard.store import CardView  # noqa: E402
from test_herdboard_board import BoardTestCase, settle  # noqa: E402


def card(**kwargs):
    fields = dict(ref="t123", board="api", path=None, column="Todo", title="Ship it",
                  checked=False, due=None, tags=[], body=[])
    fields.update(kwargs)
    return CardView(**fields)


def rendered(widget: board.CardWidget, width: int = 40) -> list[str]:
    output = StringIO()
    Console(file=output, width=width, color_system=None).print(widget.content)
    return output.getvalue().splitlines()


class CardFaceTests(unittest.TestCase):
    def test_the_state_icon_shows_not_started_started_and_done(self):
        cases = ((card(), False, "○"), (card(worktree=True), False, "●"), (card(agent_review="pi"), False, "●"),
                 (card(pr_writing=True), False, "●"), (card(planning="claude"), False, "●"),
                 (card(worktree=True), True, "✓"))
        for item, done, icon in cases:
            with self.subTest(item=item, done=done):
                text = board.card_text(item, False, done=done)
                self.assertTrue(text.plain.startswith(f"{icon} Ship it"), text.plain)
                widget = board.CardWidget(item, False, done=done)
                self.assertTrue(widget.plain.startswith("Ship it"), widget.plain)

    def test_done_cards_strike_their_title_and_description(self):
        text = board.card_text(card(body=["Initial release"]), False, done=True)
        struck = [text.plain[span.start:span.end] for span in text.spans if "strike" in str(span.style)]
        self.assertEqual(struck, ["Ship it", "Initial release"])
        self.assertTrue(board.CardWidget(card(), False, done=True).has_class("done"))

    def test_the_description_is_the_first_detail_line_that_is_not_a_checklist_item(self):
        item = card(body=["- [ ] write notes", "", "  - Speed up the search", "More text"])
        self.assertEqual(board.CardWidget(item, False).plain, "Ship it  t123\nSpeed up the search")

    def test_the_description_takes_at_most_two_rows(self):
        item = card(body=["word " * 40])
        lines = rendered(board.CardWidget(item, False), width=24)
        self.assertEqual(len(lines), 3, lines)
        self.assertTrue(lines[2].rstrip().endswith("…"), lines)

    def test_compact_cards_have_no_description(self):
        item = card(body=["Speed up the search"])
        widget = board.CardWidget(item, False, card_style="compact")
        self.assertEqual(widget.plain, "Ship it  t123")
        self.assertTrue(widget.has_class("compact"))

    def test_the_sub_card_count_shows_only_for_a_parent(self):
        self.assertIn("2/5 done", board.CardWidget(card(sub_cards=(2, 5)), False).plain)
        self.assertNotIn("done", board.CardWidget(card(), False).plain)
        nerd = board.CardWidget(card(sub_cards=(2, 5)), False, icon_style="nerd").plain
        self.assertIn(f"{board.ICONS['sub_cards']} 2/5 done", nerd)

    def test_the_footer_has_the_count_at_the_left_and_the_due_date_at_the_right(self):
        item = card(title="Ship it 📅 2026-11-01", sub_cards=(1, 2))
        footer = rendered(board.CardWidget(item, False), width=30)[-1]
        self.assertTrue(footer.startswith("1/2 done"), footer)
        self.assertTrue(footer.rstrip().endswith("Due Nov 1"), footer)

    def test_the_status_row_shows_only_when_there_is_status(self):
        self.assertEqual(board.CardWidget(card(tags=["api"], title="Ship it #api"), False).plain,
                         "Ship it  t123\n api ")
        widget = board.CardWidget(card(pr_state="open", parent_title="Billing"), False)
        self.assertEqual(widget.plain.splitlines()[-1], "PR open · ↳ Billing")

    def test_linear_cards_show_their_identifier_at_the_right(self):
        item = card(source="linear", ref="ENG-12")
        self.assertEqual(board.CardWidget(item, False).plain, f"Ship it  {board.LINEAR_MARK} ENG-12")

    def test_cards_without_a_block_id_show_no_id(self):
        for ref in ("api:3", "api:archive-2"):
            with self.subTest(ref=ref):
                self.assertEqual(board.CardWidget(card(ref=ref), False).plain, "Ship it")

    def test_chips_wrap_only_between_two_chips(self):
        item = card(title="Ship it #enhancement #low #documentation", type="feature")
        lines = rendered(board.CardWidget(item, False), width=24)
        self.assertEqual([line.rstrip() for line in lines[1:]], ["feature  enhancement", " low   documentation"])

    def test_outline_tags_are_text_in_their_color_on_a_tint(self):
        chip = board.tag_chip("api", "outline")
        color = board.chip_color("api")
        self.assertEqual(chip.plain, " api ")
        self.assertEqual(str(chip.style), f"{color} on {board.chip_tint(color)}")
        self.assertNotEqual(board.chip_tint(color, dark=True), board.chip_tint(color, dark=False))


class ColumnColorTests(unittest.TestCase):
    def test_columns_take_the_palette_colors_in_turn(self):
        names = [board.column_class(index, ColumnData(f"c{index}")) for index in range(7)]
        self.assertEqual(names, ["col-0", "col-1", "col-2", "col-3", "col-4", "col-0", "col-1"])

    def test_the_completed_column_is_green_and_the_archive_muted(self):
        self.assertEqual(board.column_class(2, ColumnData("Done", completed=True)), "col-done")
        self.assertEqual(board.column_class(5, ColumnData("Archive", archive=True)), "col-archive")

    def test_the_column_header_has_the_icon_name_and_count(self):
        output = StringIO()
        header = board.ColumnHeader(ColumnData("Todo", limit=5, cards=[card()]))
        Console(file=output, width=20, color_system=None).print(header.content)
        self.assertEqual(output.getvalue().rstrip("\n"), "○ Todo           1/5")
        self.assertEqual(header.plain, "Todo 1/5")
        done = board.ColumnHeader(ColumnData("Done", completed=True))
        started = board.ColumnHeader(ColumnData("In progress"), "In progress")
        for widget, icon in ((done, "✓"), (started, "●")):
            output = StringIO()
            Console(file=output, width=20, color_system=None).print(widget.content)
            self.assertTrue(output.getvalue().startswith(icon), output.getvalue())


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
