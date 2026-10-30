"""The board with the mouse: click, double-click, the card menu, drag between columns, and detail clicks."""

import dataclasses
import unittest

from herdboard.board import card_actions
from support.herdboard_mouse import MouseTestCase


class CardActionTests(MouseTestCase):
    def actions(self, card, **changes):
        return [action for _, action in card_actions(dataclasses.replace(card, **changes))]

    def test_a_todo_without_a_worktree(self):
        self.assertEqual(
            self.actions(self.first),
            [
                "detail",
                "done",
                "edit",
                "move_board",
                "delete",
                "archive",
                "promote",
                "start",
                "choose_agent",
                "plan",
                "parent",
            ],
        )

    def test_a_todo_with_a_worktree(self):
        self.assertEqual(
            self.actions(self.first, worktree=True),
            ["detail", "done", "edit", "move_board", "delete", "start", "go", "review", "parent", "merge", "finish"],
        )

    def test_a_linear_card(self):
        self.assertEqual(
            self.actions(self.first, source="linear", ref="ENG-1"), ["detail", "done", "start", "choose_agent", "plan"]
        )
        self.assertEqual(
            self.actions(self.first, source="linear", ref="ENG-1", worktree=True),
            ["detail", "done", "start", "go", "review", "merge", "finish"],
        )

    def test_an_archived_todo(self):
        self.assertEqual(
            card_actions(dataclasses.replace(self.first, archived=True, checked=True)),
            [("enter  Detail", "detail"), ("x  Delete", "delete"), ("A  Unarchive", "archive")],
        )

    def test_a_done_card_whose_worktree_is_left(self):
        actions = self.actions(self.first, checked=True, worktree_left=True)
        self.assertNotIn("done", actions)
        self.assertIn("finish", actions)
        self.assertEqual(
            self.actions(self.first, source="linear", ref="ENG-1", checked=True, worktree=True, worktree_left=True),
            ["detail", "start", "go", "review", "merge", "finish"],
        )

    def test_labels_show_the_key(self):
        self.assertEqual(card_actions(self.first)[:2], [("enter  Detail", "detail"), ("d  Done", "done")])


if __name__ == "__main__":
    unittest.main()
