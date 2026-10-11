import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import kanban_md  # noqa: E402
from herdboard.model import Card, NotFoundError  # noqa: E402

FIXTURES = ROOT / "tests/fixtures/herdboard"
FULL = (FIXTURES / "kanban-2.0.51-full.md").read_text()
EMPTY = (FIXTURES / "kanban-2.0.51-empty.md").read_text()

SETTINGS = '%% kanban:settings\n```\n{"kanban-plugin":"board"}\n```\n%%'
HEAD = "---\n\nkanban-plugin: board\n\n---\n\n"


def board_text(*columns: str) -> str:
    return HEAD + "".join(columns) + "\n\n" + SETTINGS


class RoundTripTest(unittest.TestCase):
    def assertRoundTrip(self, text):
        self.assertEqual(kanban_md.serialize(kanban_md.parse(text)), text)

    def test_fixtures(self):
        self.assertRoundTrip(FULL)
        self.assertRoundTrip(EMPTY)

    def test_empty_string(self):
        self.assertRoundTrip("")

    def test_no_frontmatter(self):
        text = "## A\n\n- [ ] one\n\n\n"
        self.assertRoundTrip(text)
        self.assertEqual(kanban_md.parse(text).frontmatter, [])

    def test_unclosed_frontmatter(self):
        self.assertRoundTrip("---\nkey: value\n")

    def test_no_settings_block(self):
        self.assertRoundTrip(FULL.split("%% kanban:settings")[0])

    def test_no_archive(self):
        text = board_text("## A\n\n- [ ] one\n\n\n")
        self.assertRoundTrip(text)
        self.assertIsNone(kanban_md.parse(text).archive)

    def test_crlf(self):
        text = FULL.replace("\n", "\r\n")
        board = kanban_md.parse(text)
        self.assertEqual(kanban_md.serialize(board), text)
        self.assertEqual(len(board.columns), 5)
        self.assertEqual(len(board.columns[0].cards[1].lines), 4)

    def test_trailing_newline(self):
        self.assertRoundTrip(FULL + "\n")
        self.assertRoundTrip(FULL.split("%% kanban:settings")[0] + "\n")
        self.assertRoundTrip("## A\n- [ ] x")

    def test_unknown_lines_between_cards(self):
        text = "## A\n\n- [ ] one\nloose text\n- [ ] two\n  \n# other\n\n"
        board = kanban_md.parse(text)
        self.assertEqual(kanban_md.serialize(board), text)
        self.assertEqual(len(board.columns[0].cards), 2)
        self.assertIn("loose text", board.columns[0].items)

    def test_card_like_line_in_preamble(self):
        text = "- [ ] stray\nnotes\n## A\n\n- [ ] one\n"
        board = kanban_md.parse(text)
        self.assertEqual(board.preamble, ["- [ ] stray", "notes"])
        self.assertEqual(kanban_md.serialize(board), text)

    def test_four_space_indentation(self):
        text = "## A\n\n- [ ] one\n    body\n    - [ ] nested\n- [ ] two\n\n\n"
        board = kanban_md.parse(text)
        self.assertEqual(kanban_md.serialize(board), text)
        self.assertEqual(board.columns[0].cards[0].body, ["    body", "    - [ ] nested"])

    def test_star_line_without_archive_heading(self):
        text = "## A\n\n- [ ] one\n\n***\n\ntext\n"
        board = kanban_md.parse(text)
        self.assertEqual(kanban_md.serialize(board), text)
        self.assertIsNone(board.archive)
        self.assertEqual(board.archive_lines, [])
        self.assertIn("***", board.columns[0].items)
        self.assertIn("text", board.columns[0].items)


class ArchiveDetectionTest(unittest.TestCase):
    def test_star_line_between_ordinary_columns(self):
        text = "## A\n\n- [ ] one\n\n***\n\n## More\n\n- [ ] two\n\n"
        board = kanban_md.parse(text)
        self.assertEqual(kanban_md.serialize(board), text)
        self.assertIsNone(board.archive)
        self.assertEqual([c.title for c in board.columns], ["A", "More"])
        self.assertIn("***", board.columns[0].items)
        self.assertEqual([c.title for _, c in board.all_cards()], ["one", "two"])

    def test_star_line_before_archive_heading(self):
        text = "## A\n\n- [ ] one\n\n***\n\n## Archive\n\n- [x] old\n\n"
        board = kanban_md.parse(text)
        self.assertEqual(kanban_md.serialize(board), text)
        self.assertEqual(board.archive.title, "Archive")
        self.assertEqual(board.archive_lines, ["***", ""])
        self.assertEqual([c.title for c in board.columns], ["A"])

    def test_archive_heading_without_star_line_is_a_column(self):
        text = "## A\n\n- [ ] one\n\n## Archive\n\n- [ ] two\n"
        board = kanban_md.parse(text)
        self.assertIsNone(board.archive)
        self.assertEqual([c.title for c in board.columns], ["A", "Archive"])

    def test_star_line_then_other_heading_then_archive(self):
        text = "## A\n\n***\n\n## More\n\n***\n\n## Archive\n\n- [x] old\n"
        board = kanban_md.parse(text)
        self.assertEqual(kanban_md.serialize(board), text)
        self.assertEqual([c.title for c in board.columns], ["A", "More"])
        self.assertEqual(board.archive.title, "Archive")

    def test_localized_archive_title(self):
        text = "## A\n\n***\n\n## Archiv\n\n- [x] old\n"
        default = kanban_md.parse(text)
        self.assertIsNone(default.archive)
        board = kanban_md.parse(text, archive_titles=("Archive", "Archiv"))
        self.assertEqual(kanban_md.serialize(board), text)
        self.assertEqual(board.archive.title, "Archiv")
        self.assertEqual([c.title for c in board.columns], ["A"])


class LineEndingTest(unittest.TestCase):
    def test_newline_detected(self):
        self.assertEqual(kanban_md.parse(FULL).newline, "\n")
        self.assertEqual(kanban_md.parse(FULL.replace("\n", "\r\n")).newline, "\r\n")

    def test_lines_never_contain_carriage_return(self):
        board = kanban_md.parse(FULL.replace("\n", "\r\n"))
        lines = list(board.frontmatter) + list(board.preamble) + list(board.archive_lines)
        for column in board.columns + [board.archive]:
            lines.append(column.heading_line)
            for item in column.items:
                lines.extend(item.lines if isinstance(item, Card) else [item])
        self.assertEqual([line for line in lines if "\r" in line], [])

    def test_card_ids_and_markers_recognised_in_crlf(self):
        board = kanban_md.parse(FULL.replace("\n", "\r\n"))
        ids = [card.block_id for _, card in board.all_cards()]
        self.assertEqual(ids, ["t3m9", None, "t7k2", None, None, "t2a6"])
        self.assertTrue(board.column("Done").has_marker(["Complete"]))

    def test_serialize_uses_board_newline(self):
        board = kanban_md.parse(FULL.replace("\n", "\r\n"))
        board.columns[0].cards[1].set_block_id("tnew")
        board.columns[0].items.append(Card(["- [ ] added"]))
        out = kanban_md.serialize(board)
        self.assertNotRegex(out, r"(?<!\r)\n")
        self.assertNotRegex(out, r"\r(?!\n)")


class ParseFullTest(unittest.TestCase):
    def setUp(self):
        self.board = kanban_md.parse(FULL)

    def test_frontmatter_and_preamble(self):
        self.assertEqual(
            self.board.frontmatter, ["---", "", "kanban-plugin: board", "", "---"]
        )
        self.assertEqual(self.board.preamble, [""])
        self.assertEqual(self.board.frontmatter_value("kanban-plugin"), "board")

    def test_column_titles(self):
        self.assertEqual(
            [c.title for c in self.board.columns],
            ["Backlog", "Todo", "In progress", "Review", "Done"],
        )

    def test_limit(self):
        self.assertEqual(self.board.column("Todo").limit, 5)
        self.assertIsNone(self.board.column("Backlog").limit)

    def test_complete_marker(self):
        self.assertTrue(self.board.column("Done").has_marker(["Complete"]))
        self.assertFalse(self.board.column("Todo").has_marker(["Complete"]))

    def test_multi_line_card(self):
        card = self.board.column("Backlog").cards[1]
        self.assertEqual(card.title, "Plan the lighting")
        self.assertEqual(
            card.body,
            ["\tSecond line of the same card", "\t- [ ] Nested item one", "\t- [x] Nested item two"],
        )

    def test_block_ids(self):
        ids = [card.block_id for _, card in self.board.all_cards()]
        self.assertEqual(ids, ["t3m9", None, "t7k2", None, None, "t2a6"])

    def test_archive(self):
        archive = self.board.archive
        self.assertTrue(archive.archive)
        self.assertEqual(archive.title, "Archive")
        self.assertEqual([c.block_id for c in archive.cards], ["t1x0"])
        self.assertEqual(self.board.archive_lines, ["***", ""])

    def test_tail(self):
        self.assertTrue(self.board.tail.startswith("%% kanban:settings"))
        self.assertTrue(self.board.tail.endswith("\n%%"))


class NewBoardTest(unittest.TestCase):
    def test_matches_empty_fixture(self):
        self.assertEqual(
            kanban_md.new_board(["Backlog", "Todo", "Done"], "Done"), EMPTY
        )

    def test_extra_frontmatter_and_no_complete(self):
        text = kanban_md.new_board(["A"], None, [("repo", "x/y")])
        self.assertEqual(
            text,
            "---\n\nkanban-plugin: board\nrepo: x/y\n\n---\n\n## A\n\n\n\n\n\n" + SETTINGS,
        )
        board = kanban_md.parse(text)
        self.assertEqual(board.frontmatter_value("repo"), "x/y")
        self.assertEqual(kanban_md.serialize(board), text)


class MoveTest(unittest.TestCase):
    def move(self, text, source, target, position="bottom", index=0):
        board = kanban_md.parse(text)
        src, dst = board.column(source), board.column(target)
        card = src.cards[index]
        kanban_md.remove_card(src, card)
        kanban_md.insert_card(dst, card, position)
        return kanban_md.serialize(board)

    def test_to_empty_column(self):
        text = board_text(
            "## Backlog\n\n- [ ] a\n\n\n",
            "## Todo\n\n\n\n",
        )
        expected = board_text(
            "## Backlog\n\n\n\n",
            "## Todo\n\n- [ ] a\n\n\n",
        )
        self.assertEqual(self.move(text, "Backlog", "Todo"), expected)

    def test_to_top_and_bottom_of_non_empty_column(self):
        text = board_text(
            "## Backlog\n\n- [ ] a\n\t- [ ] sub\n\n\n",
            "## Todo\n\n- [ ] b\n- [ ] c\n\n\n",
        )
        top = board_text(
            "## Backlog\n\n\n\n",
            "## Todo\n\n- [ ] a\n\t- [ ] sub\n- [ ] b\n- [ ] c\n\n\n",
        )
        bottom = board_text(
            "## Backlog\n\n\n\n",
            "## Todo\n\n- [ ] b\n- [ ] c\n- [ ] a\n\t- [ ] sub\n\n\n",
        )
        self.assertEqual(self.move(text, "Backlog", "Todo", "top"), top)
        self.assertEqual(self.move(text, "Backlog", "Todo", "bottom"), bottom)

    def test_into_empty_complete_column(self):
        text = board_text(
            "## Backlog\n\n- [ ] a\n\n\n",
            "## Done\n\n**Complete**\n\n\n",
        )
        expected = board_text(
            "## Backlog\n\n\n\n",
            "## Done\n\n**Complete**\n- [ ] a\n\n\n",
        )
        self.assertEqual(self.move(text, "Backlog", "Done"), expected)

    def test_into_complete_column_with_cards(self):
        text = board_text(
            "## Backlog\n\n- [ ] a\n\n\n",
            "## Done\n\n**Complete**\n- [x] b\n\n\n",
        )
        top = board_text(
            "## Backlog\n\n\n\n",
            "## Done\n\n**Complete**\n- [ ] a\n- [x] b\n\n\n",
        )
        self.assertEqual(self.move(text, "Backlog", "Done", "top"), top)

    def test_move_in_full_fixture_matches_empty_layout(self):
        out = self.move(FULL, "Backlog", "In progress")
        self.assertIn(
            "## In progress\n\n- [ ] Compare tile suppliers #research ^t3m9\n\n\n## Review",
            out,
        )
        self.assertTrue(out.endswith(FULL[FULL.index("\n***\n"):]))

    def test_remove_last_card_gives_empty_column_layout(self):
        board = kanban_md.parse(FULL)
        review = board.column("Review")
        kanban_md.remove_card(review, review.cards[0])
        self.assertEqual(
            kanban_md.serialize(board),
            FULL.replace("- [ ] Check the grout colour\n", ""),
        )
        self.assertIn("## In progress\n\n\n\n## Review\n\n\n\n## Done", kanban_md.serialize(board))

    def test_remove_by_identity(self):
        board = kanban_md.parse(board_text("## A\n\n- [ ] same\n- [ ] same\n\n\n"))
        column = board.columns[0]
        kanban_md.remove_card(column, column.cards[1])
        self.assertEqual(len(column.cards), 1)
        with self.assertRaises(NotFoundError):
            kanban_md.remove_card(column, Card(["- [ ] same"]))

    def test_insert_rejects_bad_position(self):
        column = kanban_md.parse("## A\n\n").columns[0]
        with self.assertRaises(ValueError):
            kanban_md.insert_card(column, Card(["- [ ] x"]), "middle")


if __name__ == "__main__":
    unittest.main()
