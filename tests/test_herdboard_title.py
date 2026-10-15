import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard.model import (  # noqa: E402
    HerdboardError, card_type, commit_type, join_title, parse_tags, plain_tags, split_title, with_type,
)


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


class CardTypeTests(unittest.TestCase):
    def test_type_from_a_type_tag(self):
        self.assertEqual(card_type(["auth", "type/bug"]), "bug")

    def test_no_type(self):
        self.assertIsNone(card_type(["auth"]))

    def test_the_first_type_tag_wins(self):
        self.assertEqual(card_type(["type/bug", "type/chore"]), "bug")

    def test_plain_tags_leave_out_type_tags(self):
        self.assertEqual(plain_tags(["auth", "type/bug", "web"]), ["auth", "web"])


class CommitTypeTests(unittest.TestCase):
    def test_bug_and_feature_map_to_fix_and_feat(self):
        self.assertEqual((commit_type("bug"), commit_type("feature")), ("fix", "feat"))

    def test_other_types_are_their_own_commit_type(self):
        self.assertEqual((commit_type("chore"), commit_type("docs"), commit_type("Spike")),
                         ("chore", "docs", "spike"))


class WithTypeTests(unittest.TestCase):
    def test_adds_the_type_before_the_tags(self):
        self.assertEqual(with_type("Fix login #auth 📅 2026-11-01", "bug"),
                         "Fix login #type/bug #auth 📅 2026-11-01")

    def test_adds_the_type_at_the_end(self):
        self.assertEqual(with_type("Fix login", "bug"), "Fix login #type/bug")

    def test_adds_the_type_before_a_parent_link(self):
        self.assertEqual(with_type("Fix login [[#^t5aa]]", "bug"), "Fix login #type/bug [[#^t5aa]]")

    def test_replaces_the_type(self):
        self.assertEqual(with_type("Fix login #type/feature #auth", "bug"), "Fix login #type/bug #auth")

    def test_removes_the_type(self):
        self.assertEqual(with_type("Fix login #type/bug #auth", None), "Fix login #auth")


class ParseTagsTests(unittest.TestCase):
    def test_spaces_commas_and_hash_marks(self):
        self.assertEqual(parse_tags("#auth, web  ops,#auth"), ["auth", "web", "ops"])

    def test_empty(self):
        self.assertEqual(parse_tags("  "), [])


if __name__ == "__main__":
    unittest.main()
