import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard.model import HerdboardError, join_title, parse_tags, split_title  # noqa: E402


class SplitTitleTests(unittest.TestCase):
    def test_text_tags_and_due_date(self):
        self.assertEqual(
            split_title("Fix the login #auth timeout #web 📅 2026-11-01"),
            ("Fix the login timeout", ["auth", "web"], "2026-11-01"),
        )

    def test_plain_text(self):
        self.assertEqual(split_title("Buy milk"), ("Buy milk", [], None))

    def test_a_hash_inside_a_word_is_text(self):
        self.assertEqual(split_title("Fix C# build #dotnet"), ("Fix C# build", ["dotnet"], None))


class JoinTitleTests(unittest.TestCase):
    def test_text_then_tags_then_date(self):
        self.assertEqual(
            join_title("Fix the login timeout", ["auth", "web"], "2026-11-01"),
            "Fix the login timeout #auth #web 📅 2026-11-01",
        )

    def test_round_trip(self):
        line = "Fix the login timeout #auth 📅 2026-11-01"
        self.assertEqual(join_title(*split_title(line)), line)

    def test_without_tags_or_date(self):
        self.assertEqual(join_title("Buy milk", [], None), "Buy milk")

    def test_rejects_a_bad_date(self):
        for due in ("2026-13-01", "tomorrow", "2026-1-1"):
            with self.subTest(due=due), self.assertRaises(HerdboardError):
                join_title("x", [], due)

    def test_rejects_empty_text(self):
        with self.assertRaises(HerdboardError):
            join_title("  ", ["auth"], None)


class ParseTagsTests(unittest.TestCase):
    def test_spaces_commas_and_hash_marks(self):
        self.assertEqual(parse_tags("#auth, web  ops,#auth"), ["auth", "web", "ops"])

    def test_empty(self):
        self.assertEqual(parse_tags("  "), [])


if __name__ == "__main__":
    unittest.main()
