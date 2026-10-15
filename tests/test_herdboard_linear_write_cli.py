import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

import linear_data  # noqa: E402
from herdboard import cli  # noqa: E402
from herdboard.linear_links import LabelLink, ProjectLink  # noqa: E402
from test_herdboard_linear_cli import LinearCliCase  # noqa: E402


class WriteCliCase(LinearCliCase):
    def setUp(self):
        super().setUp()
        linear_data.with_workflow(self.fixtures)

    def issue(self, identifier):
        return next(i for i in self.fixtures["issues"] if i["identifier"] == identifier)

    def inputs(self, operation):
        return [r["variables"]["input"] for r in self.requests(operation)]

    def error(self, *args, expect=1, directory=None):
        return self.run_json(*args, directory=directory or self.repo, expect=expect)["error"]


class MoveCommandTests(WriteCliCase):
    def test_move_prints_the_card_line(self):
        out, _ = self.run_cli("move", "ENG-1", "In progress", directory=self.repo)
        self.assertEqual(out, "ENG-1 → In progress (Platform)\n")
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Doing")

    def test_move_json_prints_the_issue_card(self):
        card = self.run_json("move", "eng-2", "Todo", directory=self.repo)["card"]
        self.assertEqual((card["ref"], card["column"], card["source"], card["state"]),
                         ("ENG-2", "Todo", "linear", "Todo"))
        self.assertEqual((card["tags"], card["type"]), (["repo:api"], "bug"))
        self.assertEqual(card["title"], "Issue 2")
        self.assertEqual(card["url"], "https://linear.app/workspace/issue/ENG-2")

    def test_move_to_the_current_state_changes_nothing(self):
        card = self.run_json("move", "ENG-1", "Todo", directory=self.repo)["card"]
        self.assertEqual(card["column"], "Todo")
        self.assertEqual(self.requests("IssueUpdate"), [])

    def test_move_without_a_position_sends_no_sort_order(self):
        self.run_cli("move", "ENG-1", "In progress", directory=self.repo)
        self.assertEqual(self.inputs("IssueUpdate"), [{"stateId": "ws-ENG-Doing"}])

    def test_position_top_and_bottom(self):
        self.run_cli("move", "ENG-1", "In progress", "--position", "top", directory=self.repo)
        self.assertEqual(self.inputs("IssueUpdate")[-1], {"stateId": "ws-ENG-Doing", "sortOrder": 4.0})
        self.run_cli("move", "ENG-3", "In progress", "--position", "bottom", directory=self.repo)
        # ENG-1 now has sort order 4.0 and ENG-5 has 5.0.
        self.assertEqual(self.inputs("IssueUpdate")[-1], {"stateId": "ws-ENG-Doing", "sortOrder": 6.0})

    def test_unknown_issue_is_a_user_error(self):
        error = self.error("move", "ENG-99", "Todo")
        self.assertEqual(error["code"], "not_found")
        self.assertIn("ENG-99", error["message"])

    def test_column_without_a_state_names_team_and_column(self):
        error = self.error("move", "ENG-1", "Nowhere")
        self.assertEqual(error["code"], "no_state")
        self.assertIn("ENG", error["message"])
        self.assertIn("Nowhere", error["message"])

    def test_offline_move_is_refused(self):
        error = self.error("move", "ENG-1", "In progress", "--offline", expect=3)
        self.assertEqual(error["code"], "linear_offline")
        _, err = self.run_cli("move", "ENG-1", "In progress", "--offline", directory=self.repo, expect=3)
        self.assertRegex(err, r"^herdboard: Linear writes need a connection")
        self.assertEqual(self.fake.requests, [])

    def test_rate_limited_write_exits_3_after_one_attempt(self):
        self.fake.mutation_failure = "http_429"
        error = self.error("move", "ENG-1", "In progress", expect=3)
        self.assertIn("rate limited", error["message"])
        self.assertEqual(len(self.requests("IssueUpdate")), 1)

    def test_write_with_unknown_result_exits_3(self):
        self.fake.mutation_failure = "http_500"
        error = self.error("move", "ENG-1", "In progress", expect=3)
        self.assertEqual(error["code"], "linear_unknown")

    def test_linear_off(self):
        del os.environ["LINEAR_API_KEY"]
        self.assertEqual(self.error("move", "ENG-1", "Todo")["code"], "linear_disabled")

    def test_move_of_a_todo_still_works_and_rejects_state(self):
        card = self.run_json("add", "todo", directory=self.repo)["card"]
        moved = self.run_json("move", card["ref"], "Todo", directory=self.repo)["card"]
        self.assertEqual(moved["column"], "Todo")
        error = self.error("move", card["ref"], "Done", "--state", "x")
        self.assertEqual(error["code"], "usage")

    def test_move_of_a_todo_defaults_to_the_bottom(self):
        self.run_json("add", "first", "--column", "Todo", directory=self.repo)
        second = self.run_json("add", "second", directory=self.repo)["card"]
        self.run_cli("move", second["ref"], "Todo", directory=self.repo)
        out, _ = self.run_cli("list", "--source", "todo", directory=self.repo)
        self.assertLess(out.index("first"), out.index("second"))

    def test_a_write_drops_the_cache(self):
        self.listing("--view", "mine")
        count = len(self.requests("MyIssues"))
        self.run_cli("move", "ENG-1", "In progress", directory=self.repo)
        data = self.listing("--view", "mine")
        self.assertEqual(len(self.requests("MyIssues")), count + 1)
        self.assertEqual({c["ref"]: c["column"] for c in data["cards"]}["ENG-1"], "In progress")
        self.assertFalse(data["linear"]["stale"])


class DoneCommandTests(WriteCliCase):
    def test_done_moves_to_the_completed_state(self):
        out, _ = self.run_cli("done", "ENG-1", directory=self.repo)
        self.assertEqual(out, "ENG-1 → Done (Platform)\n")
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Done")

    def test_done_json(self):
        card = self.run_json("done", "ENG-2", directory=self.repo)["card"]
        self.assertEqual((card["column"], card["checked"]), ("Done", True))

    def test_done_offline_is_refused(self):
        self.assertEqual(self.error("done", "ENG-1", "--offline", expect=3)["code"], "linear_offline")

    def test_done_of_a_todo_still_works(self):
        card = self.run_json("add", "todo", directory=self.repo)["card"]
        self.assertEqual(self.run_json("done", card["ref"], directory=self.repo)["card"]["column"], "Done")


class AmbiguousStateTests(WriteCliCase):
    def setUp(self):
        super().setUp()
        linear_data.add_state(self.fixtures, "ENG", "Coding", "started")

    def test_without_a_terminal_the_error_lists_the_candidates(self):
        _, err = self.run_cli("move", "ENG-1", "In progress", directory=self.repo, expect=1)
        self.assertRegex(err, r"^herdboard: Several states of team ENG match column 'In progress': "
                              r"Doing, Coding")
        self.assertEqual(self.requests("IssueUpdate"), [])

    def test_json_error_carries_the_candidates(self):
        error = self.error("move", "ENG-1", "In progress")
        self.assertEqual(error["code"], "ambiguous_state")
        self.assertEqual(error["candidates"], ["Doing", "Coding"])

    def test_json_never_asks_even_on_a_terminal(self):
        with mock.patch.object(cli, "_interactive", return_value=True):
            error = self.error("move", "ENG-1", "In progress")
        self.assertEqual(error["code"], "ambiguous_state")

    def test_state_option_answers_and_is_stored(self):
        self.run_cli("move", "ENG-1", "In progress", "--state", "coding", directory=self.repo)
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Coding")
        self.run_cli("move", "ENG-3", "In progress", directory=self.repo)
        self.assertEqual(self.issue("ENG-3")["state"]["name"], "Coding")
        stored = json.loads((self.state / "herdboard/o1/u1/state-choices.json").read_text())
        self.assertEqual(stored, {"t-eng": {"in progress": "ws-ENG-Coding"}})

    def test_unknown_state_name_lists_the_candidates(self):
        error = self.error("move", "ENG-1", "In progress", "--state", "Nope")
        self.assertEqual(error["candidates"], ["Doing", "Coding"])
        self.assertIn("Nope", error["message"])

    def run_on_terminal(self, answers, *args):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_interactive", return_value=True), \
                mock.patch.object(sys, "stdin", io.StringIO(answers)):
            import contextlib
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                code = cli.main(["--config", str(self.config), "-C", str(self.repo), *args])
        return code, out.getvalue(), err.getvalue()

    def test_terminal_prompt_with_a_numbered_list(self):
        code, out, err = self.run_on_terminal("2\n", "move", "ENG-1", "In progress")
        self.assertEqual(code, 0, err)
        self.assertEqual(out, "ENG-1 → In progress (Platform)\n")
        self.assertIn("1) Doing", err)
        self.assertIn("2) Coding", err)
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Coding")
        # The answer is stored: no question the second time.
        code, _, err = self.run_on_terminal("", "move", "ENG-3", "In progress")
        self.assertEqual((code, err), (0, ""))
        self.assertEqual(self.issue("ENG-3")["state"]["name"], "Coding")

    def test_terminal_prompt_asks_again_after_a_bad_answer(self):
        code, _, err = self.run_on_terminal("x\n9\n1\n", "move", "ENG-1", "In progress")
        self.assertEqual(code, 0, err)
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Doing")
        self.assertEqual(err.count("Choose"), 3)

    def test_terminal_prompt_without_an_answer_is_cancelled(self):
        code, _, err = self.run_on_terminal("", "move", "ENG-1", "In progress")
        self.assertEqual(code, 1)
        self.assertIn("No state chosen", err)
        self.assertEqual(self.requests("IssueUpdate"), [])


class AddLinearTests(WriteCliCase):
    def linked(self, *args):
        if not (self.todos / "projects" / "acme-api.md").exists():
            self.board()
        return self.link(*args)

    def add(self, *args, directory=None, expect=0):
        return self.run_json("add", "--linear", *args, directory=directory or self.plain, expect=expect)

    def test_team_option_creates_an_issue_assigned_to_you(self):
        card = self.add("--team", "ENG", "New thing")["card"]
        self.assertEqual((card["ref"], card["title"], card["column"], card["source"]),
                         ("ENG-7", "New thing", "Backlog", "linear"))
        (inputs,) = self.inputs("IssueCreate")
        self.assertEqual(inputs, {"id": inputs["id"], "teamId": "t-eng", "title": "New thing",
                                  "stateId": "ws-ENG-Backlog", "assigneeId": "u1"})

    def test_text_output(self):
        out, _ = self.run_cli("add", "--linear", "--team", "OPS", "--column", "Todo", "Fix it",
                              directory=self.plain)
        self.assertEqual(out, "OPS-7 → Todo (Linear)\n")

    def test_without_team_project_or_single_team_the_error_lists_team_keys(self):
        error = self.add("New thing", expect=1)["error"]
        self.assertEqual(error["code"], "team_required")
        self.assertIn("ENG", error["message"])
        self.assertIn("OPS", error["message"])
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_the_single_team_of_the_viewer(self):
        self.fixtures["my_team_ids"] = ["t-ops"]
        self.assertEqual(self.add("New thing")["card"]["team"], "OPS")

    def test_project_by_name_gives_its_single_team(self):
        card = self.add("--project", "platform", "New thing")["card"]
        self.assertEqual((card["team"], card["board"]), ("ENG", "Platform"))
        self.assertEqual(self.inputs("IssueCreate")[0]["projectId"], "p-plat")

    def test_ambiguous_project_name_is_an_error(self):
        error = self.add("--project", "twin", "x", expect=1)["error"]
        self.assertIn("2 Linear projects", error["message"])

    def test_labels_use_the_team_label_rule(self):
        card = self.add("--team", "ENG", "--label", "ENG/bug", "--label", "repo:api", "x")["card"]
        self.assertEqual((card["type"], card["tags"]), ("bug", ["repo:api"]))
        self.assertEqual(self.inputs("IssueCreate")[0]["labelIds"], ["l-bug", "l-api"])
        error = self.add("--team", "ENG", "--label", "bug", "x", expect=1)["error"]
        self.assertIn("TEAM/label", error["message"])

    def test_type_adds_the_label_of_that_name(self):
        card = self.add("--team", "ENG", "--type", "Bug", "--label", "repo:api", "x")["card"]
        self.assertEqual(self.inputs("IssueCreate")[0]["labelIds"], ["l-api", "l-bug"])
        self.assertEqual((card["type"], card["tags"]), ("bug", ["repo:api"]))

    def test_the_type_label_follows_the_project_team(self):
        card = self.add("--project", "Platform", "--type", "bug", "x")["card"]
        (inputs,) = self.inputs("IssueCreate")
        self.assertEqual((inputs["teamId"], inputs["labelIds"]), ("t-eng", ["l-bug"]))
        self.assertEqual(card["type"], "bug")

    def test_the_type_label_of_the_chosen_team(self):
        self.add("--team", "OPS", "--type", "bug", "x")
        self.assertEqual(self.inputs("IssueCreate")[0]["labelIds"], ["l-bug-ops"])

    def test_a_workspace_type_label_serves_a_team_without_one(self):
        self.fixtures["labels"].append({"id": "l-chore", "name": "Chore", "team": None})
        self.add("--team", "ENG", "--type", "chore", "x")
        self.assertEqual(self.inputs("IssueCreate")[0]["labelIds"], ["l-chore"])

    def test_a_type_without_a_label_creates_nothing(self):
        error = self.add("--team", "ENG", "--type", "docs", "x", expect=1)["error"]
        self.assertIn("docs", error["message"])
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_an_unknown_type_creates_nothing(self):
        error = self.add("--team", "ENG", "--type", "epic", "x", expect=1)["error"]
        self.assertEqual(error["code"], "unknown_type")
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_unknown_label_creates_nothing(self):
        self.add("--team", "ENG", "--label", "nope", "x", expect=1)
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_column_and_state(self):
        linear_data.add_state(self.fixtures, "ENG", "Coding", "started")
        error = self.add("--team", "ENG", "--column", "In progress", "x", expect=1)["error"]
        self.assertEqual(error["candidates"], ["Doing", "Coding"])
        card = self.add("--team", "ENG", "--column", "In progress", "--state", "Coding", "x")["card"]
        self.assertEqual((card["column"], card["state"]), ("In progress", "Coding"))

    def test_repository_with_one_linked_project_supplies_project_and_labels(self):
        self.linked("Platform", "--label", "repo:api")
        card = self.add("From the repo", directory=self.repo)["card"]
        (inputs,) = self.inputs("IssueCreate")
        self.assertEqual((inputs["projectId"], inputs["labelIds"], inputs["teamId"]),
                         ("p-plat", ["l-api"], "t-eng"))
        data = self.listing()
        self.assertIn(card["ref"], self.refs(data))

    def test_explicit_labels_are_added_to_the_link_labels(self):
        self.linked("Platform", "--label", "repo:api")
        self.add("--label", "repo:web", "x", directory=self.repo)
        self.assertEqual(self.inputs("IssueCreate")[0]["labelIds"], ["l-api", "l-web"])

    def test_a_link_without_labels_sends_no_labels(self):
        self.linked("Platform")
        self.add("x", directory=self.repo)
        inputs = self.inputs("IssueCreate")[0]
        self.assertEqual(inputs["projectId"], "p-plat")
        self.assertNotIn("labelIds", inputs)

    def test_the_project_option_replaces_the_repository_default(self):
        self.linked("Platform", "--label", "repo:api")
        self.add("--project", "Mobile", "--team", "ENG", "x", directory=self.repo)
        inputs = self.inputs("IssueCreate")[0]
        self.assertEqual(inputs["projectId"], "p-mob")
        self.assertNotIn("labelIds", inputs)

    def test_two_linked_projects_give_no_default(self):
        self.linked("Platform")
        self.linked("Mobile")
        error = self.add("x", directory=self.repo, expect=1)["error"]
        self.assertEqual(error["code"], "team_required")

    def test_repository_without_a_board_gives_no_default(self):
        self.add("--team", "ENG", "x", directory=self.repo)
        self.assertNotIn("projectId", self.inputs("IssueCreate")[0])

    def test_renamed_linked_project_uses_stable_id_and_current_name(self):
        self.linked("Platform")
        project = next(p for p in self.fixtures["projects"] if p["id"] == "p-plat")
        project["name"] = "Renamed Platform"
        card = self.add("x", directory=self.repo)["card"]
        self.assertEqual(self.inputs("IssueCreate")[0]["projectId"], "p-plat")
        self.assertEqual(card["board"], "Renamed Platform")

    def test_linked_project_that_is_gone_is_an_error(self):
        self.board()
        self.relink(ProjectLink("gone", "Old project", [LabelLink("l-api", "repo:api")]))
        error = self.add("--team", "ENG", "x", directory=self.repo, expect=1)["error"]
        self.assertIn("Old project", error["message"])
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_offline_is_refused(self):
        error = self.add("--team", "ENG", "x", "--offline", expect=3)["error"]
        self.assertEqual(error["code"], "linear_offline")
        self.assertEqual(self.fake.requests, [])

    def test_json_create_warns_when_invalidation_timestamp_write_fails(self):
        from herdboard import linear_source
        real = linear_source._write_json

        def fail_timestamp(path, data):
            if path.name == "cache-invalidated":
                raise PermissionError("denied")
            return real(path, data)

        with mock.patch.object(linear_source, "_write_json", side_effect=fail_timestamp):
            out, err = self.run_cli("--json", "add", "--linear", "--team", "ENG", "x",
                                    directory=self.repo)
        self.assertEqual(json.loads(out)["card"]["title"], "x")
        self.assertIn("warning", err)
        self.assertIn("cache", err)
        self.assertEqual(len(self.requests("IssueCreate")), 1)

    def test_json_create_succeeds_when_cache_deletion_fails(self):
        self.run_json("list", "--view", "mine", directory=self.repo)
        with mock.patch.object(Path, "unlink", side_effect=PermissionError("denied")):
            card = self.add("--team", "ENG", "x", directory=self.repo)["card"]
        self.assertEqual(card["title"], "x")
        self.assertEqual(len(self.requests("IssueCreate")), 1)
        error = self.error("show", "ENG-1", "--offline", expect=3)
        self.assertIn("No cached", error["message"])

    def test_create_whose_result_is_unknown_is_looked_up_by_its_id(self):
        self.fake.mutation_failure = "http_500"
        error = self.add("--team", "ENG", "x", expect=3)["error"]
        self.assertEqual(error["code"], "linear_unknown")
        self.assertEqual(error["issue_id"], self.inputs("IssueCreate")[0]["id"])
        self.assertIn(error["issue_id"], error["message"])
        self.assertEqual(len(self.requests("IssueCreate")), 1)
        lookups = self.requests("IssueForWrite")
        self.assertEqual(lookups[-1]["variables"]["id"], self.inputs("IssueCreate")[0]["id"])

    def test_linear_options_need_linear(self):
        for args in (["--team", "ENG"], ["--project", "Platform"], ["--label", "x"], ["--state", "x"]):
            error = self.error("add", "x", *args)
            self.assertEqual(error["code"], "usage")
            self.assertIn("--linear", error["message"])

    def test_linear_and_board_conflict(self):
        error = self.error("add", "--linear", "--board", "inbox", "x")
        self.assertEqual(error["code"], "usage")

    def test_linear_off(self):
        del os.environ["LINEAR_API_KEY"]
        self.assertEqual(self.add("--team", "ENG", "x", expect=1)["error"]["code"], "linear_disabled")

    def test_local_add_is_unchanged(self):
        card = self.run_json("add", "buy milk", directory=self.plain)["card"]
        self.assertEqual((card["source"], card["board"]), ("todo", "inbox"))
        self.assertEqual(self.fake.requests, [])


class CardOrderTests(WriteCliCase):
    def setUp(self):
        super().setUp()
        self.board()
        self.link("Platform")
        self.run_cli("add", "local todo", "--column", "Todo", directory=self.repo)
        self.issue("ENG-1")["sortOrder"] = 10.0
        self.issue("ENG-3")["sortOrder"] = -1.0

    def column(self, *args):
        return self.refs(self.listing(*args, "--column", "Todo"))

    def test_linear_cards_follow_sort_order_after_the_local_cards(self):
        refs = self.column()
        self.assertEqual(refs[1:], ["ENG-3", "ENG-1"])
        self.assertEqual(refs[0][0], "t")

    def test_text_list_uses_the_same_order(self):
        out, _ = self.run_cli("list", "--column", "Todo", directory=self.repo)
        self.assertLess(out.index("local todo"), out.index("Issue 3"))
        self.assertLess(out.index("Issue 3"), out.index("Issue 1"))

    def test_other_views_use_the_same_order(self):
        self.assertEqual(self.column("--view", "mine"), ["ENG-1"])
        self.issue("ENG-5")["state"] = dict(self.issue("ENG-1")["state"])
        self.issue("ENG-5")["sortOrder"] = 3.0
        self.assertEqual(self.column("--view", "mine"), ["ENG-5", "ENG-1"])
        self.assertEqual(self.column("--view", "all")[-2:], ["ENG-5", "ENG-1"])
        self.assertEqual(self.column("--project", "Platform", "--view", "project")[1:], ["ENG-3", "ENG-1"])

    def test_a_moved_issue_takes_its_new_place(self):
        self.run_cli("move", "ENG-1", "Todo", "--position", "top", directory=self.repo)
        self.assertEqual(self.column()[1:], ["ENG-1", "ENG-3"])


if __name__ == "__main__":
    unittest.main()
