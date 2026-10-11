import json
import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard.identity import RepoIdentity  # noqa: E402
from herdboard.linear_links import LabelLink, ProjectLink  # noqa: E402
from herdboard.store import Store  # noqa: E402
from linear_data import PR_URL  # noqa: E402
from test_herdboard_cli import CliTestCase  # noqa: E402
from test_herdboard_linear_source import DEAD, LinearEnvTestCase  # noqa: E402


class LinearCliCase(CliTestCase, LinearEnvTestCase):
    def setUp(self):
        CliTestCase.setUp(self)
        cli_root = self.tmp
        LinearEnvTestCase.setUp(self)
        self.cli_tmp = cli_root

    def refs(self, data):
        return [card["ref"] for card in data["cards"]]

    def board(self):
        return Path(self.init_repo_board())

    def link(self, *args, expect=0):
        return self.run_json("link", *args, directory=self.repo, expect=expect)

    def store_for(self):
        from herdboard.config import load_config
        return Store(load_config(self.config))

    def relink(self, *links):
        store = self.store_for()
        store.update_links(store.projects_dir / "acme-api.md", lambda _: list(links))

    def listing(self, *args, directory=None, expect=0):
        return self.run_json("list", *args, directory=directory or self.repo, expect=expect)


class ViewTests(LinearCliCase):
    def setUp(self):
        super().setUp()
        self.board()
        self.run_cli("add", "local todo", "--column", "Todo", directory=self.repo)

    def test_repo_view_shows_todos_and_every_issue_of_linked_projects(self):
        self.link("Platform")
        data = self.listing()
        # ENG-3 belongs to another assignee: the repo view has no assignee filter.
        self.assertEqual(self.refs(data)[1:], ["ENG-1", "ENG-2", "ENG-3"])
        self.assertEqual(data["cards"][0]["source"], "todo")
        self.assertEqual(set(data["linear"]), {"fetched_at", "stale"})
        self.assertFalse(data["linear"]["stale"])
        self.assertNotIn("errors", data)

    def test_canceled_issues_are_hidden(self):
        self.link("Platform")
        self.assertNotIn("ENG-4", self.refs(self.listing()))

    def test_label_filter_needs_any_one_of_the_labels(self):
        self.link("Platform", "--label", "repo:web")
        self.assertEqual(self.refs(self.listing())[1:], ["ENG-3"])
        self.link("Platform", "--label", "repo:web", "--label", "repo:api")
        self.assertEqual(self.refs(self.listing())[1:], ["ENG-1", "ENG-2", "ENG-3"])

    def test_issue_shown_once_across_queries(self):
        self.relink(ProjectLink("p-plat", "Platform", [LabelLink("l-api", "repo:api")]),
                    ProjectLink("p-plat", "Platform"))
        refs = self.refs(self.listing())
        self.assertEqual(sorted(refs[1:]), ["ENG-1", "ENG-2", "ENG-3"])

    def test_inaccessible_project_is_an_error_entry_not_an_empty_view(self):
        self.relink(ProjectLink("gone", "Old project"), ProjectLink("p-mob", "Mobile"))
        data = self.listing()
        self.assertEqual(self.refs(data)[1:], ["ENG-5"])
        self.assertEqual(len(data["errors"]), 1)
        self.assertIn("Old project", data["errors"][0])
        out, err = self.run_cli("list", directory=self.repo)
        self.assertIn("ENG-5", out)
        self.assertRegex(err, r"warning: Linear project 'Old project'")

    def test_only_inaccessible_project_still_reports_it(self):
        self.relink(ProjectLink("gone", "Old project"))
        data = self.listing()
        self.assertEqual(self.refs(data), self.refs(data)[:1])
        self.assertNotIn("linear", data)
        self.assertEqual(len(data["errors"]), 1)

    def test_state_names_override_columns(self):
        self.config.write_text(self.config.read_text() + '[linear.state_names]\n"In Review" = "Todo"\n')
        self.link("Platform")
        columns = {c["ref"]: c["column"] for c in self.listing()["cards"]}
        self.assertEqual(columns["ENG-2"], "Todo")
        self.assertEqual(columns["ENG-1"], "Todo")

    def test_default_state_columns(self):
        self.link("Mobile")
        columns = {c["ref"]: c["column"] for c in self.listing()["cards"]}
        self.assertEqual(columns["ENG-5"], "In progress")
        self.assertEqual({c["ref"]: c["column"] for c in self.listing("--view", "mine")["cards"]}["ENG-6"], "Done")

    def test_mine_view(self):
        data = self.listing("--view", "mine")
        self.assertEqual(self.refs(data), ["ENG-1", "ENG-2", "ENG-5", "ENG-6"])
        self.assertEqual({c["source"] for c in data["cards"]}, {"linear"})
        self.assertEqual(data["cards"][3]["board"], "Linear")

    def test_all_view_has_every_board_and_your_issues(self):
        self.run_cli("add", "in inbox", directory=self.plain)
        self.link("Platform")
        data = self.listing("--view", "all")
        self.assertEqual(self.refs(data)[2:], ["ENG-1", "ENG-2", "ENG-5", "ENG-6"])
        self.assertEqual({c["title"] for c in data["cards"] if c["source"] == "todo"},
                         {"local todo", "in inbox"})

    def test_global_view_has_no_linear(self):
        self.run_cli("add", "in inbox", directory=self.plain)
        count = len(self.fake.requests)
        data = self.listing("--view", "global")
        self.assertEqual(self.refs(data)[0][0], "t")
        self.assertEqual(len(data["cards"]), 1)
        self.assertNotIn("linear", data)
        self.assertEqual(len(self.fake.requests), count)

    def test_project_view_has_issues_and_todos_of_every_linking_repo(self):
        self.link("Platform")
        store = self.store_for()
        web = store.init_board(RepoIdentity("github.com/acme/web", "github.com", ("acme", "web")))
        store.add(web, "web todo", "Todo")
        store.update_links(web, lambda _: [ProjectLink("p-plat", "Platform")])
        other = store.init_board(RepoIdentity("github.com/acme/other", "github.com", ("acme", "other")))
        store.add(other, "other todo", "Todo")
        data = self.listing("--project", "platform", directory=self.plain)
        self.assertEqual(sorted(c["title"] for c in data["cards"] if c["source"] == "todo"),
                         ["local todo", "web todo"])
        self.assertEqual(self.refs(data)[2:], ["ENG-1", "ENG-2", "ENG-3"])
        same = self.listing("--view", "project", "--project", "Platform", directory=self.plain)
        self.assertEqual(self.refs(same), self.refs(data))

    def test_project_view_for_unlinked_project_resolves_by_name(self):
        data = self.listing("--project", "Mobile", directory=self.plain)
        self.assertEqual(self.refs(data), ["ENG-5"])

    def test_project_view_needs_a_project_and_rejects_other_views(self):
        for args in (["--view", "project"], ["--view", "repo", "--project", "Platform"]):
            with self.subTest(args=args):
                self.assertEqual(self.listing(*args, expect=1)["error"]["code"], "usage")
        self.assertEqual(self.listing("--project", "Nope", expect=1)["error"]["code"], "error")

    def test_linear_off_shows_todos_only_and_blocks_mine_and_project(self):
        self.link("Platform")
        del os.environ["LINEAR_API_KEY"]
        count = len(self.fake.requests)
        data = self.listing()
        self.assertEqual(self.refs(data)[0][0], "t")
        self.assertEqual(len(data["cards"]), 1)
        self.assertNotIn("linear", data)
        for args in (["--view", "mine"], ["--project", "Platform"]):
            error = self.listing(*args, expect=1)["error"]
            self.assertEqual(error["code"], "linear_disabled")
            self.assertIn("LINEAR_API_KEY", error["message"])
        self.assertEqual(len(self.fake.requests), count)

    def test_source_filter(self):
        self.link("Platform")
        self.assertEqual(self.refs(self.listing("--source", "linear")), ["ENG-1", "ENG-2", "ENG-3"])
        count = len(self.fake.requests)
        todos = self.listing("--source", "todo")
        self.assertEqual(len(todos["cards"]), 1)
        self.assertNotIn("linear", todos)
        self.assertEqual(len(self.fake.requests), count)

    def test_offline_uses_cache_and_marks_it_stale(self):
        self.link("Platform")
        online = self.listing()
        count = len(self.fake.requests)
        offline = self.listing("--offline")
        self.assertEqual(len(self.fake.requests), count)
        self.assertEqual(self.refs(offline), self.refs(online))
        self.assertTrue(offline["linear"]["stale"])
        self.assertEqual(offline["linear"]["fetched_at"], online["linear"]["fetched_at"])
        _, err = self.run_cli("list", "--offline", directory=self.repo)
        self.assertIn("stale", err)

    def test_offline_without_cache_is_a_linear_error(self):
        self.link("Platform")
        error = self.listing("--offline", "--view", "mine", expect=3)["error"]
        self.assertEqual(error["code"], "linear")

    def test_network_failure_falls_back_to_cache(self):
        self.link("Platform")
        online = self.listing()
        self.endpoint = DEAD
        data = self.listing()
        self.assertEqual(self.refs(data), self.refs(online))
        self.assertTrue(data["linear"]["stale"])

    def test_authentication_error_exits_3_with_json_error(self):
        self.link("Platform")
        self.listing()
        os.environ["LINEAR_API_KEY"] = "wrong-key"
        out, _ = self.run_cli("--json", "list", directory=self.repo, expect=3)
        error = json.loads(out)["error"]
        self.assertEqual(error["code"], "linear")
        self.assertIn("uthentication", error["message"])
        _, err = self.run_cli("list", directory=self.repo, expect=3)
        self.assertRegex(err, r"^herdboard: .*uthentication")


class ReviewFixTests(LinearCliCase):
    AUTH_FAILED = (200, {"errors": [{"message": "Authentication required",
                                     "extensions": {"code": "AUTHENTICATION_ERROR"}}]}, {})

    def setUp(self):
        super().setUp()
        self.board()
        self.run_cli("add", "local todo", "--column", "Todo", directory=self.repo)
        self.link("Platform")

    def test_authentication_failure_after_a_fetch_shows_no_cached_cards(self):
        self.assertIn("ENG-1", self.refs(self.listing()))
        self.fake.responses.append(self.AUTH_FAILED)
        out, err = self.run_cli("--json", "list", directory=self.repo, expect=3)
        self.assertNotIn("ENG-", out)
        self.assertEqual(json.loads(out)["error"]["code"], "linear")
        self.assertNotIn("stale", err)

    def test_project_name_is_resolved_once_per_command(self):
        before = len(self.requests("Projects"))
        data = self.listing("--project", "Mobile", directory=self.plain)
        self.assertEqual(self.refs(data), ["ENG-5"])
        self.assertEqual(len(self.requests("Projects")) - before, 1)

    def test_missing_label_is_a_link_error_and_valid_labels_still_filter(self):
        self.relink(ProjectLink("p-plat", "Platform", [LabelLink("l-web", "repo:web"),
                                                        LabelLink("gone", "Deleted")]))
        data = self.listing()
        self.assertEqual(self.refs(data)[1:], ["ENG-3"])
        self.assertEqual(len(data["errors"]), 1)
        self.assertIn("Platform", data["errors"][0])
        self.assertIn("Deleted", data["errors"][0])
        _, err = self.run_cli("list", directory=self.repo)
        self.assertRegex(err, r"warning: Linear project 'Platform': .*Deleted")

    def test_only_missing_labels_give_no_issues_and_an_error(self):
        self.relink(ProjectLink("p-plat", "Platform", [LabelLink("gone", "Deleted")]))
        data = self.listing()
        self.assertEqual(self.refs(data), self.refs(data)[:1])
        self.assertEqual(len(data["errors"]), 1)
        self.assertEqual(self.requests("ProjectIssues"), [])

    def test_done_days_from_config_reaches_the_request(self):
        self.config.write_text(self.config.read_text() + "[linear]\ndone_days = 5\n")
        self.listing()
        self.listing("--view", "mine")
        for operation in ("ProjectIssues", "MyIssues"):
            sent = self.requests(operation)[-1]["variables"]["filter"]
            self.assertEqual(sent["or"][1], {"completedAt": {"gte": "-P5D"}})

    def test_show_looks_the_issue_up_directly(self):
        data = self.run_json("show", "eng-3", directory=self.plain)
        self.assertEqual(data["card"]["ref"], "ENG-3")
        self.assertFalse(data["linear"]["stale"])
        self.assertEqual(len(self.requests("Issue")), 1)
        self.assertEqual(self.requests("ProjectIssues"), [])
        self.assertEqual(self.requests("MyIssues"), [])

    def test_show_unknown_issue_is_not_found(self):
        self.assertEqual(self.run_json("show", "ENG-99", directory=self.plain, expect=1)["error"]["code"],
                         "not_found")

    def test_show_uses_any_cached_view_when_the_network_is_down(self):
        self.listing()  # caches the Platform project, not "mine"
        self.endpoint = DEAD
        data = self.run_json("show", "ENG-3", directory=self.plain)
        self.assertEqual(data["card"]["ref"], "ENG-3")
        self.assertTrue(data["linear"]["stale"])
        error = self.run_json("show", "ENG-99", directory=self.plain, expect=3)["error"]
        self.assertEqual(error["code"], "linear")

    def test_show_offline_reads_only_caches(self):
        self.listing()
        count = len(self.fake.requests)
        data = self.run_json("show", "ENG-3", "--offline", directory=self.plain)
        self.assertTrue(data["linear"]["stale"])
        self.assertEqual(len(self.fake.requests), count)
        self.assertEqual(self.run_json("show", "ENG-99", "--offline", directory=self.plain,
                                       expect=3)["error"]["code"], "linear")


class TextTests(LinearCliCase):
    def setUp(self):
        super().setUp()
        self.board()
        self.link("Platform")

    def test_list_text_lines(self):
        out, _ = self.run_cli("list", "--source", "linear", directory=self.repo)
        lines = out.splitlines()
        self.assertEqual(lines[0], "## Todo")
        self.assertIn("  ENG-1  Issue 1  PR", lines)
        self.assertIn("## Review", lines)
        self.assertIn("  ENG-2  Issue 2  [In Review]", lines)
        self.assertIn("  ENG-3  Issue 3", lines)

    def test_list_json_fields(self):
        card = self.listing("--source", "linear")["cards"][0]
        self.assertEqual(
            {k: card[k] for k in ("ref", "source", "state", "team", "pr", "read_only", "stale", "path",
                                  "url", "board", "column", "due", "tags", "body", "checked")},
            {"ref": "ENG-1", "source": "linear", "state": "Todo", "team": "ENG", "pr": True,
             "read_only": True, "stale": False, "path": None,
             "url": "https://linear.app/workspace/issue/ENG-1", "board": "Platform", "column": "Todo",
             "due": "2026-10-20", "tags": ["repo:api"], "body": ["Line one", "Line two"],
             "checked": False})
        self.assertIsNotNone(card["fetched_at"])

    def test_pr_marker_needs_a_pull_request_of_this_repository(self):
        cards = {c["ref"]: c["pr"] for c in self.listing()["cards"]}
        self.assertEqual(cards, {"ENG-1": True, "ENG-2": False, "ENG-3": False})
        self.fixtures["issues"][1]["attachments"]["nodes"] = [
            {"url": "https://github.com/acme/other/pull/1"}, {"url": PR_URL.replace("API", "api")}]
        self.assertTrue({c["ref"]: c["pr"] for c in self.listing()["cards"]}["ENG-2"])
        self.fixtures["issues"][1]["attachments"]["nodes"] = [{"url": "https://github.com/acme/other/pull/1"}]
        self.assertFalse({c["ref"]: c["pr"] for c in self.listing()["cards"]}["ENG-2"])

    def test_show_text(self):
        out, _ = self.run_cli("show", "ENG-2", directory=self.repo)
        self.assertEqual(out.splitlines(), [
            "Issue 2", "Platform / Review", "ref: ENG-2", "state: In Review", "team: ENG",
            "url: https://linear.app/workspace/issue/ENG-2", "labels: repo:api, bug", "due: 2026-10-20",
            "Line one", "Line two"])

    def test_show_json_and_other_places(self):
        data = self.run_json("show", "ENG-1", directory=self.repo)
        self.assertEqual((data["card"]["ref"], data["card"]["pr"]), ("ENG-1", True))
        self.assertFalse(data["linear"]["stale"])
        # Not in the repository's projects: found in your own issues.
        self.assertEqual(self.run_json("show", "eng-5", directory=self.plain)["card"]["ref"], "ENG-5")
        # Found in a linked project although it is not yours and not in this repository's view.
        self.assertEqual(self.run_json("show", "ENG-3", directory=self.plain)["card"]["ref"], "ENG-3")

    def test_show_unknown_issue(self):
        error = self.run_json("show", "ENG-99", directory=self.repo, expect=1)["error"]
        self.assertEqual(error["code"], "not_found")

    def test_show_with_linear_off(self):
        del os.environ["LINEAR_API_KEY"]
        self.assertEqual(self.run_json("show", "ENG-1", directory=self.repo, expect=1)["error"]["code"],
                         "linear_disabled")

    def test_move_and_done_on_linear_refs_are_refused(self):
        count = len(self.fake.requests)
        for args in (["move", "ENG-1", "Done"], ["done", "ENG-1"]):
            data = self.run_json(*args, directory=self.repo, expect=1)
            self.assertEqual(data["error"], {"code": "not_implemented",
                                             "message": "Linear writes arrive in a later step"})
            _, err = self.run_cli(*args, directory=self.repo, expect=1)
            self.assertEqual(err, "herdboard: Linear writes arrive in a later step\n")
        self.assertEqual(len(self.fake.requests), count)

    def test_move_of_todo_still_works(self):
        card = self.run_json("add", "todo", directory=self.repo)["card"]
        self.assertEqual(self.run_json("move", card["ref"], "Todo", directory=self.repo)["card"]["column"], "Todo")


class LinkTests(LinearCliCase):
    def links(self):
        return self.store_for().board_meta(self.store_for().projects_dir / "acme-api.md").links

    def test_requires_repository_and_board(self):
        error = self.link("Platform", expect=1)["error"]
        self.assertIn("herdboard init", error["message"])
        out = self.run_json("link", "Platform", directory=self.plain, expect=1)["error"]
        self.assertIn("Not in a Git repository", out["message"])

    def test_add_replace_and_remove(self):
        board = self.board()
        before = board.read_text()
        data = self.link("platform")
        self.assertEqual(data["links"], [{"id": "p-plat", "name": "Platform"}])
        self.assertEqual(self.links(), [ProjectLink("p-plat", "Platform")])
        self.link("Mobile")
        self.link("Platform", "--label", "repo:api")
        self.assertEqual(self.links(), [
            ProjectLink("p-plat", "Platform", [LabelLink("l-api", "repo:api")]),
            ProjectLink("p-mob", "Mobile")])
        out, _ = self.run_cli("link", "Platform", "--remove", directory=self.repo)
        self.assertIn("Mobile", out)
        self.assertEqual(self.links(), [ProjectLink("p-mob", "Mobile")])
        self.link("mobile", "--remove")
        self.assertEqual(board.read_text(), before)

    def test_remove_unknown_link(self):
        self.board()
        self.assertEqual(self.link("Platform", "--remove", expect=1)["error"]["code"], "not_found")

    def test_ambiguous_and_unknown_project(self):
        self.board()
        error = self.link("twin", expect=1)["error"]
        self.assertIn("Twin", error["message"])
        self.assertIn("p-dup1", error["message"])
        self.assertIn("p-dup2", error["message"])
        self.assertIn("ENG", error["message"])
        self.assertIn("OPS", error["message"])
        self.assertIn("No Linear project", self.link("Nope", expect=1)["error"]["message"])
        self.assertEqual(self.links(), [])

    def test_ambiguous_label_lists_teams_and_team_prefix_picks_one(self):
        self.board()
        error = self.link("Platform", "--label", "bug", expect=1)["error"]
        self.assertIn("ENG", error["message"])
        self.assertIn("OPS", error["message"])
        self.assertEqual(self.links(), [])
        self.link("Platform", "--label", "OPS/bug", "--label", "repo:web")
        self.assertEqual(self.links(), [ProjectLink("p-plat", "Platform", [
            LabelLink("l-bug-ops", "bug"), LabelLink("l-web", "repo:web")])])

    def test_unknown_label(self):
        self.board()
        self.assertIn("No Linear label", self.link("Platform", "--label", "nope", expect=1)["error"]["message"])
        self.assertIn("No Linear label", self.link("Platform", "--label", "ENG/nope", expect=1)["error"]["message"])
        self.assertEqual(self.links(), [])

    def test_linear_off(self):
        self.board()
        del os.environ["LINEAR_API_KEY"]
        self.assertEqual(self.link("Platform", expect=1)["error"]["code"], "linear_disabled")

    def test_invalid_frontmatter_board_is_read_only(self):
        board = self.board()
        board.write_text(board.read_text().replace("repo:", "bad: [x\nrepo:", 1))
        error = self.link("Platform", expect=1)["error"]
        self.assertEqual(error["code"], "format")
        self.assertIn("invalid YAML", error["message"])

    def test_text_output(self):
        self.board()
        out, _ = self.run_cli("link", "Platform", directory=self.repo)
        self.assertRegex(out, r"acme-api.md: linked projects: Platform\n$")


if __name__ == "__main__":
    unittest.main()
