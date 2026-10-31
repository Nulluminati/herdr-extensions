import unittest

from herdboard.model import commit_type, parse_tags


class CommitTypeTests(unittest.TestCase):
    def test_bug_and_feature_map_to_fix_and_feat(self):
        self.assertEqual((commit_type("bug"), commit_type("Feature")), ("fix", "feat"))

    def test_other_types_are_their_own_commit_type(self):
        self.assertEqual((commit_type("chore"), commit_type("docs"), commit_type("Spike")), ("chore", "docs", "spike"))


class ParseTagsTests(unittest.TestCase):
    def test_spaces_commas_and_hash_marks(self):
        self.assertEqual(parse_tags("#api, ui  api,#db"), ["api", "ui", "db"])

    def test_empty(self):
        self.assertEqual(parse_tags("  "), [])


if __name__ == "__main__":
    unittest.main()
