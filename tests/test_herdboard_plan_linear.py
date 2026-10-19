"""Plan a Linear issue: the planner needs a Linear connection, and its brief has the issue description."""
import json
from pathlib import Path
import sys
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_herdboard_linear_write_cli import WriteCliCase  # noqa: E402
from test_herdboard_plan import PlanHerdr  # noqa: E402
from test_herdboard_cli import git  # noqa: E402
from herdboard import plan, start  # noqa: E402


class PlanLinearTests(WriteCliCase):
    def setUp(self):
        super().setUp()
        self.board()
        from herdboard.linear_links import LabelLink, ProjectLink
        self.relink(ProjectLink("p-plat", "Platform", [LabelLink("l-api", "repo:api")]))
        git(self.repo, "-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "--allow-empty", "-qm", "init")
        start.register_checkout(self.repo)
        self.fake_herdr = PlanHerdr(Path(self.tmp.name))
        self.enterContext(patch.object(start, "command", self.fake_herdr))
        self.workers = []
        self.enterContext(patch.object(plan, "start_worker",
                                       lambda arguments, log: self.workers.append((arguments, log))))

    def brief(self):
        call = self.fake_herdr.find("agent", "start")[0]
        message = call[call.index("--") + 1]
        return Path(message.split("Read the brief at ")[1].split(" first")[0]).read_text()

    def test_a_linear_issue_is_planned_with_its_description(self):
        result = self.run_json("plan", "ENG-1", directory=self.repo)
        self.assertEqual(result["ref"], "ENG-1")
        self.assertEqual(self.fake_herdr.find("agent", "start")[0][2], "hb-eng-1-plan")
        text = self.brief()
        self.assertIn("Plan Linear issue `ENG-1`", text)
        self.assertIn("## Description\n\nLine one\nLine two", text)
        self.assertIn("/attachments/ENG-1/plan.md", text)
        self.assertEqual(self.workers[0][0][-4:], ["plan-wait", "ENG-1", "--pane", "w1:p7"])
        # Planning writes nothing to Linear.
        self.assertEqual(self.requests("IssueUpdate"), [])

    def test_a_linear_issue_plans_in_a_worktree_keyed_by_its_issue_id(self):
        result = self.run_json("plan", "ENG-1", directory=self.repo)
        records = json.loads((start.state_dir() / "starts.json").read_text())
        self.assertIn("i1", records, "the record key is the issue ID, so a later start finds it")
        record = records["i1"]
        self.assertEqual(record["branch"], "nate/eng-1")
        self.assertEqual(result["branch"], "nate/eng-1")
        self.assertEqual(record["ref"], "ENG-1")
        tab = self.fake_herdr.find("tab", "create")[0]
        self.assertIn("--workspace", tab)
        self.assertEqual(tab[tab.index("--cwd") + 1], record["worktree_path"])

    def test_a_linear_issue_needs_a_linear_connection(self):
        import os
        del os.environ["LINEAR_API_KEY"]
        self.assertEqual(self.error("plan", "ENG-1")["code"], "linear_disabled")
        self.assertEqual(self.fake_herdr.find("tab", "create"), [])

    def test_an_unknown_linear_issue_is_not_found(self):
        self.assertEqual(self.error("plan", "ENG-404")["code"], "not_found")
        self.assertEqual(self.fake_herdr.find("tab", "create"), [])
