"""The activity log: one Markdown file per card, appended by agents, herdboard and the user."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import activity  # noqa: E402
from herdboard.linear_source import state_dir  # noqa: E402
from herdboard.config import load_config  # noqa: E402
from herdboard.model import HerdboardError  # noqa: E402
from herdboard.store import Store  # noqa: E402

from test_herdboard_cli import CliTestCase  # noqa: E402

NOON = datetime(2026, 10, 12, 12, 0)


class LogFileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        config = root / "config.toml"
        config.write_text(f'todos_dir = "{root / "todos"}"\n')
        self.store = Store(load_config(config))
        self.todos = root / "todos"

    def test_append_writes_one_line_in_a_hidden_folder(self):
        entry = activity.append(self.store, "t3m9", "Ready for review: 3 commits.", "pi", now=NOON)
        self.assertEqual(entry, {"time": "2026-10-12 12:00", "author": "pi",
                                 "message": "Ready for review: 3 commits."})
        path = self.todos / ".herdboard" / "log" / "t3m9.md"
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "- 2026-10-12 12:00 · pi · Ready for review: 3 commits.\n")

    def test_entries_come_back_oldest_first(self):
        activity.append(self.store, "t3m9", "First.", "claude", now=NOON)
        activity.append(self.store, "t3m9", "Second.", "user", now=NOON.replace(minute=5))
        entries = activity.read(self.store.config, "t3m9")
        self.assertEqual([e["message"] for e in entries], ["First.", "Second."])
        self.assertEqual([e["author"] for e in entries], ["claude", "user"])

    def test_limit_keeps_the_newest_entries(self):
        for number in range(12):
            activity.append(self.store, "t3m9", f"Entry {number}.", "pi", now=NOON)
        entries = activity.read(self.store.config, "t3m9", limit=10)
        self.assertEqual(len(entries), 10)
        self.assertEqual(entries[0]["message"], "Entry 2.")
        self.assertEqual(entries[-1]["message"], "Entry 11.")

    def test_several_lines_become_indented_lines_of_one_entry(self):
        activity.append(self.store, "t3m9", "Blocked.\nThe API key is missing.\r\n", "pi", now=NOON)
        path = self.todos / ".herdboard" / "log" / "t3m9.md"
        self.assertEqual(path.read_text(encoding="utf-8"),
                         "- 2026-10-12 12:00 · pi · Blocked.\n\tThe API key is missing.\n")
        self.assertEqual(activity.read(self.store.config, "t3m9")[0]["message"],
                         "Blocked.\nThe API key is missing.")

    def test_a_card_without_a_log_has_no_entries(self):
        self.assertEqual(activity.read(self.store.config, "t3m9"), [])

    def test_linear_identifiers_name_their_own_file(self):
        activity.append(self.store, "ENG-123", "Started.", "herdboard", now=NOON)
        self.assertTrue((self.todos / ".herdboard" / "log" / "ENG-123.md").is_file())

    def test_an_empty_message_is_refused(self):
        with self.assertRaises(HerdboardError):
            activity.append(self.store, "t3m9", " \n ", "pi", now=NOON)

    def test_a_reference_that_is_not_a_card_id_is_refused(self):
        for ref in ("../x", "api:12", ""):
            with self.subTest(ref=ref), self.assertRaises(HerdboardError):
                activity.append(self.store, ref, "Hi.", "pi", now=NOON)

    def test_lines_that_are_not_entries_are_ignored(self):
        path = self.todos / ".herdboard" / "log" / "t3m9.md"
        path.parent.mkdir(parents=True)
        path.write_text("# notes\n- 2026-10-12 12:00 · pi · Kept.\nstray\n", encoding="utf-8")
        self.assertEqual([e["message"] for e in activity.read(self.store.config, "t3m9")], ["Kept."])


class AuthorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name) / "state"
        self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.state)}))

    def test_outside_herdr_the_author_is_the_user(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(activity.author(), "user")

    def test_an_agent_pane_names_the_agent_kind(self):
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}), \
                mock.patch.object(activity, "agent_kind", return_value="pi") as kind:
            self.assertEqual(activity.author(), "pi")
        kind.assert_called_once_with("w1:p2")

    def test_a_pane_without_an_agent_is_the_user(self):
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}), \
                mock.patch.object(activity, "agent_kind", return_value=None):
            self.assertEqual(activity.author(), "user")

    def test_a_pane_herdboard_cannot_reach_falls_back_to_the_started_agent(self):
        # Codex's sandbox denies the Herdr socket, so `herdr agent get` fails from its tools.
        activity.record_pane_agent("w1:p2", "codex")
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2", "XDG_STATE_HOME": str(self.state)}), \
                mock.patch.object(activity, "agent_kind", return_value=None):
            self.assertEqual(activity.author(), "codex")

    def test_a_pane_without_a_started_agent_stays_the_user(self):
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2", "XDG_STATE_HOME": str(self.state)}), \
                mock.patch.object(activity, "agent_kind", return_value=None):
            self.assertEqual(activity.author(), "user")

    def test_a_recorded_pane_that_herdr_reports_wins(self):
        activity.record_pane_agent("w1:p2", "codex")
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2", "XDG_STATE_HOME": str(self.state)}), \
                mock.patch.object(activity, "agent_kind", return_value="pi"):
            self.assertEqual(activity.author(), "pi")

    def test_record_pane_agent_updates_a_pane_and_keeps_other_panes(self):
        activity.record_pane_agent("w1:p2", "claude")
        activity.record_pane_agent("w1:p3", "pi")
        activity.record_pane_agent("w1:p2", "codex")
        panes = json.loads((state_dir() / "panes.json").read_text(encoding="utf-8"))
        self.assertEqual(panes, {"w1:p2": "codex", "w1:p3": "pi"})

    def test_record_pane_agent_ignores_a_pane_or_kind_that_is_not_a_name(self):
        for pane, kind in (("", "codex"), ("w1:p2", "Not A Kind")):
            with self.subTest(pane=pane, kind=kind):
                activity.record_pane_agent(pane, kind)
                self.assertFalse((state_dir() / "panes.json").exists())

    def test_record_pane_agent_keeps_only_the_newest_panes(self):
        for number in range(300):
            activity.record_pane_agent(f"w{number}:p1", "pi")
        panes = json.loads((state_dir() / "panes.json").read_text(encoding="utf-8"))
        self.assertEqual(len(panes), 256)
        self.assertNotIn("w0:p1", panes)
        self.assertIn("w299:p1", panes)

    def test_agent_kind_reads_herdr(self):
        reply = {"id": "x", "result": {"type": "agent_info", "agent": {"agent": "claude", "pane_id": "w1:p2"}}}
        done = subprocess.CompletedProcess([], 0, json.dumps(reply), "")
        with mock.patch.object(activity, "_run", return_value=done) as run:
            self.assertEqual(activity.agent_kind("w1:p2"), "claude")
        self.assertEqual(run.call_args.args[0][1:], ["agent", "get", "w1:p2"])

    def test_agent_kind_is_none_when_herdr_fails_or_finds_no_agent(self):
        failures = [
            subprocess.CompletedProcess([], 1, '{"error": {"code": "agent_not_found"}}', ""),
            subprocess.CompletedProcess([], 0, "not json", ""),
            OSError("no herdr"),
        ]
        for failure in failures:
            with self.subTest(failure=failure):
                effect = {"side_effect": failure} if isinstance(failure, Exception) else {"return_value": failure}
                with mock.patch.object(activity, "_run", **effect):
                    self.assertIsNone(activity.agent_kind("w1:p2"))


class LogCliTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(activity, "author", return_value="pi"))

    def add(self, text="Fix the login timeout"):
        return self.run_json("add", text, directory=self.plain)["card"]["ref"]

    def test_log_appends_and_returns_the_entry(self):
        ref = self.add()
        data = self.run_json("log", ref, "Ready for review.", directory=self.plain)
        self.assertEqual(data["ref"], ref)
        self.assertEqual(data["entry"]["author"], "pi")
        self.assertEqual(data["entry"]["message"], "Ready for review.")

    def test_log_text_output(self):
        ref = self.add()
        out, _ = self.run_cli("log", ref, "Ready for review.", directory=self.plain)
        self.assertEqual(out, f"{ref}: logged\n")

    def test_log_of_an_unknown_todo_fails(self):
        data = self.run_json("log", "tzzz", "Hi.", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "not_found")

    def test_log_of_a_temporary_reference_gives_the_card_an_id(self):
        (self.todos).mkdir(parents=True, exist_ok=True)
        (self.todos / "inbox.md").write_text("---\nkanban-plugin: board\n---\n\n## Todo\n\n- [ ] No ID yet\n",
                                             encoding="utf-8")
        data = self.run_json("log", "inbox:1", "Hi.", directory=self.plain)
        self.assertRegex(data["ref"], r"^t[0-9a-z]{3}$")
        self.assertIn("^" + data["ref"], (self.todos / "inbox.md").read_text(encoding="utf-8"))

    def test_log_of_a_linear_issue_needs_no_connection(self):
        data = self.run_json("log", "eng-123", "Started.", directory=self.plain)
        self.assertEqual(data["ref"], "ENG-123")

    def test_an_empty_message_is_a_usage_error(self):
        ref = self.add()
        data = self.run_json("log", ref, "  ", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_show_has_the_last_ten_entries(self):
        ref = self.add()
        for number in range(12):
            self.run_json("log", ref, f"Entry {number}.", directory=self.plain)
        data = self.run_json("show", ref, directory=self.plain)
        self.assertEqual([e["message"] for e in data["log"]], [f"Entry {n}." for n in range(2, 12)])
        data = self.run_json("show", ref, "--log", directory=self.plain)
        self.assertEqual(len(data["log"]), 12)

    def test_show_text_lists_the_log(self):
        ref = self.add()
        self.run_json("log", ref, "Ready for review.\nTwo commits.", directory=self.plain)
        out, _ = self.run_cli("show", ref, directory=self.plain)
        self.assertRegex(out, r"--- log ---\n\d{4}-\d\d-\d\d \d\d:\d\d · pi · Ready for review\.\n\tTwo commits\.\n$")

    def test_show_without_a_log_has_an_empty_list(self):
        ref = self.add()
        self.assertEqual(self.run_json("show", ref, directory=self.plain)["log"], [])
        out, _ = self.run_cli("show", ref, directory=self.plain)
        self.assertNotIn("--- log ---", out)


class EditGuardTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.ref = self.run_json("add", "Fix the login timeout", directory=self.plain)["card"]["ref"]
        state = Path(self.tmp.name) / "state" / "herdboard"
        state.mkdir(parents=True)
        (state / "starts.json").write_text(json.dumps({
            self.ref: {"ref": self.ref, "steps_done": [], "pane_id": "w1:p2", "agent_name": "hb-x"},
            "other": {"ref": "tnone", "steps_done": [], "pane_id": "w1:p9"},
        }), encoding="utf-8")

    def test_a_card_agent_cannot_edit_card_text(self):
        for option in (["--title", "New"], ["--body", "x"]):
            with self.subTest(option=option), mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}):
                data = self.run_json("edit", self.ref, *option, directory=self.plain, expect=1)
                self.assertEqual(data["error"]["code"], "agent_edit_denied")
                self.assertIn("herdboard log", data["error"]["message"])

    def test_a_card_agent_can_still_log_and_move(self):
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}), \
                mock.patch.object(activity, "author", return_value="pi"):
            self.run_json("log", self.ref, "Ready.", directory=self.plain)
            self.run_json("move", self.ref, "Review", directory=self.plain)

    def test_other_panes_can_edit(self):
        for pane in ("w1:p3", "w1:p9"):
            with self.subTest(pane=pane), mock.patch.dict(os.environ, {"HERDR_PANE_ID": pane}):
                self.run_json("edit", self.ref, "--title", "New " + pane, directory=self.plain)

    def test_outside_herdr_editing_works(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("HERDR_PANE_ID", None)
            self.run_json("edit", self.ref, "--title", "New", directory=self.plain)


if __name__ == "__main__":
    unittest.main()
