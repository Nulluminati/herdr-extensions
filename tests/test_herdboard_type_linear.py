import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard.config import Config  # noqa: E402
from herdboard.linear_cards import issue_card  # noqa: E402


def issue(*labels):
    return {
        "identifier": "ENG-12", "title": "Fix login", "state": {"name": "Todo", "type": "unstarted"},
        "labels": [{"name": name} for name in labels], "team": {"key": "ENG"},
    }


class LinearTypeTests(unittest.TestCase):
    def card(self, *labels, config=None):
        return issue_card(config or Config(), issue(*labels), set(), datetime.now(timezone.utc), False)

    def test_a_label_that_names_a_type_is_the_type(self):
        card = self.card("auth", "Bug")
        self.assertEqual((card.type, card.tags), ("bug", ["auth"]))

    def test_the_first_type_label_wins_and_the_others_stay_labels(self):
        card = self.card("Feature", "bug")
        self.assertEqual((card.type, card.tags), ("feature", ["bug"]))

    def test_no_type_label(self):
        card = self.card("auth", "Improvement")
        self.assertEqual((card.type, card.tags), (None, ["auth", "Improvement"]))

    def test_the_types_come_from_the_config(self):
        card = self.card("Improvement", config=Config(types=["improvement"]))
        self.assertEqual((card.type, card.tags), ("improvement", []))


if __name__ == "__main__":
    unittest.main()
