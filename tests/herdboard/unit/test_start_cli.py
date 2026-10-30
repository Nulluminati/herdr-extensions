"""Start configuration and command output."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdboard import cli
from herdboard.config import load_config
from herdboard.model import HerdboardError
from herdboard.start import StartError


class StartCliTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / "config.toml"
        self.config.write_text(f'todos_dir = "{self.root / "todos"}"\n')
        env = patch.dict(os.environ, {"HERDBOARD_CONFIG": str(self.config), "XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
    def test_config_defaults_and_mapping(self):
        config = load_config(self.config)
        self.assertEqual(config.start_agent, "claude")
        self.assertEqual(config.start_column, "In progress")
        self.config.write_text('start_agent = "pi"\n[linear.columns]\nstarted = "Working"\n')
        self.assertEqual(load_config(self.config).start_column, "Working")
        self.assertEqual(load_config(self.config).start_agent, "pi")
        self.config.write_text('start_column = "Doing"\n')
        self.assertEqual(load_config(self.config).start_column, "Doing")
    def test_merge_setting(self):
        self.assertEqual(load_config(self.config).merge, "local")
        self.config.write_text('merge = "pr"\n')
        self.assertEqual(load_config(self.config).merge, "pr")
        self.config.write_text('merge = "squash"\n')
        with self.assertRaises(HerdboardError):
            load_config(self.config)

    def test_config_errors(self):
        for text in ('start_agent = "unsupported"', 'start_agent = 2', 'start_column = ""', 'start_column = []'):
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(HerdboardError):
                    load_config(self.config)
    def test_model_settings(self):
        config = load_config(self.config)
        self.assertEqual((config.start_model, config.start_thinking, config.pi_models), (None, None, None))
        self.config.write_text('start_model = "zai/glm-5.3"\nstart_thinking = "low"\npi_models = ["a/b", "c/d:high"]\n')
        config = load_config(self.config)
        self.assertEqual((config.start_model, config.start_thinking, config.pi_models), ("zai/glm-5.3", "low", ["a/b", "c/d:high"]))

    def test_model_setting_errors(self):
        for text in ('start_thinking = "huge"', 'start_model = "two words"', 'start_model = ""', 'pi_models = "a/b"'):
            with self.subTest(text=text):
                self.config.write_text(text)
                with self.assertRaises(HerdboardError):
                    load_config(self.config)

    def test_cli_accepts_codex_for_all_agent_roles(self):
        parser = cli.build_parser()
        for command in (["start", "t123", "--agent", "codex"],
                        ["plan", "t123", "--agent", "codex"],
                        ["review", "t123", "--agent", "--reviewer", "codex"],
                        ["pr-review", "123", "--agent", "codex"]):
            with self.subTest(command=command):
                args = parser.parse_args([*command, "--thinking", "ultra"])
                self.assertEqual(args.thinking, "ultra")

    def test_pr_review_filters_config_thinking_for_the_agent(self):
        from types import SimpleNamespace

        from herdboard.config import Config
        config = Config()
        for kind, level, expected in (("codex", "ultra", "ultra"), ("codex", "off", None),
                                      ("pi", "ultra", None), ("claude", "high", "high"),
                                      ("claude", "ultra", None)):
            with self.subTest(kind=kind, level=level):
                config.start_thinking = level
                args = SimpleNamespace(agent=kind, model=None, thinking=None, number=42)
                ctx = SimpleNamespace(args=args, config=config)
                with patch.object(cli, "_pr_target", return_value=(self.root, "owner/repo")), \
                        patch.object(cli.pr_reviews, "request") as request:
                    cli._cmd_pr_review(ctx)
                self.assertEqual(request.call_args.args[-1], expected)

    def test_cli_passes_model_and_thinking(self):
        out = io.StringIO()
        result = {"steps_done": [], "workspace_id": "w1", "pane_id": "w1:p1", "branch": "b", "worktree_path": "/w", "agent_name": "a", "ref": "t123"}
        with patch.object(cli, "Starter") as starter, patch.object(cli, "register_checkout"), contextlib.redirect_stdout(out):
            starter.return_value.start.return_value = result
            cli.main(["--config", str(self.config), "--json", "start", "t123", "--agent", "pi",
                      "--model", "openai-codex/gpt-6.1-sol", "--thinking", "medium"])
        kwargs = starter.return_value.start.call_args.kwargs
        self.assertEqual((kwargs["model"], kwargs["thinking"]), ("openai-codex/gpt-6.1-sol", "medium"))
        self.assertEqual(starter.call_args.kwargs["config_file"], self.config)

    def test_cli_start_json(self):
        result = {"steps_done": ["repository"], "workspace_id": "w1", "pane_id": "w1:p1", "branch": "t123-task", "worktree_path": "/tmp/work", "agent_name": None, "ref": "t123"}
        out = io.StringIO()
        with patch.object(cli, "Starter") as starter, patch.object(cli, "register_checkout"), contextlib.redirect_stdout(out):
            starter.return_value.start.return_value = result
            status = cli.main(["-C", str(self.root), "start", "t123", "--repo", "api", "--checkout", str(self.root), "--no-agent", "--json"])
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(out.getvalue()), result)
        self.assertEqual(starter.return_value.start.call_args.kwargs["repo"], "api")
    def test_error_json_has_progress(self):
        error = StartError("No checkout", "checkout_unknown")
        error.steps_done = ["repository"]
        out = io.StringIO()
        with patch.object(cli, "Starter") as starter, patch.object(cli, "register_checkout"), contextlib.redirect_stdout(out):
            starter.return_value.start.side_effect = error
            status = cli.main(["-C", str(self.root), "start", "t123", "--json"])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(out.getvalue())["error"]["steps_done"], ["repository"])
