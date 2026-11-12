import unittest

import yaml

from support.paths import ROOT

WORKFLOW = ROOT / ".github/workflows/ci.yml"


class CiTests(unittest.TestCase):
    def setUp(self):
        self.workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))

    def test_runs_on_main_pushes_and_all_pull_requests(self):
        self.assertEqual(
            self.workflow["on"],
            {
                "push": {"branches": ["main"]},
                "pull_request": None,
            },
        )

    def test_uses_read_only_repository_permissions(self):
        self.assertEqual(self.workflow["permissions"], {"contents": "read"})
        checkout = self.workflow["jobs"]["test"]["steps"][0]
        self.assertTrue(checkout["uses"].startswith("actions/checkout@"))
        self.assertFalse(checkout["with"]["persist-credentials"])

    def test_runs_the_full_suite_with_locked_dependencies_on_latest_python(self):
        job = self.workflow["jobs"]["test"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        steps = job["steps"]
        setup_python = next(step for step in steps if step.get("uses", "").startswith("actions/setup-python@"))
        self.assertEqual(setup_python["with"]["python-version"], "3.x")
        self.assertTrue(any(step.get("uses", "").startswith("astral-sh/setup-uv@") for step in steps))
        self.assertIn(
            "uv run --locked pytest -n auto --cov=plugins --cov-report=term-missing",
            [step.get("run") for step in steps],
        )

    def test_lints_and_checks_the_format_before_the_tests(self):
        job = self.workflow["jobs"]["test"]
        self.assertEqual(job["timeout-minutes"], 10)
        commands = [step.get("run") for step in job["steps"]]
        tests = commands.index("uv run --locked pytest -n auto --cov=plugins --cov-report=term-missing")
        self.assertLess(commands.index("uv run --locked ruff check ."), tests)
        self.assertLess(commands.index("uv run --locked ruff format --check ."), tests)

    def test_names_each_job_for_the_required_status_checks(self):
        names = {job_id: job.get("name") for job_id, job in self.workflow["jobs"].items()}
        self.assertEqual(names, {"test": "Lint, format, test", "secrets": "Secret scan (gitleaks)"})

    def test_scans_the_full_history_for_secrets_with_a_checksum_checked_gitleaks(self):
        job = self.workflow["jobs"]["secrets"]
        self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(job["timeout-minutes"], 5)
        self.assertEqual(job["env"]["GITLEAKS_VERSION"], "8.30.1")
        self.assertRegex(job["env"]["GITLEAKS_SHA256"], r"^[0-9a-f]{64}$")
        checkout = job["steps"][0]
        self.assertEqual(checkout["uses"], self.workflow["jobs"]["test"]["steps"][0]["uses"])
        self.assertEqual(checkout["with"], {"fetch-depth": 0, "persist-credentials": False})
        install = job["steps"][1]["run"]
        self.assertIn("set -euo pipefail", install)
        self.assertIn('echo "$GITLEAKS_SHA256  $RUNNER_TEMP/$archive" | sha256sum -c -', install)
        self.assertEqual(
            job["steps"][2]["run"],
            '"$RUNNER_TEMP/gitleaks" git --redact --no-banner --log-opts="--all" .',
        )


if __name__ == "__main__":
    unittest.main()
