import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from herdboard import card_file
from herdboard.model import FormatError

PATH = Path("/todos/boards/inbox/cards/tabc/card.md")
BOARD_PATH = Path("/todos/boards/inbox/board.toml")
CREATED = datetime(2026, 10, 31, 10, 0, tzinfo=timezone(timedelta(hours=-7)))

CARD = """+++
title = "Drop obsidian support"
type = "refactor"
tags = ["herdboard"]
due = 2026-11-15
parent = "t5aa"
created = 2026-10-31T10:00:00-07:00
# A key that herdboard does not know.
colour = "blue"
+++

Body line one.

- [ ] item
"""

BOARD = """format = 1
repo = "github.com/acme/api"
merge = "pr"

[[linear-projects]]
id = "p1"
name = "Project"
labels = [{ id = "l1", name = "Bug" }]

[[column]]
name = "Todo"
limit = 5
cards = [
    "tw4d",
    "tvnn",
]

[[column]]
name = "Done"
done = true
cards = []
"""


class CardFileTest(unittest.TestCase):
    def test_parse_fields(self):
        card = card_file.parse_card(CARD, PATH)
        self.assertEqual(card.title, "Drop obsidian support")
        self.assertEqual(card.type, "refactor")
        self.assertEqual(card.tags, ["herdboard"])
        self.assertEqual(card.due, "2026-11-15")
        self.assertEqual(card.parent, "t5aa")
        self.assertIsNone(card.promoted)
        self.assertEqual(card.created, CREATED)
        self.assertIsNone(card.archived)
        self.assertEqual(card.body, ["Body line one.", "", "- [ ] item"])

    def test_round_trip_keeps_unknown_keys_and_comments(self):
        card = card_file.parse_card(CARD, PATH)
        self.assertEqual(card_file.dump_card(card), CARD)

    def test_set_fields(self):
        card = card_file.parse_card(CARD, PATH)
        card.title = "New title"
        card.tags = []
        card.type = None
        card.due = "2026-12-01"
        card.promoted = "ENG-1"
        text = card_file.dump_card(card)
        self.assertIn('title = "New title"\n', text)
        self.assertNotIn("tags", text)
        self.assertNotIn("type =", text)
        self.assertIn("due = 2026-12-01\n", text)
        self.assertIn('promoted = "ENG-1"\n', text)
        self.assertIn('colour = "blue"', text)
        self.assertEqual(card_file.parse_card(text, PATH).due, "2026-12-01")

    def test_new_card(self):
        card = card_file.new_card("Title", created=CREATED, tags=["a"], body=["x", "", ""])
        self.assertEqual(
            card_file.dump_card(card),
            '+++\ntitle = "Title"\ntags = ["a"]\ncreated = 2026-10-31T10:00:00-07:00\n+++\n\nx\n',
        )

    def test_no_body(self):
        card = card_file.new_card("Title", created=CREATED)
        text = card_file.dump_card(card)
        self.assertTrue(text.endswith("+++\n"))
        self.assertEqual(card_file.parse_card(text, PATH).body, [])

    def test_crlf(self):
        card = card_file.parse_card(CARD.replace("\n", "\r\n"), PATH)
        self.assertEqual(card.title, "Drop obsidian support")
        self.assertEqual(card.body[-1], "- [ ] item")

    def test_bad_due(self):
        card = card_file.new_card("Title", created=CREATED)
        with self.assertRaises(FormatError):
            card.due = "2026-13-01"
        with self.assertRaises(FormatError):
            card.due = "soon"

    def test_errors_name_the_path(self):
        bad = [
            "no fences",
            "+++\ntitle = 'x'\n",
            "+++\ntitle = \n+++\n",
            "+++\ntags = []\n+++\n",
            "+++\ntitle = ''\n+++\n",
            "+++\ntitle = 'x'\ntags = 'a'\n+++\n",
            "+++\ntitle = 'x'\ndue = 'tomorrow'\n+++\n",
            "+++\ntitle = 'x'\nparent = 3\n+++\n",
        ]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(FormatError) as caught:
                card_file.parse_card(text, PATH)
            self.assertIn(str(PATH), str(caught.exception))

    def test_title_must_be_one_line(self):
        card = card_file.new_card("Title", created=CREATED)
        for title in ("", "  ", "a\nb"):
            with self.subTest(title=title), self.assertRaises(FormatError):
                card.title = title


class BoardFileTest(unittest.TestCase):
    def test_parse(self):
        board = card_file.parse_board(BOARD, BOARD_PATH)
        self.assertEqual(board.repo, "github.com/acme/api")
        self.assertEqual(board.merge, "pr")
        self.assertEqual([c.name for c in board.columns], ["Todo", "Done"])
        self.assertEqual(board.columns[0].limit, 5)
        self.assertEqual(board.columns[0].cards, ["tw4d", "tvnn"])
        self.assertFalse(board.columns[0].done)
        self.assertTrue(board.columns[1].done)
        self.assertEqual(board.tables("linear-projects")[0]["labels"], [{"id": "l1", "name": "Bug"}])
        self.assertEqual(board.tables("linear-issues"), [])

    def test_round_trip(self):
        board = card_file.parse_board(BOARD, BOARD_PATH)
        self.assertEqual(card_file.dump_board(board), BOARD)

    def test_move_rewrites_lists(self):
        board = card_file.parse_board(BOARD, BOARD_PATH)
        board.columns[0].cards.remove("tvnn")
        board.columns[1].cards.append("tvnn")
        text = card_file.dump_board(board)
        self.assertIn('cards = [\n    "tvnn",\n]', text)
        self.assertEqual(card_file.parse_board(text, BOARD_PATH).columns[1].cards, ["tvnn"])

    def test_new_column_and_settings(self):
        board = card_file.parse_board(BOARD, BOARD_PATH)
        board.columns.insert(0, card_file.ColumnFile("Triage"))
        board.merge = None
        board.repo = None
        board.set_tables("linear-projects", [])
        board.set_tables("linear-issues", [{"id": "i1", "identifier": "ENG-1"}])
        parsed = card_file.parse_board(card_file.dump_board(board), BOARD_PATH)
        self.assertEqual([c.name for c in parsed.columns], ["Triage", "Todo", "Done"])
        self.assertIsNone(parsed.merge)
        self.assertIsNone(parsed.repo)
        self.assertEqual(parsed.tables("linear-projects"), [])
        self.assertEqual(parsed.tables("linear-issues"), [{"id": "i1", "identifier": "ENG-1"}])

    def test_new_board(self):
        board = card_file.new_board(["Todo", "Doing", "Done"], repo="github.com/a/b", merge="local")
        text = card_file.dump_board(board)
        parsed = card_file.parse_board(text, BOARD_PATH)
        self.assertEqual(parsed.repo, "github.com/a/b")
        self.assertEqual(parsed.merge, "local")
        self.assertEqual([c.done for c in parsed.columns], [False, False, True])
        self.assertTrue(text.startswith("format = 1\n"))

    def test_new_board_without_done(self):
        board = card_file.new_board(["A", "B"])
        self.assertEqual([c.done for c in board.columns], [False, False])

    def test_lookup(self):
        board = card_file.parse_board(BOARD, BOARD_PATH)
        self.assertEqual(board.column("todo").name, "Todo")
        self.assertIsNone(board.column("nope"))
        self.assertEqual(board.column_of("tvnn").name, "Todo")
        self.assertIsNone(board.column_of("tzzz"))
        self.assertEqual(board.card_ids(), ["tw4d", "tvnn"])

    def test_errors_name_the_path(self):
        bad = [
            "format = 2\n",
            "column = 3\nformat = 1\n",
            "[[column]]\nname = 'A'\n",
            "format = 1\n[[column]]\ncards = []\n",
            "format = 1\n[[column]]\nname = 'A'\ncards = [1]\n",
            "format = 1\n[[column]]\nname = 'A'\nlimit = 'x'\n",
            "format = 1\n[[column]]\nname = 'A'\ndone = 'yes'\n",
            "format = 1\nrepo = 3\n",
            "format = 1\nlinear-issues = 3\n",
            "format = \n",
        ]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(FormatError) as caught:
                card_file.parse_board(text, BOARD_PATH)
            self.assertIn(str(BOARD_PATH), str(caught.exception))


if __name__ == "__main__":
    unittest.main()
