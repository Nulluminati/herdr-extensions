import tempfile
import unittest
from pathlib import Path


from herdboard.config import Config
from herdboard.identity import RepoIdentity
from herdboard.linear_links import LabelLink, ProjectLink, parse_links, replace_links
from herdboard.model import FormatError
from herdboard.store import Store

BODY = "## Backlog\n\n- [ ] keep\tme  \n\n\n## Done\n\n**Complete**\n\n\n\n%% kanban:settings\n```\n{}\n```\n%%"
PLUGIN = {"id": "p1", "name": "Platform"}


def board_text(front, newline="\n"):
    return newline.join(["---", "", *front, "", "---", ""]) + BODY.replace("\n", newline)


class ParseTests(unittest.TestCase):
    def test_reads_projects_and_labels(self):
        lines = ["---", "kanban-plugin: board", "repo: github.com/acme/api", "linear-projects:",
                 "  - id: p1", "    name: Platform", "    labels:",
                 "      - {id: l1, name: 'repo:api'}", "  - id: p2", "    name: Mobile", "---"]
        self.assertEqual(parse_links(lines), [
            ProjectLink("p1", "Platform", [LabelLink("l1", "repo:api")]),
            ProjectLink("p2", "Mobile"),
        ])

    def test_no_key_no_frontmatter_and_unclosed_give_no_links(self):
        self.assertEqual(parse_links(["---", "kanban-plugin: board", "---"]), [])
        self.assertEqual(parse_links([]), [])
        self.assertEqual(parse_links(["---", "---"]), [])

    def test_invalid_yaml_and_shapes_are_format_errors(self):
        bad = [
            ["---", "a: [unclosed", "---"],
            ["---", "- just\n- a list", "---"],
            ["---", "linear-projects: nope", "---"],
            ["---", "linear-projects:", "  - name: no id", "---"],
            ["---", "linear-projects:", "  - {id: p1, name: x, labels: 3}", "---"],
            ["---", "linear-projects:", "  - {id: p1, name: x, labels: [{id: l1}]}", "---"],
        ]
        for lines in bad:
            with self.subTest(lines=lines), self.assertRaises(FormatError):
                parse_links(lines)


class ReplaceTests(unittest.TestCase):
    LINKS = [ProjectLink("p1", "Platform", [LabelLink("l1", "repo:api")])]
    BLOCK = ["linear-projects:", "- id: p1", "  name: Platform", "  labels:", "  - id: l1",
             "    name: repo:api"]

    def test_adds_the_block_before_the_trailing_blank_line_and_keeps_other_bytes(self):
        front = ["repo: github.com/acme/api", "kanban-plugin: board", "owner: nate"]
        text = board_text(front)
        new = replace_links(text, self.LINKS)
        self.assertEqual(new, board_text(front + self.BLOCK))
        self.assertTrue(new.endswith(BODY))
        self.assertEqual(parse_links(new.split("\n")[: new.split("\n").index("---", 1) + 1]), self.LINKS)

    def test_adds_before_the_closing_fence_without_a_blank_line(self):
        text = "---\nkanban-plugin: board\n---\n## Backlog\n"
        self.assertEqual(replace_links(text, [ProjectLink("p1", "Platform")]),
                         "---\nkanban-plugin: board\nlinear-projects:\n- id: p1\n  name: Platform\n"
                         "---\n## Backlog\n")

    def test_other_lines_keep_order_comments_and_spelling(self):
        front = ["# a comment", "owner:   'nate'   # who", "kanban-plugin: board", "",
                 "tags: [a,   b]", "# trailing comment"]
        new = replace_links(board_text(front), self.LINKS)
        self.assertEqual(new, board_text(front + self.BLOCK))

    def test_replaces_only_its_own_block(self):
        front = ["kanban-plugin: board", "linear-projects:", "- {id: x, name: X}",
                 "  # inside", "", "- id: y", "  name: Y", "after: 1", "# comment", "last: 2"]
        new = replace_links(board_text(front), [ProjectLink("p1", "Platform")])
        self.assertEqual(new, board_text(["kanban-plugin: board", "linear-projects:", "- id: p1",
                                          "  name: Platform", "after: 1", "# comment", "last: 2"]))

    def test_block_at_the_end_keeps_the_blank_line_after_it(self):
        front = ["kanban-plugin: board", "linear-projects:", "  - id: x", "    name: X"]
        new = replace_links(board_text(front), [ProjectLink("p1", "Platform")])
        self.assertEqual(new, board_text(["kanban-plugin: board", "linear-projects:", "- id: p1",
                                          "  name: Platform"]))

    def test_removes_the_block_and_only_the_block(self):
        front = ["owner: nate", "kanban-plugin: board", "linear-projects:", "- id: x", "  name: X",
                 "after: 1"]
        removed = replace_links(board_text(front), [])
        self.assertEqual(removed, board_text(["owner: nate", "kanban-plugin: board", "after: 1"]))
        self.assertEqual(replace_links(removed, []), removed)

    def test_crlf_is_kept(self):
        text = board_text(["kanban-plugin: board", "owner: nate"], "\r\n")
        new = replace_links(text, [ProjectLink("p1", "Platform")])
        self.assertNotRegex(new, r"(?<!\r)\n")
        self.assertEqual(new, board_text(["kanban-plugin: board", "owner: nate", "linear-projects:",
                                          "- id: p1", "  name: Platform"], "\r\n"))
        self.assertTrue(new.endswith(BODY.replace("\n", "\r\n")))

    def test_unicode_is_written_as_is(self):
        new = replace_links(board_text(["kanban-plugin: board"]), [ProjectLink("p1", "Plattform Ü")])
        self.assertIn("  name: Plattform Ü\n", new)

    def test_invalid_yaml_is_not_replaced(self):
        with self.assertRaises(FormatError):
            replace_links(board_text(["a: [unclosed"]), [])

    def test_duplicate_top_level_keys_are_a_format_error(self):
        for front in (["owner: a", "kanban-plugin: board", "owner: b"],
                      ["kanban-plugin: board", "linear-projects:", "- id: x", "  name: X",
                       "linear-projects: []"]):
            with self.subTest(front=front), self.assertRaisesRegex(FormatError, "duplicate"):
                replace_links(board_text(front), self.LINKS)

    def test_indented_lookalike_keys_are_not_duplicates(self):
        front = ["kanban-plugin: board", "owner: a", "nested:", "  owner: b"]
        self.assertIn("linear-projects:", replace_links(board_text(front), self.LINKS))


class StoreLinkTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Config(todos_dir=Path(self.tmp.name) / "todos"))
        identity = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))
        self.path = self.store.init_board(identity)

    def test_add_replace_remove_round_trip(self):
        before = self.path.read_text()
        body = before[before.index("## Backlog"):]
        self.store.update_links(self.path, lambda links: links + [ProjectLink("p1", "Platform")])
        self.store.update_links(self.path, lambda links: [ProjectLink("p1", "Renamed", [LabelLink("l1", "x")])])
        meta = self.store.board_meta(self.path)
        self.assertEqual(meta.repo, "github.com/acme/api")
        self.assertEqual(meta.links, [ProjectLink("p1", "Renamed", [LabelLink("l1", "x")])])
        self.store.update_links(self.path, lambda links: [])
        self.assertEqual(self.path.read_text(), before)
        self.assertTrue(self.path.read_text().endswith(body))

    def test_invalid_yaml_board_is_read_only(self):
        text = self.path.read_text().replace("repo:", "bad: [unclosed\nrepo:")
        self.path.write_text(text)
        meta = self.store.board_meta(self.path)
        self.assertEqual(meta.links, [])
        self.assertIn("invalid YAML", meta.error)
        with self.assertRaisesRegex(FormatError, "read only"):
            self.store.update_links(self.path, lambda links: links)
        with self.assertRaisesRegex(FormatError, "read only"):
            self.store.add(self.path, "card")
        self.assertEqual(self.path.read_text(), text)


if __name__ == "__main__":
    unittest.main()
