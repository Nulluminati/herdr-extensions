import tempfile
import unittest
from pathlib import Path

from herdboard.config import Config
from herdboard.identity import RepoIdentity
from herdboard.linear import LinearError
from herdboard.linear_links import ProjectLink, parse_links, replace_links
from herdboard.linear_pins import IssuePin, add_pin, parse_pins, pin_issue, replace_pins, unpin_issue
from herdboard.model import FormatError, NotFoundError
from herdboard.store import Store

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
        lines = [
            "---",
            "kanban-plugin: board",
            "linear-issues:",
            "  - id: u1",
            "    identifier: ENG-1",
            "  - {id: u2, identifier: OPS-7}",
            "---",
        ]
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


class StoreCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(Config(todos_dir=Path(self.tmp.name) / "todos"))
        identity = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))
        self.path = self.store.init_board(identity)


class StorePinTests(StoreCase):
    def test_add_and_remove_round_trip(self):
        before = self.path.read_text()
        self.assertEqual(self.store.update_pins(self.path, lambda pins: pins + PINS), PINS)
        self.assertEqual(self.store.board_meta(self.path).pins, PINS)
        self.store.update_pins(self.path, lambda pins: [])
        self.assertEqual(self.path.read_text(), before)

    def test_update_config_writes_links_and_pins_together(self):
        self.store.update_pins(self.path, lambda pins: PINS)
        self.store.update_config(
            self.path, change_links=lambda links: [ProjectLink("p1", "Platform")], change_pins=lambda pins: pins[1:]
        )
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


class FakeClient:
    def __init__(self, issues):
        self.issues = issues
        self.asked = []

    def issue(self, ref):
        self.asked.append(ref)
        return next((i for i in self.issues if ref in (i["identifier"], i["id"])), None)


class FakeSource:
    def __init__(self, issues, offline=False):
        self.client = FakeClient(issues)
        self.offline = offline

    def require_active(self):
        pass


class PinCoreTests(StoreCase):
    ISSUES = [{"id": "u1", "identifier": "ENG-1"}, {"id": "u2", "identifier": "OPS-7"}]

    def test_pin_issue_adds_a_pin_once(self):
        source = FakeSource(self.ISSUES)
        self.assertEqual(pin_issue(self.store, source, self.path, "eng-1"), (PINS[0], True))
        self.assertEqual(source.client.asked, ["ENG-1"])
        self.assertEqual(pin_issue(self.store, source, self.path, "OPS-7"), (PINS[1], True))
        self.assertEqual(pin_issue(self.store, source, self.path, "ENG-1"), (PINS[0], False))
        self.assertEqual(self.store.board_meta(self.path).pins, PINS)

    def test_pin_of_an_unknown_issue_is_not_found(self):
        with self.assertRaises(NotFoundError):
            pin_issue(self.store, FakeSource(self.ISSUES), self.path, "ENG-404")
        self.assertEqual(self.store.board_meta(self.path).pins, [])

    def test_pin_needs_a_connection(self):
        with self.assertRaisesRegex(LinearError, "connection"):
            pin_issue(self.store, FakeSource(self.ISSUES, offline=True), self.path, "ENG-1")

    def test_add_pin_updates_the_identifier_of_a_moved_issue(self):
        add_pin(self.store, self.path, {"id": "u1", "identifier": "OLD-1"})
        self.assertEqual(add_pin(self.store, self.path, self.ISSUES[0]), (PINS[0], False))
        self.assertEqual(self.store.board_meta(self.path).pins, [PINS[0]])

    def test_unpin_by_identifier_or_id(self):
        self.store.update_pins(self.path, lambda pins: PINS)
        self.assertEqual(unpin_issue(self.store, self.path, "eng-1"), PINS[0])
        self.assertEqual(unpin_issue(self.store, self.path, "u2"), PINS[1])
        self.assertEqual(self.store.board_meta(self.path).pins, [])
        with self.assertRaises(NotFoundError):
            unpin_issue(self.store, self.path, "ENG-1")


if __name__ == "__main__":
    unittest.main()
