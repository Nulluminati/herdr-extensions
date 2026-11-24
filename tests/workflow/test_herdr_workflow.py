import io
import json
import os
import subprocess
import tempfile
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

from support.paths import PLUGINS
from support.scripts import load_script

herdr_workflow = load_script("workflow", "herdr_workflow")

TEMPLATE = """name = "Implementation"
working_dir = "~"

[[tabs]]
label = "agent"
"""


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        # herdr-plus's config directory: the templates and prompts live there, not next to the script.
        self.config = Path(temporary.name)
        (self.config / "projects").mkdir()
        (self.config / "projects" / "implementation.toml").write_text(TEMPLATE, encoding="utf-8")
        (self.config / "prompts").mkdir()
        (self.config / "prompts" / "review.md").write_text("Review.", encoding="utf-8")
        (self.config / "prompts" / "local-review.md").write_text("Local.", encoding="utf-8")
        self.environment = patch.dict(os.environ, {"HERDR_ENV": "1"})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("HERDR_BIN_PATH", None)
        # The launcher runs from other plugins' actions too, so this must not decide the config directory.
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(self.config / "elsewhere")
        self.workspaces = []
        self.calls = []
        self.timeouts = {}
        self.failing_workspaces = {}
        replacement = patch.object(herdr_workflow, "command", self.fake_command)
        replacement.start()
        self.addCleanup(replacement.stop)

    def fake_command(self, argv, env=None, timeout=None):
        self.calls.append(argv)
        self.timeouts[tuple(argv)] = timeout
        if argv[1:] == ["plugin", "config-dir", "cloudmanic.herdr-plus"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.config}\n", "")
        if argv[1:3] == ["workspace", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"workspaces": self.workspaces}}), "")
        if argv[1:3] == ["workspace", "report-metadata"]:
            if argv[3] in self.failing_workspaces:
                raise self.failing_workspaces[argv[3]]
            return subprocess.CompletedProcess(argv, 0, "{}", "")
        raise AssertionError(f"unexpected command {argv}")

    def test_template_comes_from_the_herdr_plus_config_directory(self):
        with patch.object(herdr_workflow.shutil, "which", return_value="/usr/bin/zoxide"):
            self.assertEqual(herdr_workflow.workflow_template("implementation"), TEMPLATE)

    def test_missing_herdr_plus_config_directory_names_the_path(self):
        (self.config / "projects" / "implementation.toml").unlink()
        with self.assertRaisesRegex(herdr_workflow.WorkflowError, str(self.config / "projects")):
            herdr_workflow.workflow_template("implementation")

    def test_review_agent_gets_the_prompts_from_the_herdr_plus_config_directory(self):
        with patch.object(herdr_workflow.os, "execvp") as execvp:
            herdr_workflow.launch_review_agent("local-review", "claude")
        execvp.assert_called_once_with("claude", ["claude", "--append-system-prompt", "Review.\n\nLocal."])

    def test_refresh_sidebar_reports_workflow_workspaces_only(self):
        home = Path.home().resolve()
        self.workspaces = [
            {"workspace_id": "w1", "label": "implementation · ~"},
            {"workspace_id": "w2", "label": "notes"},
            {"workspace_id": "w3", "label": "unknown · /tmp"},
        ]
        self.assertEqual(herdr_workflow.refresh_sidebar(), 1)
        reports = [argv for argv in self.calls if argv[1:3] == ["workspace", "report-metadata"]]
        self.assertEqual(
            reports,
            [
                [
                    "herdr",
                    "workspace",
                    "report-metadata",
                    "w1",
                    "--source",
                    "herdr-workflow",
                    "--token",
                    f"workflow_project={home.name}",
                    "--token",
                    "workflow_profile=implementation",
                ]
            ],
        )

    def test_refresh_sidebar_clears_tokens_from_a_renamed_workspace(self):
        self.workspaces = [
            {
                "workspace_id": "w5",
                "label": "renamed workspace",
                "tokens": {"workflow_profile": "implementation", "workflow_project": "project", "other": "x"},
            },
            {"workspace_id": "w6", "label": "notes", "tokens": {"other": "x"}},
        ]
        self.assertEqual(herdr_workflow.refresh_sidebar(), 1)
        reports = [argv for argv in self.calls if argv[1:3] == ["workspace", "report-metadata"]]
        self.assertEqual(
            reports,
            [
                [
                    "herdr",
                    "workspace",
                    "report-metadata",
                    "w5",
                    "--source",
                    "herdr-workflow",
                    "--clear-token",
                    "workflow_project",
                    "--clear-token",
                    "workflow_profile",
                ]
            ],
        )

    def test_refresh_sidebar_skips_a_workspace_whose_report_fails(self):
        # The workspace list is a snapshot, so a workspace can close before its report.
        self.workspaces = [
            {"workspace_id": "w1", "label": "implementation · ~"},
            {"workspace_id": "w2", "label": "implementation · ~"},
            {"workspace_id": "w3", "label": "renamed workspace", "tokens": {"workflow_profile": "implementation"}},
            {"workspace_id": "w4", "label": "implementation · ~"},
        ]
        self.failing_workspaces = {
            "w2": herdr_workflow.WorkflowError('{"error":{"code":"workspace_not_found"}}'),
            "w3": OSError("socket closed"),
        }
        with patch("sys.stderr", new_callable=io.StringIO) as stderr:
            self.assertEqual(herdr_workflow.refresh_sidebar(), 2)
        reported = [argv[3] for argv in self.calls if argv[1:3] == ["workspace", "report-metadata"]]
        self.assertEqual(reported, ["w1", "w2", "w3", "w4"])
        self.assertEqual(
            stderr.getvalue().splitlines(),
            [
                'herdr-workflow: cannot update the sidebar of workspace w2: {"error":{"code":"workspace_not_found"}}',
                "herdr-workflow: cannot update the sidebar of workspace w3: socket closed",
            ],
        )

    def test_sidebar_reports_use_a_short_timeout(self):
        self.workspaces = [
            {"workspace_id": "w1", "label": "implementation · ~"},
            {"workspace_id": "w2", "label": "renamed workspace", "tokens": {"workflow_project": "project"}},
        ]
        herdr_workflow.refresh_sidebar()
        reports = [argv for argv in self.calls if argv[1:3] == ["workspace", "report-metadata"]]
        self.assertEqual(len(reports), 2)
        for argv in reports:
            self.assertEqual(self.timeouts[tuple(argv)], herdr_workflow.REPORT_TIMEOUT_SECONDS)
        self.assertLess(herdr_workflow.REPORT_TIMEOUT_SECONDS, 120)


class CommandTests(unittest.TestCase):
    def test_timeout_is_passed_to_the_process_and_named_in_the_error(self):
        error = subprocess.TimeoutExpired(["herdr"], 5)
        with patch.object(herdr_workflow.subprocess, "run", side_effect=error) as run:
            with self.assertRaisesRegex(herdr_workflow.WorkflowError, "herdr did not finish within 5 seconds"):
                herdr_workflow.command(["herdr", "workspace", "list"], timeout=5)
        self.assertEqual(run.call_args.kwargs["timeout"], 5)

    def test_default_timeout_is_120_seconds(self):
        with patch.object(herdr_workflow.subprocess, "run") as run:
            herdr_workflow.command(["herdr", "workspace", "list"])
        self.assertEqual(run.call_args.kwargs["timeout"], 120)

    def test_output_that_is_not_utf8_keeps_its_bytes(self):
        result = herdr_workflow.command(["printf", "/tmp/\\377"])
        self.assertEqual(os.fsencode(result.stdout), b"/tmp/\xff")


class NonUtf8RepositoryTests(unittest.TestCase):
    """On Linux a directory name is a byte string, so a repository can live under a name that is not UTF-8."""

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name).resolve()
        self.bad = base / os.fsdecode(b"\xff")
        self.good = base / "good"
        for directory in (self.bad, self.good):
            directory.mkdir()
            subprocess.run(["git", "init", "--quiet", str(directory)], check=True)
        self.escaped = os.fsencode(self.bad).decode("utf-8", "backslashreplace")

    def test_repository_root_names_the_path_that_cannot_be_a_label(self):
        with self.assertRaises(herdr_workflow.WorkflowError) as raised:
            herdr_workflow.repository_root(self.bad)
        self.assertIn(self.escaped, str(raised.exception))
        self.assertIn("UTF-8", str(raised.exception))

    def test_list_repositories_skips_the_repository_and_keeps_the_others(self):
        real_command = herdr_workflow.command

        def fake_command(argv, env=None, timeout=None):
            if argv[0] == "zoxide":
                stdout = os.fsencode(self.bad) + b"\n" + os.fsencode(self.good) + b"\n"
                return subprocess.CompletedProcess(argv, 0, os.fsdecode(stdout), "")
            return real_command(argv, env)

        with (
            patch.object(herdr_workflow, "command", fake_command),
            patch.object(herdr_workflow, "current_directory", return_value=self.bad),
        ):
            self.assertEqual(herdr_workflow.list_repositories(), [self.good])


class PullRequestReferenceTests(unittest.TestCase):
    def test_reference_names_the_repository_and_the_number(self):
        for value in (
            "owner/repo#12",
            " owner/repo#12\n",
            "https://github.com/owner/repo/pull/12",
            "https://github.com/owner/repo/pull/12/",
            "https://github.com/owner/repo/pull/12?diff=split&tab=files",
            "https://github.com/owner/repo/pull/12#issue-1",
        ):
            with self.subTest(value=value):
                self.assertEqual(herdr_workflow.parse_pr_reference(value), ("owner/repo", "12"))

    def test_url_of_a_pull_request_tab_names_the_pull_request(self):
        for value in (
            "https://github.com/owner/repo/pull/12/files",
            "https://github.com/owner/repo/pull/12/commits",
            "https://github.com/owner/repo/pull/12/checks",
            "https://github.com/owner/repo/pull/12/changes",
            "https://github.com/owner/repo/pull/12/files/",
            "https://github.com/owner/repo/pull/12/files?diff=split",
            "https://github.com/owner/repo/pull/12/files#diff-0123abcd",
            "https://github.com/owner/repo/pull/12/commits/0123abcd",
            "https://github.com/owner/repo/pull/12/files/0123abcd..4567ef01",
        ):
            with self.subTest(value=value):
                self.assertEqual(herdr_workflow.parse_pr_reference(value), ("owner/repo", "12"))

    def test_other_text_is_not_a_pull_request_reference(self):
        for value in (
            "",
            "owner/repo",
            "owner/repo#0",
            "owner/..#12",
            "https://github.com/owner/repo/issues/12",
            "https://github.com/owner/repo/pull/12files",
            "https://github.com/owner/repo/pull/123abc",
            "https://github.com/owner/repo/pull/12/files and more",
            "https://github.com/owner/repo/pull/12/files\nowner/repo#13",
            "https://github.example.com/owner/repo/pull/12",
        ):
            with self.subTest(value=value):
                with self.assertRaisesRegex(herdr_workflow.WorkflowError, "pull request URL"):
                    herdr_workflow.parse_pr_reference(value)


class ManifestTests(unittest.TestCase):
    def setUp(self):
        with open(PLUGINS / "workflow" / "herdr-plugin.toml", "rb") as manifest:
            self.manifest = tomllib.load(manifest)

    def test_manifest_id(self):
        self.assertEqual(self.manifest["id"], "nulluminati.workflow")

    def test_startup_and_refresh_action_refresh_the_sidebar(self):
        command = ["python3", "bin/herdr_workflow.py", "refresh-sidebar"]
        self.assertEqual([startup["command"] for startup in self.manifest["startup"]], [command])
        self.assertEqual(
            [(action["id"], action["command"]) for action in self.manifest["actions"]],
            [("refresh", command)],
        )
