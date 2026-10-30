import unittest

from herdboard.board import HELP_KEYS, card_actions, promote_refusal
from herdboard.store import CardView


def todo(**fields):
    return CardView(
        **{
            "ref": "t1",
            "board": "api",
            "path": None,
            "column": "Todo",
            "title": "Task",
            "checked": False,
            "due": None,
            "tags": [],
            "body": [],
            **fields,
        }
    )


class MenuRuleTests(unittest.TestCase):
    def actions(self, card):
        return [action for _label, action in card_actions(card)]

    def test_a_top_level_todo_offers_promotion(self):
        self.assertIn("promote", self.actions(todo()))
        self.assertIn("T  Promote to Linear", [label for label, _ in card_actions(todo())])

    def test_a_checked_todo_offers_promotion(self):
        self.assertIn("promote", self.actions(todo(checked=True)))

    def test_cards_that_cannot_be_promoted_do_not_offer_it(self):
        cards = {
            "linear": todo(source="linear", ref="ENG-1", read_only=True),
            "read only": todo(read_only=True),
            "archived": todo(archived=True),
            "sub-card": todo(parent="t9"),
            "promoted": todo(title="Task → ENG-7"),
            "worktree": todo(worktree=True),
        }
        for name, card in cards.items():
            with self.subTest(card=name):
                self.assertNotIn("promote", self.actions(card))
                self.assertIsNotNone(promote_refusal(card))
        self.assertIsNone(promote_refusal(todo()))

    def test_help_lists_T(self):
        self.assertIn("T", [key for key, _what in HELP_KEYS])


if __name__ == "__main__":
    unittest.main()
