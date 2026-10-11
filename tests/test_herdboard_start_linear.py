"""Linear start uses the server branch and the normal write path."""
from unittest.mock import patch
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_herdboard_linear_write_cli import WriteCliCase
from test_herdboard_start import FakeHerdr
from herdboard import start


class StartLinearTests(WriteCliCase):
    def setUp(self):
        super().setUp()
        self.board()
        from herdboard.linear_links import ProjectLink, LabelLink
        self.relink(ProjectLink("p-plat", "Platform", [LabelLink("l-api", "repo:api")]))
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
