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
        replacement = patch.object(herdr_workflow, "command", self.fake_command)
        replacement.start()
        self.addCleanup(replacement.stop)

    def fake_command(self, argv, env=None):
        self.calls.append(argv)
        if argv[1:] == ["plugin", "config-dir", "cloudmanic.herdr-plus"]:
            return subprocess.CompletedProcess(argv, 0, f"{self.config}\n", "")
        if argv[1:3] == ["workspace", "list"]:
            return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"workspaces": self.workspaces}}), "")
        if argv[1:3] == ["workspace", "report-metadata"]:
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
