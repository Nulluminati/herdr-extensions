"""The card details pane: its data and its lines, without a terminal."""
import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import activity, details  # noqa: E402
from herdboard.config import Config  # noqa: E402
from herdboard.store import CardView, Store  # noqa: E402


def card(**fields):
    values = dict(ref="t123", board="acme-api", path=None, column="In progress",
                  title="Fix login timeout #bug #api", checked=False, due=None, tags=["api"],
                  body=[], type="feature")
    values.update(fields)
    return CardView(**values)


RECORD = {"ref": "t123", "agent": "claude", "model": "opus", "thinking": "high",
          "branch": "t123-fix-login-timeout", "base": "main",
          "pr_url": "https://github.com/acme/api/pull/12", "pr_state": "open"}


def log(count):
    return [{"time": f"2026-10-{index + 1:02d} 14:0{index % 10}", "author": "claude" if index % 2 else "herdboard",
             "message": f"Entry {index + 1}\nsecond line"} for index in range(count)]


def plain(lines):
    return [getattr(line, "plain", line) for line in lines]


class DetailsLinesTests(unittest.TestCase):
    def test_a_started_card_with_everything(self):
        data = {"ref": "t123", "card": card(body=["\t- [x] one", "\t- [ ] two", "\t- [x] three", "\tnotes"]),
                "record": RECORD, "attachments": [{"name": "plan.md"}, {"name": "mockup-pane.png"}],
                "log": log(8)}
        lines = plain(details.details_lines(data, 80, 20))
        self.assertEqual(lines[0], "t123 │ Fix login timeout  In progress " + " " * 35 + "feature")
        self.assertEqual(len(lines[0]), 80)
        self.assertEqual(lines[1], "claude opus (high) · t123-fix-login-timeout → main · PR #12 open")
        self.assertEqual(lines[2], "☑ 2/3  plan.md  mockup-pane.png")
        self.assertEqual(lines[3], "── log " + "─" * 73)
        self.assertEqual(lines[4], "10-01 14:00  herdboard  Entry 1")
        self.assertEqual(lines[5], "10-02 14:01  claude     Entry 2")
        self.assertEqual(len(lines), 12)

    def test_a_small_height_keeps_the_newest_entries(self):
        data = {"ref": "t123", "card": card(), "record": RECORD, "attachments": [], "log": log(8)}
        lines = plain(details.details_lines(data, 80, 6))
        self.assertEqual(len(lines), 6)
        # No checklist and no attachments: the third line is left out.
        self.assertTrue(lines[2].startswith("── log"))
        self.assertEqual([line.split()[-2] + " " + line.split()[-1] for line in lines[3:]], ["Entry 6", "Entry 7", "Entry 8"])

    def test_a_repoless_record_has_no_branch(self):
        record = {"ref": "t123", "agent": "pi", "model": None, "thinking": None, "mode": "repoless"}
        data = {"ref": "t123", "card": card(), "record": record, "attachments": [], "log": []}
        lines = plain(details.details_lines(data, 80, 10))
        self.assertEqual(lines[1], "pi · no repository")
        self.assertEqual(lines[3], "no log entries")

    def test_a_card_without_a_record(self):
        data = {"ref": "t123", "card": card(), "record": None, "attachments": [{"name": "plan.md"}], "log": []}
        lines = plain(details.details_lines(data, 80, 10))
        self.assertEqual(lines[1], "no start record for t123")
        self.assertEqual(lines[2], "plan.md")

    def test_a_missing_card_is_one_dim_line(self):
        data = {"ref": "t123", "card": None, "record": None, "attachments": [], "log": []}
        lines = details.details_lines(data, 80, 10)
        self.assertEqual(plain(lines), ["no card t123"])
        self.assertEqual(str(lines[0].style), "dim")

    def test_every_line_is_cut_to_the_width(self):
        long = card(title="A very long title that does not fit in a narrow details pane at all")
        entries = [{"time": "2026-10-29 14:02", "author": "herdboard", "message": "x" * 100}]
        data = {"ref": "t123", "card": long, "record": RECORD, "attachments": [{"name": "plan.md"}],
                "log": entries}
        lines = plain(details.details_lines(data, 40, 10))
        self.assertTrue(all(len(line) <= 40 for line in lines), lines)
        self.assertTrue(lines[0].endswith("feature"))
        self.assertIn("…", lines[0])
        self.assertTrue(lines[1].endswith("…"))
        self.assertTrue(lines[-1].endswith("…"))

    def test_a_linear_card_shows_its_state_as_the_column(self):
        issue = card(ref="ENG-12", title="Speed up search", source="linear", state="In Review", column="Review",
                     type=None)
        data = {"ref": "ENG-12", "card": issue, "record": None, "attachments": [], "log": []}
        first = plain(details.details_lines(data, 60, 5))[0]
        self.assertEqual(first, "ENG-12 │ Speed up search  In Review ")


class DetailsDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.config = Config(todos_dir=self.root / "todos")
        self.store = Store(self.config)
        self.card = self.store.add(self.store.ensure_inbox(), "Fix login timeout #type/feature")

    def write_records(self, records):
        path = self.root / "state" / "herdboard" / "starts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records))

    def test_collects_the_card_record_attachments_and_log(self):
        ref = self.card.ref
        self.write_records({ref: {**RECORD, "ref": ref}})
        activity.append(self.store, ref, "Started.", "claude", now=datetime(2026, 10, 29, 13, 28))
        folder = self.config.todos_dir / ".herdboard" / "attachments" / ref
        folder.mkdir(parents=True)
        (folder / "plan.md").write_text("# Plan\n")
        data = details.details_data(self.store, self.config, ref)
        self.assertEqual(data["card"].ref, ref)
        self.assertEqual(data["record"]["branch"], RECORD["branch"])
        self.assertEqual([entry["name"] for entry in data["attachments"]], ["plan.md"])
        self.assertEqual([entry["message"] for entry in data["log"]], ["Started."])

    def test_an_unknown_card_has_no_card(self):
        data = details.details_data(self.store, self.config, "t999")
        self.assertIsNone(data["card"])

    def test_a_linear_card_comes_from_the_lookup(self):
        issue = card(ref="ENG-12", source="linear")
        data = details.details_data(self.store, self.config, "ENG-12", linear_card=lambda ref: issue)
        self.assertIs(data["card"], issue)

    def test_watched_paths(self):
        ref = self.card.ref
        paths = details.watched_paths(self.config, ref, self.card.path)
        self.assertEqual(paths, [self.card.path, activity.log_path(self.config, ref),
                                 self.config.todos_dir / ".herdboard" / "attachments" / ref,
                                 self.root / "state" / "herdboard" / "starts.json"])

    def test_signature_changes_when_a_watched_file_changes(self):
        ref = self.card.ref
        paths = details.watched_paths(self.config, ref, self.card.path)
        before = details.signature(paths)
        activity.append(self.store, ref, "Started.", "claude")
        self.assertNotEqual(details.signature(paths), before)


if __name__ == "__main__":
    unittest.main()
