import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard.config import Config  # noqa: E402
from herdboard.identity import RepoIdentity  # noqa: E402
from herdboard.linear_links import ProjectLink, parse_links, replace_links  # noqa: E402
from herdboard.linear_pins import IssuePin, parse_pins, replace_pins  # noqa: E402
from herdboard.model import FormatError  # noqa: E402
from herdboard.store import Store  # noqa: E402

BODY = "## Backlog\n\n- [ ] keep\tme  \n\n\n## Done\n\n**Complete**\n\n\n\n%% kanban:settings\n```\n{}\n```\n%%"
PINS = [IssuePin("u1", "ENG-1"), IssuePin("u2", "OPS-7")]
BLOCK = ["linear-issues:", "- id: u1", "  identifier: ENG-1", "- id: u2", "  identifier: OPS-7"]


def board_text(front, newline="\n"):
    return newline.join(["---", "", *front, "", "---", ""]) + BODY.replace("\n", newline)


def frontmatter(text):
    lines = text.split("\n")
    return lines[: lines.index("---", 1) + 1]


class ParsePinTests(unittest.TestCase):
    def test_reads_pins(self):
        lines = ["---", "kanban-plugin: board", "linear-issues:", "  - id: u1", "    identifier: ENG-1",
                 "  - {id: u2, identifier: OPS-7}", "---"]
        self.assertEqual(parse_pins(lines), PINS)

    def test_no_key_gives_no_pins(self):
        self.assertEqual(parse_pins(["---", "kanban-plugin: board", "---"]), [])
        self.assertEqual(parse_pins([]), [])

    def test_invalid_shapes_are_format_errors(self):
        bad = [
            ["---", "linear-issues: nope", "---"],
            ["---", "linear-issues:", "  - identifier: ENG-1", "---"],
            ["---", "linear-issues:", "  - {id: u1}", "---"],
            ["---", "linear-issues:", "  - just text", "---"],
        ]
        for lines in bad:
            with self.subTest(lines=lines), self.assertRaisesRegex(FormatError, "linear-issues"):
                parse_pins(lines)


class ReplacePinTests(unittest.TestCase):
    def test_adds_the_block_and_keeps_other_bytes(self):
        front = ["# a comment", "repo: github.com/acme/api", "kanban-plugin: board"]
        new = replace_pins(board_text(front), PINS)
        self.assertEqual(new, board_text(front + BLOCK))
        self.assertEqual(parse_pins(frontmatter(new)), PINS)

    def test_keeps_the_project_links_block(self):
        front = ["kanban-plugin: board", "linear-projects:", "- id: p1", "  name: Platform", "after: 1"]
        new = replace_pins(board_text(front), PINS)
        self.assertEqual(new, board_text(front + BLOCK))
        self.assertEqual(parse_links(frontmatter(new)), [ProjectLink("p1", "Platform")])
        removed = replace_pins(new, [])
        self.assertEqual(removed, board_text(front))

    def test_links_keep_the_pins_block(self):
        front = ["kanban-plugin: board", *BLOCK, "after: 1"]
        new = replace_links(board_text(front), [ProjectLink("p1", "Platform")])
        self.assertEqual(new, board_text(front + ["linear-projects:", "- id: p1", "  name: Platform"]))
        self.assertEqual(parse_pins(frontmatter(new)), PINS)

    def test_replaces_only_its_own_block(self):
        front = ["kanban-plugin: board", "linear-issues:", "- {id: x, identifier: X-1}", "after: 1"]
        new = replace_pins(board_text(front), PINS[:1])
        self.assertEqual(new, board_text(["kanban-plugin: board", *BLOCK[:3], "after: 1"]))

    def test_crlf_is_kept(self):
        new = replace_pins(board_text(["kanban-plugin: board"], "\r\n"), PINS)
        self.assertNotRegex(new, r"(?<!\r)\n")
        self.assertEqual(new, board_text(["kanban-plugin: board", *BLOCK], "\r\n"))

    def test_duplicate_keys_are_a_format_error(self):
        front = ["kanban-plugin: board", "linear-issues: []", "linear-issues: []"]
        with self.assertRaisesRegex(FormatError, "duplicate"):
            replace_pins(board_text(front), PINS)


class StorePinTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Config(todos_dir=Path(self.tmp.name) / "todos"))
        identity = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))
        self.path = self.store.init_board(identity)

    def test_add_and_remove_round_trip(self):
        before = self.path.read_text()
        self.assertEqual(self.store.update_pins(self.path, lambda pins: pins + PINS), PINS)
        self.assertEqual(self.store.board_meta(self.path).pins, PINS)
        self.store.update_pins(self.path, lambda pins: [])
        self.assertEqual(self.path.read_text(), before)

    def test_update_config_writes_links_and_pins_together(self):
        self.store.update_pins(self.path, lambda pins: PINS)
        self.store.update_config(self.path, change_links=lambda links: [ProjectLink("p1", "Platform")],
                                 change_pins=lambda pins: pins[1:])
        meta = self.store.board_meta(self.path)
        self.assertEqual(meta.links, [ProjectLink("p1", "Platform")])
        self.assertEqual(meta.pins, PINS[1:])

    def test_bad_pins_make_the_board_read_only(self):
        text = self.path.read_text().replace("repo:", "linear-issues: nope\nrepo:")
        self.path.write_text(text)
        meta = self.store.board_meta(self.path)
        self.assertEqual(meta.pins, [])
        self.assertIn("linear-issues", meta.error)
        with self.assertRaisesRegex(FormatError, "read only"):
            self.store.add(self.path, "card")
        self.assertEqual(self.path.read_text(), text)


if __name__ == "__main__":
    unittest.main()
