import unittest


from herdboard.board import card_matches
from herdboard.store import CardView


class TypeFilterTests(unittest.TestCase):
    def issue(self):
        return CardView(ref="ENG-2", board="Platform", path=None, column="Todo", title="Fix login",
                        checked=False, due=None, tags=["auth"], body=[], source="linear", type="bug")

    def test_a_plain_word_matches_a_linear_card_type(self):
        self.assertTrue(card_matches(self.issue(), ["bug"]))

    def test_hash_type_matches_a_linear_card_type(self):
        self.assertTrue(card_matches(self.issue(), ["#type/bug"]))
        self.assertFalse(card_matches(self.issue(), ["#type/feature"]))


if __name__ == "__main__":
    unittest.main()
