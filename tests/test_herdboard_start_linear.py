"""Linear start uses the server branch and the normal write path."""
import json
from unittest.mock import patch
from pathlib import Path
import subprocess
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_herdboard_linear_write_cli import WriteCliCase
from test_herdboard_start import FakeHerdr
from test_herdboard_cli import git
from herdboard import start
from herdboard.linear_pins import IssuePin


class StartLinearTests(WriteCliCase):
    def setUp(self):
        super().setUp()
        self.board()
        from herdboard.linear_links import ProjectLink, LabelLink
        self.relink(ProjectLink("p-plat", "Platform", [LabelLink("l-api", "repo:api")]))
        # A worktree starts from a commit, so the repository needs one.
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "--allow-empty", "-qm", "init")
        self.fake_herdr = FakeHerdr(self.repo.parent)
        self.enterContext(patch.object(start, "command", self.fake_herdr))

    def test_branch_and_move_and_resume(self):
        result = self.run_json("start", "ENG-1", "--no-agent", directory=self.repo)
        self.assertEqual(result["branch"], "nate/eng-1")
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Doing")
        self.assertEqual(self.inputs("IssueUpdate"), [{"stateId": "ws-ENG-Doing"}])
        result2 = self.run_json("start", "ENG-1", "--no-agent", directory=self.repo)
        self.assertEqual(result["worktree_path"], result2["worktree_path"])
        self.assertEqual(len(self.inputs("IssueUpdate")), 1)

    def test_the_move_gets_the_state_that_prepare_resolved(self):
        from herdboard.linear_writes import LinearWriter
        with patch.object(LinearWriter, "move", return_value=None) as move:
            self.run_json("start", "ENG-1", "--no-agent", directory=self.repo)
        self.assertEqual(move.call_args.kwargs.get("state_name"), "Doing")

    def test_a_sub_issue_starts_from_the_current_branch_without_its_parent(self):
        self.issue("ENG-1")["parent"] = {"id": "i2", "identifier": "ENG-2"}
        result = self.run_json("start", "ENG-1", "--no-agent", directory=self.repo)
        head = subprocess.run(["git", "-C", str(self.repo), "symbolic-ref", "--short", "HEAD"],
                              check=True, capture_output=True, text=True).stdout.strip()
        self.assertEqual(result["base"], head)
        self.assertIsNone(start.find_record("ENG-2"))

    def test_explicit_board_or_checkout_repository(self):
        for repo in ("acme-api", str(self.repo)):
            with self.subTest(repo=repo):
                result = self.run_json("start", "ENG-1", "--repo", repo, "--no-agent", directory=self.plain)
                self.assertEqual(result["branch"], "nate/eng-1")

    def test_no_repository_outside_repo_view(self):
        error = self.run_json("start", "ENG-1", "--no-agent", directory=self.plain, expect=1)["error"]
        self.assertEqual(error["code"], "repo_required")

    def test_non_repository_board_view_does_not_infer_checkout_repository(self):
        from herdboard.config import load_config
        from herdboard.store import Store
        from herdboard.linear_source import LinearSource
        from herdboard.linear_writes import LinearWriter
        config = load_config(self.config)
        starter = start.Starter(Store(config), self.repo,
                                writer=LinearWriter(config, LinearSource(config)),
                                repository_view=False)
        with self.assertRaises(start.StartError) as raised:
            starter.start("ENG-1", no_agent=True)
        self.assertEqual(raised.exception.code, "repo_required")

    def test_invalid_server_branch_is_refused(self):
        self.issue("ENG-1")["branchName"] = "bad..branch"
        error = self.run_json("start", "ENG-1", "--no-agent", directory=self.repo, expect=1)["error"]
        self.assertEqual(error["code"], "invalid_branch")
        self.assertEqual(self.inputs("IssueUpdate"), [])
        self.issue("ENG-1")["branchName"] = "nate/eng-1"
        result = self.run_json("start", "ENG-1", "--no-agent", directory=self.repo)
        self.assertEqual(result["branch"], "nate/eng-1")

    def test_already_started_does_not_write(self):
        self.run_json("start", "ENG-5", "--repo", "acme-api", "--no-agent", directory=self.repo)
        self.assertEqual(self.inputs("IssueUpdate"), [])

    def test_wrong_project_or_label_requires_repository(self):
        from herdboard.linear_links import ProjectLink, LabelLink
        for issue in ("ENG-3", "ENG-5"):
            error = self.run_json("start", issue, "--no-agent", directory=self.repo, expect=1)["error"]
            self.assertEqual(error["code"], "repo_required")
        self.relink(ProjectLink("p-plat", "Platform", [LabelLink("missing", "Missing")]))
        error = self.run_json("start", "ENG-1", "--no-agent", directory=self.repo, expect=1)["error"]
        self.assertEqual(error["code"], "repo_required")

    def test_wrong_repository_lists_all_matching_boards(self):
        from herdboard.config import load_config
        from herdboard.store import Store
        from herdboard.identity import RepoIdentity
        from herdboard.linear_links import ProjectLink
        store = Store(load_config(self.config))
        for name in ("web", "frontend"):
            path = store.init_board(RepoIdentity("github.com/acme/" + name, "github.com", ("acme", name)))
            store.update_links(path, lambda _: [ProjectLink("p-plat", "Platform")])
        self.relink(ProjectLink("p-mob", "Mobile"))
        error = self.run_json("start", "ENG-1", "--no-agent", directory=self.repo, expect=1)["error"]
        self.assertEqual(error["code"], "repo_required")
        self.assertIn("github.com/acme/web", error["message"])
        self.assertIn("github.com/acme/frontend", error["message"])

    def pins(self):
        store = self.store_for()
        return [pin.identifier for pin in store.board_meta(store.projects_dir / "acme-api.md").pins]

    def test_start_pins_the_issue_to_the_repository_board(self):
        self.run_json("start", "ENG-3", "--repo", "acme-api", "--no-agent", directory=self.plain)
        self.assertEqual(self.pins(), ["ENG-3"])
        self.run_json("start", "ENG-3", "--repo", "acme-api", "--no-agent", directory=self.plain)
        self.assertEqual(self.pins(), ["ENG-3"])

    def test_no_pin_when_a_linked_project_shows_the_issue(self):
        self.run_json("start", "ENG-1", "--no-agent", directory=self.repo)
        self.assertEqual(self.pins(), [])

    def test_a_pinned_issue_starts_in_its_board_repository(self):
        self.repin(IssuePin("i5", "ENG-5"))
        result = self.run_json("start", "ENG-5", "--no-agent", directory=self.repo)
        self.assertEqual(result["branch"], "nate/eng-5")

    def test_no_board_means_no_pin_and_no_error(self):
        other = self.repo.parent / "web"
        other.mkdir()
        git(other, "init", "-q")
        git(other, "remote", "add", "origin", "https://github.com/acme/web.git")
        git(other, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "--allow-empty", "-qm", "init")
        _, err = self.run_cli("--json", "start", "ENG-5", "--repo", str(other), "--no-agent",
                              directory=self.plain)
        self.assertEqual(err, "")
        self.assertEqual(self.pins(), [])

    def mixed_case_board(self):
        board = self.store_for().projects_dir / "acme-api.md"
        board.write_text(board.read_text().replace("repo: github.com/acme/api", "repo: github.com/Acme/API", 1))

    def test_a_checkout_start_pins_to_a_mixed_case_github_board(self):
        self.mixed_case_board()
        _, err = self.run_cli("--json", "start", "ENG-3", "--repo", str(self.repo), "--no-agent",
                              directory=self.plain)
        self.assertEqual(err, "")
        self.assertEqual(self.pins(), ["ENG-3"])

    def test_a_pinned_issue_of_a_mixed_case_github_board_starts_in_the_repo_view(self):
        self.mixed_case_board()
        self.repin(IssuePin("i5", "ENG-5"))
        result = self.run_json("start", "ENG-5", "--no-agent", directory=self.repo)
        self.assertEqual(result["branch"], "nate/eng-5")

    def test_a_pin_failure_warns_and_the_start_goes_on(self):
        board = self.store_for().projects_dir / "acme-api.md"
        board.write_text(board.read_text().replace("repo:", "linear-issues: nope\nrepo:", 1))
        out, err = self.run_cli("--json", "start", "ENG-3", "--repo", "acme-api", "--no-agent",
                                directory=self.plain)
        self.assertEqual(json.loads(out)["branch"], "nate/eng-3")
        self.assertRegex(err, r"herdboard: warning: cannot pin ENG-3 to .*acme-api.md: .*linear-issues")

    def test_missing_state_precedes_worktree(self):
        self.config.write_text(self.config.read_text() + '\nstart_column = "Missing"\n')
        # Keep the override at the top level, outside existing TOML tables.
        self.config.write_text('start_column = "Missing"\n' + self.config.read_text().replace('\nstart_column = "Missing"\n', '\n'))
        error = self.run_json("start", "ENG-1", "--no-agent", directory=self.repo, expect=1)["error"]
        self.assertEqual(error["code"], "no_state")
        self.assertFalse(any(c[:2] == ["worktree", "create"] for c in self.fake_herdr.calls))

    def test_linear_move_failure_preserves_worktree(self):
        self.fake.mutation_failure = "http_429"
        error = self.run_json("start", "ENG-1", "--no-agent", directory=self.repo, expect=3)["error"]
        self.assertIn("worktree", error["steps_done"])
        self.fake.mutation_failure = None
        self.run_json("start", "ENG-1", "--no-agent", directory=self.repo)
        self.assertEqual(sum(c[:2] == ["worktree", "create"] for c in self.fake_herdr.calls), 1)
