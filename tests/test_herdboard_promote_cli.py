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
from herdboard.store import Store  # noqa: E402
from test_herdboard_linear_write_cli import WriteCliCase  # noqa: E402


class PromoteCliCase(WriteCliCase):
    def setUp(self):
        super().setUp()
        self.fixtures["labels"].append({"id": "l-ship", "name": "ship", "team": None})
        self.path = self.board()

    def add_todo(self, text="Ship it #bug 📅 2026-12-01", column="Todo", *body):
        card = self.run_json("add", text, "--column", column, directory=self.repo)["card"]
        if body:
            marker = f"{text} ^{card['ref']}"
            self.path.write_text(self.path.read_text().replace(
                marker, marker + "".join(f"\n\t{line}" for line in body)))
        return card["ref"]

    def promote(self, *args, expect=0, directory=None):
        return self.run_json("promote", *args, directory=directory or self.repo, expect=expect)

    def board_text(self):
        return self.path.read_text()


class PromoteCommandTests(PromoteCliCase):
    def test_json_has_the_issue_card_the_sub_issues_and_the_todo(self):
        ref = self.add_todo("Ship it #bug 📅 2026-12-01", "Todo", "- [ ] First", "- [x] Second")
        data = self.promote(ref, "--team", "ENG")
        card = data["card"]
        self.assertEqual((card["ref"], card["title"], card["column"], card["source"], card["due"]),
                         ("ENG-7", "Ship it", "Todo", "linear", "2026-12-01"))
        self.assertEqual(card["tags"], ["bug"])
        self.assertEqual(data["sub_issues"], ["ENG-8", "ENG-9"])
        self.assertEqual((data["todo"]["ref"], data["todo"]["column"]), (ref, "Done"))
        self.assertIn(f"- [x] Ship it #bug 📅 2026-12-01 → ENG-7 ^{ref}", self.board_text())
        self.assertEqual(self.issue("ENG-9")["state"]["type"], "completed")

    def test_text_output(self):
        ref = self.add_todo("Ship it", "Todo", "- [ ] First")
        out, _ = self.run_cli("promote", ref, "--team", "ENG", directory=self.repo)
        self.assertEqual(out, f"ENG-7 → Todo (Linear)\nsub-issues: ENG-8\ntodo {ref} → Done (acme-api)\n")

    def test_text_output_without_sub_issues(self):
        ref = self.add_todo("Ship it")
        out, _ = self.run_cli("promote", ref, "--team", "ENG", directory=self.repo)
        self.assertEqual(out, f"ENG-7 → Todo (Linear)\ntodo {ref} → Done (acme-api)\n")

    def test_a_todo_in_done_becomes_a_completed_issue(self):
        ref = self.add_todo("Shipped", "Done")
        card = self.promote(ref, "--team", "ENG")["card"]
        self.assertEqual((card["column"], card["checked"]), ("Done", True))

    def test_a_todo_without_an_id_is_addressed_by_its_temporary_reference(self):
        self.path.write_text(self.path.read_text().replace("## Todo\n", "## Todo\n\n- [ ] Plain\n", 1))
        data = self.promote("acme-api:1", "--team", "ENG")
        self.assertRegex(data["todo"]["ref"], r"^t[0-9a-z]{3}$")
        self.assertIn(f"- [x] Plain → ENG-7 ^{data['todo']['ref']}", self.board_text())

    def test_without_team_the_error_lists_team_keys(self):
        ref = self.add_todo()
        error = self.promote(ref, expect=1)["error"]
        self.assertEqual(error["code"], "team_required")
        self.assertEqual(self.requests("IssueCreate"), [])
        self.assertNotIn("→ ENG", self.board_text())


class TargetTests(PromoteCliCase):
    def test_the_repository_default_supplies_project_labels_and_team(self):
        self.link("Platform", "--label", "repo:api")
        ref = self.add_todo()
        self.promote(ref)
        parent = self.inputs("IssueCreate")[0]
        self.assertEqual((parent["projectId"], parent["teamId"]), ("p-plat", "t-eng"))
        self.assertEqual(parent["labelIds"], ["l-api", "l-bug"])

    def other_repo(self):
        """A second repository whose board links Mobile."""
        import subprocess
        other = self.repo.parent / "web"
        other.mkdir()
        for args in (("init", "-q"), ("remote", "add", "origin", "https://github.com/acme/web.git")):
            subprocess.run(["git", *args], cwd=other, check=True)
        self.run_json("init", directory=other)
        self.run_json("link", "Mobile", directory=other)
        return other

    def test_the_default_comes_from_the_board_of_the_todo(self):
        self.link("Platform", "--label", "repo:api")
        ref = self.add_todo()
        other = self.other_repo()
        self.promote(ref, "--team", "ENG", directory=other)
        self.promote(self.add_todo("Again #bug"), "--team", "ENG", directory=self.plain)
        for parent in self.inputs("IssueCreate"):
            self.assertEqual(parent["projectId"], "p-plat")
            self.assertEqual(parent["labelIds"], ["l-api", "l-bug"])

    def test_an_inbox_todo_gets_no_linked_default(self):
        self.link("Platform", "--label", "repo:api")
        card = self.run_json("add", "Loose #bug", directory=self.plain)["card"]
        self.promote(card["ref"], "--team", "ENG")
        (parent,) = self.inputs("IssueCreate")
        self.assertNotIn("projectId", parent)
        self.assertEqual(parent["labelIds"], ["l-bug"])

    def test_the_project_option_replaces_the_repository_default(self):
        self.link("Platform", "--label", "repo:api")
        ref = self.add_todo()
        self.promote(ref, "--project", "Mobile", "--team", "ENG")
        parent = self.inputs("IssueCreate")[0]
        self.assertEqual(parent["projectId"], "p-mob")
        self.assertEqual(parent["labelIds"], ["l-bug"])

    def test_an_unknown_project_creates_nothing(self):
        ref = self.add_todo()
        error = self.promote(ref, "--project", "Nope", "--team", "ENG", expect=1)["error"]
        self.assertIn("Nope", error["message"])
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_a_linked_project_that_is_gone_is_an_error(self):
        from herdboard.linear_links import LabelLink, ProjectLink
        self.relink(ProjectLink("gone", "Old project", [LabelLink("l-api", "repo:api")]))
        ref = self.add_todo()
        error = self.promote(ref, "--team", "ENG", expect=1)["error"]
        self.assertIn("Old project", error["message"])
        self.assertEqual(self.requests("IssueCreate"), [])


class StateTests(PromoteCliCase):
    def setUp(self):
        super().setUp()
        linear_data.add_state(self.fixtures, "ENG", "Coding", "started")

    def test_an_ambiguous_column_lists_the_candidates_and_creates_nothing(self):
        ref = self.add_todo("Busy", "In progress")
        error = self.promote(ref, "--team", "ENG", expect=1)["error"]
        self.assertEqual((error["code"], error["candidates"]), ("ambiguous_state", ["Doing", "Coding"]))
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_state_answers_the_question(self):
        ref = self.add_todo("Busy", "In progress")
        card = self.promote(ref, "--team", "ENG", "--state", "coding")["card"]
        self.assertEqual((card["column"], card["state"]), ("In progress", "Coding"))


class RefusalTests(PromoteCliCase):
    def test_a_linear_issue_is_not_a_todo(self):
        error = self.promote("ENG-1", expect=1)["error"]
        self.assertEqual(error["code"], "not_a_todo")
        self.assertEqual(self.fake.requests, [])

    def test_a_promoted_todo_is_refused(self):
        ref = self.add_todo()
        self.promote(ref, "--team", "ENG")
        error = self.promote(ref, "--team", "ENG", expect=1)["error"]
        self.assertEqual(error["code"], "already_promoted")
        self.assertIn("ENG-7", error["message"])
        self.assertEqual(len(self.requests("IssueCreate")), 1)

    def test_an_unknown_todo(self):
        self.assertEqual(self.promote("t0zz", expect=1)["error"]["code"], "not_found")

    def test_offline_is_refused(self):
        ref = self.add_todo()
        before = self.board_text()
        error = self.promote(ref, "--team", "ENG", "--offline", expect=3)["error"]
        self.assertEqual(error["code"], "linear_offline")
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.board_text(), before)

    def test_text_error_for_an_offline_write(self):
        ref = self.add_todo()
        _, err = self.run_cli("promote", ref, "--offline", directory=self.repo, expect=3)
        self.assertRegex(err, r"^herdboard: Linear writes need a connection")

    def test_linear_off(self):
        ref = self.add_todo()
        del os.environ["LINEAR_API_KEY"]
        self.assertEqual(self.promote(ref, expect=1)["error"]["code"], "linear_disabled")

    def test_a_todo_that_is_not_in_a_board_with_a_done_column_is_refused(self):
        ref = self.add_todo()
        self.path.write_text(self.path.read_text().replace("**Complete**\n", "").replace("## Done", "## Finished"))
        error = self.promote(ref, "--team", "ENG", expect=1)["error"]
        self.assertIn("done column", error["message"])
        self.assertEqual(self.requests("IssueCreate"), [])


class FailureTests(PromoteCliCase):
    def test_an_unknown_result_exits_3_and_a_rerun_finishes(self):
        ref = self.add_todo("Ship it", "Todo", "- [ ] First")
        self.fake.mutation_failures = [None, "http_500"]
        error = self.promote(ref, "--team", "ENG", expect=3)["error"]
        self.assertEqual(error["code"], "linear_unknown")
        self.assertEqual(error["issue_id"], self.inputs("IssueCreate")[1]["id"])
        self.assertIn(f"promote {ref}", error["message"])
        self.assertNotIn("→ ENG", self.board_text())
        data = self.promote(ref)
        self.assertEqual((data["card"]["ref"], data["sub_issues"]), ("ENG-7", ["ENG-8"]))
        self.assertEqual([i["title"] for i in self.fixtures["issues"][6:]], ["Ship it", "First"])

    def test_a_changed_todo_exits_2_and_a_rerun_creates_nothing(self):
        ref = self.add_todo("Ship it", "Todo", "- [ ] First")
        real = Store.finish_promotion

        def edited(store, snapshot, identifier):
            self.path.write_text(self.board_text().replace("First", "First thing"))
            return real(store, snapshot, identifier)

        with mock.patch.object(Store, "finish_promotion", edited):
            error = self.promote(ref, "--team", "ENG", expect=2)["error"]
        self.assertEqual(error["code"], "conflict")
        self.assertIn("ENG-7", error["message"])
        out, _ = self.run_cli("promote", ref, directory=self.repo)
        self.assertEqual(out, f"ENG-7 → Todo (Linear)\nsub-issues: ENG-8\ntodo {ref} → Done (acme-api)\n")
        self.assertEqual(len(self.requests("IssueCreate")), 2)
        self.assertIn(f"→ ENG-7 ^{ref}", self.board_text())

    def test_a_rejected_write_exits_3(self):
        ref = self.add_todo()
        self.fake.mutation_failure = "graphql_error"
        error = self.promote(ref, "--team", "ENG", expect=3)["error"]
        self.assertEqual(error["code"], "linear_rejected")
        self.assertEqual(len(self.requests("IssueCreate")), 1)


class CacheTests(PromoteCliCase):
    def test_a_promotion_drops_the_cache(self):
        ref = self.add_todo()
        self.listing("--view", "mine")
        count = len(self.requests("MyIssues"))
        self.promote(ref, "--team", "ENG")
        data = self.listing("--view", "mine")
        self.assertEqual(len(self.requests("MyIssues")), count + 1)
        self.assertIn("ENG-7", self.refs(data))
        self.assertFalse(data["linear"]["stale"])


if __name__ == "__main__":
    unittest.main()


class WarningTests(PromoteCliCase):
    def test_a_rerun_with_other_options_warns_on_stderr_and_in_json(self):
        ref = self.add_todo("Ship it", "Todo", "- [ ] First")
        self.fake.mutation_failures = [None, "http_500"]
        self.promote(ref, "--team", "ENG", expect=3)
        out, err = self.run_cli("--json", "promote", ref, "--team", "OPS", directory=self.repo)
        self.assertRegex(err, r"^herdboard: warning: .*OPS.*ENG")
        warnings = json.loads(out)["warnings"]
        self.assertEqual(len(warnings), 1)
        self.assertIn("OPS", warnings[0])
        self.assertEqual({c["teamId"] for c in self.inputs("IssueCreate")}, {"t-eng"})

    def test_a_first_run_has_an_empty_warnings_list(self):
        ref = self.add_todo()
        self.assertEqual(self.promote(ref, "--team", "ENG")["warnings"], [])
