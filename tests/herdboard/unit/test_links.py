import tempfile
import unittest
from pathlib import Path

from herdboard.config import Config
from herdboard.identity import RepoIdentity
from herdboard.linear_links import LabelLink, ProjectLink, parse_links
from herdboard.model import FormatError
from herdboard.store import Store


class ParseTests(unittest.TestCase):
    def test_reads_projects_and_labels(self):
        raw = [
            {"id": "p1", "name": "Platform", "labels": [{"id": "l1", "name": "repo:api"}]},
            {"id": "p2", "name": "Mobile"},
        ]
        self.assertEqual(
            parse_links(raw),
            [ProjectLink("p1", "Platform", [LabelLink("l1", "repo:api")]), ProjectLink("p2", "Mobile")],
        )

    def test_no_key_gives_no_links(self):
        self.assertEqual(parse_links(None), [])
        self.assertEqual(parse_links([]), [])

    def test_invalid_shapes_are_format_errors(self):
        bad = [
            "nope",
            [{"name": "no id"}],
            [{"id": "p1", "name": "x", "labels": 3}],
            [{"id": "p1", "name": "x", "labels": [{"id": "l1"}]}],
        ]
        for raw in bad:
            with self.subTest(raw=raw), self.assertRaises(FormatError):
                parse_links(raw)

    def test_to_dict_round_trips(self):
        link = ProjectLink("p1", "Platform", [LabelLink("l1", "repo:api")])
        self.assertEqual(parse_links([link.to_dict()]), [link])


class StoreLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Config(todos_dir=Path(self.tmp.name) / "todos"))
        identity = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))
        self.path = self.store.init_board(identity)

    def test_add_replace_remove_round_trip(self):
        board = self.path / "board.toml"
        before = board.read_text()
        self.store.update_links(self.path, lambda links: links + [ProjectLink("p1", "Platform")])
        self.assertIn("[[linear-projects]]", board.read_text())
        self.store.update_links(self.path, lambda links: [ProjectLink("p1", "Renamed", [LabelLink("l1", "x")])])
        meta = self.store.board_meta(self.path)
        self.assertEqual(meta.repo, "github.com/acme/api")
        self.assertEqual(meta.links, [ProjectLink("p1", "Renamed", [LabelLink("l1", "x")])])
        self.store.update_links(self.path, lambda links: [])
        self.assertEqual(board.read_text(), before)

    def test_bad_links_make_the_settings_read_only_and_cards_still_work(self):
        board = self.path / "board.toml"
        text = board.read_text().replace("repo =", "linear-projects = 3\nrepo =", 1)
        board.write_text(text)
        meta = self.store.board_meta(self.path)
        self.assertEqual(meta.links, [])
        self.assertIn("linear-projects", meta.error)
        with self.assertRaisesRegex(FormatError, "read only"):
            self.store.update_links(self.path, lambda links: links)
        self.assertEqual(board.read_text(), text)
        self.store.add(self.path, "card")
        self.assertIn("linear-projects = 3", board.read_text())


if __name__ == "__main__":
    unittest.main()
