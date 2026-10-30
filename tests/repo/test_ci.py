import unittest

import yaml

from support.paths import ROOT

WORKFLOW = ROOT / ".github/workflows/ci.yml"


class CiTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def test_runs_on_main_pushes_and_all_pull_requests(self):
        self.assertEqual(self.workflow["on"], {
            "push": {"branches": ["main"]},
            "pull_request": None,
        })

    def test_uses_read_only_repository_permissions(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        checkout = self.workflow["jobs"]["test"]["steps"][0]
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertFalse(checkout["with"]["persist-credentials"])

    def test_runs_the_full_suite_with_locked_dependencies_on_latest_python(self):
        job = self.workflow["jobs"]["test"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        steps = job["steps"]
        setup_python = next(step for step in steps
                            if step.get("uses", "").startswith("actions/setup-python@"))
        self.assertEqual(setup_python["with"]["python-version"], "3.x")
        self.assertTrue(any(step.get("uses", "").startswith("astral-sh/setup-uv@")
                            for step in steps))
        self.assertIn("uv run --locked python -m unittest discover -s tests",
                      [step.get("run") for step in steps])

    def test_checks_the_herdboard_lock_that_the_root_run_never_resolves(self):
        commands = [step.get("run") for step in self.workflow["jobs"]["test"]["steps"]]
        self.assertIn("uv lock --check --project plugins/herdboard", commands)
        self.assertIn(
            "uv run --locked --project plugins/herdboard python -c"
            ' "import textual, yaml, tomlkit, herdboard.board, herdboard.cli"',
            commands)


if __name__ == "__main__":
    unittest.main()
