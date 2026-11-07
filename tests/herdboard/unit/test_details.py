"""The card details pane: its data and its lines, without a terminal."""

import json
import os
import subprocess
import unittest
from datetime import datetime
from unittest import mock

from herdboard import activity, details, start
from herdboard.config import Config
from herdboard.store import CardView
from support.herdboard_cli import CliTestCase
from support.herdboard_details import StoreCase
from support.herdboard_linear_cli import LinearCliCase


def card(**fields):
    values = dict(
        ref="t123",
        board="acme-api",
        path=None,
        column="In progress",
        title="Fix login timeout",
        checked=False,
        due=None,
        tags=["api"],
        body=[],
        type="feature",
    )
    values.update(fields)
    return CardView(**values)


RECORD = {
    "ref": "t123",
    "agent": "claude",
    "model": "opus",
    "thinking": "high",
    "branch": "t123-fix-login-timeout",
    "base": "main",
    "pr_url": "https://github.com/acme/api/pull/12",
    "pr_state": "open",
}


def log(count):
    return [
        {
            "time": f"2026-10-{index + 1:02d} 14:0{index % 10}",
            "author": "claude" if index % 2 else "herdboard",
            "message": f"Entry {index + 1}\nsecond line",
        }
        for index in range(count)
    ]


def plain(lines):
    return [getattr(line, "plain", line) for line in lines]


class DetailsLinesTests(unittest.TestCase):
    def test_a_started_card_with_everything(self):
        data = {
            "ref": "t123",
            "card": card(body=["- [x] one", "- [ ] two", "- [x] three", "notes"]),
            "record": RECORD,
            "attachments": [{"name": "plan.md"}, {"name": "mockup-pane.png"}],
            "log": log(8),
        }
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
        self.assertEqual(
            [line.split()[-2] + " " + line.split()[-1] for line in lines[3:]], ["Entry 6", "Entry 7", "Entry 8"]
        )

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
        data = {"ref": "t123", "card": long, "record": RECORD, "attachments": [{"name": "plan.md"}], "log": entries}
        lines = plain(details.details_lines(data, 40, 10))
        self.assertTrue(all(len(line) <= 40 for line in lines), lines)
        self.assertTrue(lines[0].endswith("feature"))
        self.assertIn("…", lines[0])
        self.assertTrue(lines[1].endswith("…"))
        self.assertTrue(lines[-1].endswith("…"))

    def test_a_wide_title_is_cut_to_the_width_in_cells(self):
        # 31 characters, 62 cells.
        wide = card(title="ログイン画面のタイムアウト処理を修正して再試行できるようにする")
        data = {"ref": "t123", "card": wide, "record": None, "attachments": [], "log": []}
        first = details.details_lines(data, 60, 5)[0]
        self.assertEqual(first.cell_len, 60)
        self.assertIn("…", first.plain)
        self.assertTrue(first.plain.endswith(" In progress  feature"), first.plain)

    def test_a_wide_title_that_fits_pads_the_type_to_the_right_edge(self):
        wide = card(title="ログイン修正")
        data = {"ref": "t123", "card": wide, "record": None, "attachments": [], "log": []}
        first = details.details_lines(data, 60, 5)[0]
        self.assertEqual(first.cell_len, 60)
        self.assertTrue(first.plain.startswith("t123 │ ログイン修正  In progress "), first.plain)
        self.assertTrue(first.plain.endswith("feature"), first.plain)

    def test_a_linear_card_shows_its_state_as_the_column(self):
        issue = card(
            ref="ENG-12", title="Speed up search", source="linear", state="In Review", column="Review", type=None
        )
        data = {"ref": "ENG-12", "card": issue, "record": None, "attachments": [], "log": []}
        first = plain(details.details_lines(data, 60, 5))[0]
        self.assertEqual(first, "ENG-12 │ Speed up search  In Review ")


class DetailsDataTests(StoreCase, unittest.TestCase):
    def test_collects_the_card_record_attachments_and_log(self):
        ref = self.card.ref
        self.write_records({ref: {**RECORD, "ref": ref}})
        activity.append(self.store, ref, "Started.", "claude", now=datetime(2026, 10, 29, 13, 28))
        folder = self.card.path / "cards" / ref / "attachments"
        folder.mkdir()
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
        self.assertEqual(
            paths,
            [
                self.card.path / "board.toml",
                self.card.path / "cards" / ref / "card.md",
                activity.log_path(self.config, ref),
                self.card.path / "cards" / ref / "attachments",
                self.root / "state" / "herdboard" / "starts.json",
            ],
        )

    def test_signature_changes_when_the_card_file_changes(self):
        ref = self.card.ref
        paths = details.watched_paths(self.config, ref, self.card.path)
        before = details.signature(paths)
        card_file = self.card.path / "cards" / ref / "card.md"
        os.utime(card_file, ns=(1, 1))
        self.assertNotEqual(details.signature(paths), before)

    def test_signature_changes_when_a_watched_file_changes(self):
        ref = self.card.ref
        paths = details.watched_paths(self.config, ref, self.card.path)
        before = details.signature(paths)
        activity.append(self.store, ref, "Started.", "claude")
        self.assertNotEqual(details.signature(paths), before)


class CachedLookupTests(unittest.TestCase):
    def test_asks_again_only_after_the_interval(self):
        from herdboard.details_view import CachedLookup

        now = [0.0]
        calls = []
        lookup = CachedLookup(lambda ref: calls.append(ref) or len(calls), seconds=60, clock=lambda: now[0])
        self.assertEqual((lookup("ENG-1"), lookup("ENG-1")), (1, 1))
        now[0] = 59
        self.assertEqual(lookup("ENG-1"), 1)
        now[0] = 60
        self.assertEqual(lookup("ENG-1"), 2)

    def test_a_failed_lookup_waits_for_the_interval_too(self):
        from herdboard.details_view import CachedLookup
        from herdboard.model import NotFoundError

        now = [0.0]
        calls = []

        def missing(ref):
            calls.append(ref)
            raise NotFoundError(f"No card {ref}")

        lookup = CachedLookup(missing, seconds=60, clock=lambda: now[0])
        for _ in range(3):
            with self.assertRaises(NotFoundError):
                lookup("ENG-1")
        self.assertEqual(len(calls), 1)
        self.assertTrue(
            lookup.at is not None and lookup.clock() - lookup.at < lookup.seconds,
            "the view's check finds the failed lookup not due",
        )
        now[0] = 60
        with self.assertRaises(NotFoundError):
            lookup("ENG-1")
        self.assertEqual(len(calls), 2)


class DetailsCliTests(CliTestCase):
    def test_details_runs_the_view_for_the_card_and_creates_no_board(self):
        ref = self.run_json("add", "Ship it", directory=self.plain)["card"]["ref"]
        with (
            mock.patch("herdboard.details_view.DetailsApp.run") as run,
            mock.patch("herdboard.details_view.DetailsApp.__init__", return_value=None) as made,
        ):
            self.run_cli("details", ref, directory=self.repo)
        run.assert_called_once_with()
        self.assertEqual(made.call_args.args[2], ref)
        self.assertFalse(list(self.todos.glob("acme*.md")))


class DetailsLinearCliTests(LinearCliCase):
    def test_a_lookup_after_a_network_failure_asks_linear_again(self):
        from herdboard.linear import LinearClient, LinearNetworkError
        from herdboard.model import HerdboardError

        with (
            mock.patch("herdboard.details_view.DetailsApp.run"),
            mock.patch("herdboard.details_view.DetailsApp.__init__", return_value=None) as made,
        ):
            self.run_cli("details", "eng-3", directory=self.plain)
        linear_card = made.call_args.args[3]
        issue = LinearClient.issue
        calls = []

        def down_once(client, identifier):
            calls.append(identifier)
            if len(calls) == 1:
                raise LinearNetworkError("down")
            return issue(client, identifier)

        with mock.patch.object(LinearClient, "issue", autospec=True, side_effect=down_once):
            with self.assertRaises(HerdboardError):
                linear_card("ENG-3")
            self.assertEqual(linear_card("ENG-3").ref, "ENG-3")
        self.assertEqual(calls, ["ENG-3", "ENG-3"])


class PaneHerdr:
    """A fake `herdr` for the details pane: one tab with the agent pane, and maybe a details pane."""

    def __init__(self, labels=None, focused="w1:t1"):
        self.calls = []
        self.labels = labels or {}
        self.focused = focused
        self.fail = None

    def __call__(self, argv, timeout=120, stdin=None):
        args = argv[1:]
        self.calls.append(args)
        operation = " ".join(args[:2])
        if self.fail == operation:
            return subprocess.CompletedProcess(argv, 1, "", json.dumps({"error": {"code": "broken", "message": "no"}}))
        result = {}
        if operation == "pane get":
            result = {"pane": {"pane_id": args[2], "tab_id": "w1:t1", "workspace_id": "w1"}}
        elif operation == "pane list":
            result = {
                "panes": [
                    {"pane_id": "w1:p1", "tab_id": "w1:t1"},
                    *({"pane_id": pane, "tab_id": "w1:t1", "label": label} for pane, label in self.labels.items()),
                ]
            }
        elif operation == "tab list":
            result = {
                "tabs": [
                    {"tab_id": "w1:t1", "focused": self.focused == "w1:t1"},
                    {"tab_id": "w2:t1", "focused": self.focused == "w2:t1"},
                ]
            }
        elif operation == "pane split":
            result = {"pane": {"pane_id": "w1:p2", "tab_id": "w1:t1"}}
        elif operation == "pane run":
            return subprocess.CompletedProcess(argv, 0, "", "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": result}), "")

    def operations(self):
        return [" ".join(call[:2]) for call in self.calls]


class OpenDetailsPaneTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.fake = PaneHerdr()
        patcher = mock.patch.object(start, "command", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        command = mock.patch.object(start, "herdboard_command", return_value="herdboard")
        command.start()
        self.addCleanup(command.stop)

    def test_splits_swaps_renames_and_runs_the_view(self):
        self.assertEqual(start.open_details_pane("t123", "w1:p1", self.config), "w1:p2")
        self.assertEqual(
            self.fake.calls,
            [
                ["pane", "get", "w1:p1"],
                ["pane", "list", "--workspace", "w1"],
                ["tab", "list"],
                ["pane", "split", "w1:p1", "--direction", "down", "--ratio", "0.25", "--no-focus"],
                ["pane", "swap", "--source-pane", "w1:p1", "--target-pane", "w1:p2"],
                ["pane", "rename", "w1:p2", "details - t123"],
                ["pane", "run", "w1:p2", "herdboard details t123"],
            ],
        )

    def test_the_swap_gives_the_focus_back_to_the_users_tab(self):
        self.fake.focused = "w2:t1"
        self.config.details_pane_ratio = 0.4
        start.open_details_pane("t123", "w1:p1", self.config)
        self.assertIn(
            ["pane", "split", "w1:p1", "--direction", "down", "--ratio", "0.4", "--no-focus"], self.fake.calls
        )
        operations = self.fake.operations()
        self.assertEqual(operations[operations.index("pane swap") + 1], "tab focus")
        self.assertIn(["tab", "focus", "w2:t1"], self.fake.calls)

    def test_nothing_when_the_setting_is_off(self):
        self.config.details_pane = False
        self.assertIsNone(start.open_details_pane("t123", "w1:p1", self.config))
        self.assertEqual(self.fake.calls, [])

    def test_no_second_pane_in_the_tab(self):
        self.fake.labels = {"w1:p5": "details - t123"}
        self.assertIsNone(start.open_details_pane("t123", "w1:p1", self.config))
        self.assertNotIn("pane split", self.fake.operations())

    def test_a_failed_run_closes_the_pane_and_notifies(self):
        self.fake.fail = "pane run"
        self.assertIsNone(start.open_details_pane("t123", "w1:p1", self.config))
        self.assertIn(["pane", "close", "w1:p2"], self.fake.calls)
        self.assertEqual(self.fake.operations()[-1], "notification show")


class OpenTargetTests(StoreCase, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.fake = PaneHerdr()
        patcher = mock.patch.object(start, "command", self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write_plans(self, records):
        path = self.root / "state" / "herdboard" / "plans.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(records))

    def test_finds_the_started_card_of_the_workspace(self):
        self.write_records(
            {
                "t555": {"ref": "t555", "workspace_id": "w9", "pane_id": "w9:p1"},
                "issue-id": {"ref": "ENG-1", "workspace_id": "w1", "pane_id": "w1:p1"},
            }
        )
        self.assertEqual(details.open_target(None, "w1:p3"), ("ENG-1", "w1:p1"))

    def test_a_planner_tab_wins_over_the_workspace_record(self):
        self.write_records({"t123": {"ref": "t123", "workspace_id": "w1", "pane_id": "w1:p1"}})
        self.write_plans({"t123": {"pane_id": "w1:p7", "workspace_id": "w1", "tab_id": "w1:t1"}})
        self.assertEqual(details.open_target(None, "w1:p3"), ("t123", "w1:p7"))

    def test_a_workspace_without_a_card_is_an_error(self):
        self.write_records({"t555": {"ref": "t555", "workspace_id": "w9", "pane_id": "w9:p1"}})
        with self.assertRaisesRegex(details.HerdboardError, "No herdboard card"):
            details.open_target(None, "w1:p3")

    def test_a_named_card_uses_its_agent_pane_else_its_planner_else_the_given_pane(self):
        self.write_records({"t123": {"ref": "t123", "workspace_id": "w1", "pane_id": "w1:p1"}})
        self.write_plans({"t456": {"pane_id": "w2:p7", "workspace_id": "w2", "tab_id": "w2:t3"}})
        self.assertEqual(details.open_target("t123", "w5:p1"), ("t123", "w1:p1"))
        self.assertEqual(details.open_target("t456", "w5:p1"), ("t456", "w2:p7"))
        self.assertEqual(details.open_target("t789", "w5:p1"), ("t789", "w5:p1"))
        with self.assertRaisesRegex(details.HerdboardError, "pane"):
            details.open_target("t789", None)


class DetailsOpenCliTests(CliTestCase):
    def test_open_without_a_ref_opens_the_pane_for_the_workspace_card(self):
        with (
            mock.patch("herdboard.details.open_target", return_value=("t123", "w1:p1")) as target,
            mock.patch("herdboard.start.open_details_pane", return_value="w1:p2") as opened,
        ):
            result = self.run_json("details", "--open", "--pane", "w1:p3", directory=self.repo)
        target.assert_called_once_with(None, "w1:p3")
        self.assertEqual(opened.call_args.args[:2], ("t123", "w1:p1"))
        self.assertEqual(result, {"ref": "t123", "host_pane_id": "w1:p1", "pane_id": "w1:p2"})

    def test_the_pane_defaults_to_this_herdr_pane(self):
        with (
            mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w4:p2"}),
            mock.patch("herdboard.details.open_target", return_value=("t123", "w4:p2")) as target,
            mock.patch("herdboard.start.open_details_pane", return_value=None),
        ):
            self.run_json("details", "--open", directory=self.repo)
        target.assert_called_once_with(None, "w4:p2")

    def test_the_view_needs_a_ref(self):
        out, err = self.run_cli("details", directory=self.repo, expect=1)
        self.assertIn("card", err)


if __name__ == "__main__":
    unittest.main()
