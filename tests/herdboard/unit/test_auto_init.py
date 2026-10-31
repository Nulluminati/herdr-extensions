import shutil
from pathlib import Path
from unittest import mock

from herdboard import cli
from herdboard.config import load_config
from herdboard.identity import repo_identity
from herdboard.linear_links import ProjectLink
from herdboard.model import HerdboardError
from herdboard.store import Store
from support.herdboard_cli import CliTestCase


class AutoInitTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(
            mock.patch("herdboard.cli.LinearSource.active", new_callable=mock.PropertyMock, return_value=False)
        )

    def board(self):
        return Store(load_config(self.config)).board_for_repo(repo_identity(self.repo))

    def test_json_list_creates_repo_board_without_prompts(self):
        with mock.patch("builtins.input", side_effect=AssertionError("Unexpected prompt")):
            data = self.run_json("list", "--view", "repo", directory=self.repo)
        self.assertEqual(data["boards"][0]["name"], "acme-api")
        self.assertIsNotNone(self.board())

    def test_add_uses_created_repo_board(self):
        data = self.run_json("add", "first card", directory=self.repo)
        self.assertEqual(data["card"]["board"], "acme-api")
        self.assertNotIn("hint", data)

    def test_interactive_setup_saves_merge_and_project(self):
        link = ProjectLink("p1", "API")
        with (
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("herdboard.cli.LinearSource.active", new_callable=mock.PropertyMock, return_value=True),
            mock.patch("builtins.input", side_effect=["pr", "API", "bug, repo:api"]),
            mock.patch("herdboard.cli._project_link", return_value=link) as resolve,
        ):
            self.run_cli("list", "--source", "todo", directory=self.repo)
        self.assertEqual(resolve.call_args.args[1:], ("API", ["bug", "repo:api"]))
        meta = Store(load_config(self.config)).board_meta(self.board())
        self.assertEqual(meta.merge, "pr")
        self.assertEqual(meta.links, [link])

    def test_existing_board_does_not_prompt_or_change(self):
        path = Path(self.init_repo_board())
        before = (path / "board.toml").read_bytes()
        with (
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("builtins.input", side_effect=AssertionError("Unexpected prompt")),
        ):
            self.run_cli("list", "--source", "todo", directory=self.repo)
        self.assertEqual((path / "board.toml").read_bytes(), before)

    def test_invalid_merge_repeats_question(self):
        with (
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("builtins.input", side_effect=["bad", "local", ""]),
        ):
            _, err = self.run_cli("list", "--source", "todo", directory=self.repo)
        self.assertIn("Choose local or pr", err)
        self.assertEqual(Store(load_config(self.config)).board_meta(self.board()).merge, "local")

    def test_failed_lookup_repeats_project_question_and_keeps_merge_choice(self):
        link = ProjectLink("p1", "API")
        with (
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("herdboard.cli.LinearSource.active", new_callable=mock.PropertyMock, return_value=True),
            mock.patch("builtins.input", side_effect=["pr", "AP", "bug", "API", "bug"]),
            mock.patch("herdboard.cli._project_link", side_effect=[HerdboardError("Lookup failed"), link]),
        ):
            _, err = self.run_cli("list", "--source", "todo", directory=self.repo)
        self.assertIn("Lookup failed", err)
        self.assertEqual(err.count("Merge method"), 1)
        self.assertEqual(err.count("Linear project"), 2)
        meta = Store(load_config(self.config)).board_meta(self.board())
        self.assertEqual((meta.merge, meta.links), ("pr", [link]))

    def test_failed_lookup_can_skip_link_without_restarting_setup(self):
        with (
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("herdboard.cli.LinearSource.active", new_callable=mock.PropertyMock, return_value=True),
            mock.patch("builtins.input", side_effect=["pr", "API", "bad-label", ""]),
            mock.patch("herdboard.cli._project_link", side_effect=HerdboardError("Unknown label")),
        ):
            _, err = self.run_cli("list", "--source", "todo", directory=self.repo)
        self.assertIn("Unknown label", err)
        self.assertEqual(Store(load_config(self.config)).board_meta(self.board()).merge, "pr")

    def test_failed_lookup_can_cancel_without_a_partial_board(self):
        with (
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("herdboard.cli.LinearSource.active", new_callable=mock.PropertyMock, return_value=True),
            mock.patch("builtins.input", side_effect=["pr", "API", "", KeyboardInterrupt]),
            mock.patch("herdboard.cli._project_link", side_effect=HerdboardError("Lookup failed")),
        ):
            _, err = self.run_cli("list", directory=self.repo, expect=1)
        self.assertIn("cancelled", err)
        self.assertIsNone(self.board())

    def test_disabled_linear_skips_project_question(self):
        with (
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("builtins.input", side_effect=["local"]) as answers,
        ):
            _, err = self.run_cli("list", "--source", "todo", directory=self.repo)
        self.assertEqual(answers.call_count, 1)
        self.assertNotIn("Linear project", err)

    def test_offline_setup_skips_project_question(self):
        with (
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("herdboard.cli.LinearSource.active", new_callable=mock.PropertyMock, return_value=True),
            mock.patch("builtins.input", side_effect=["local"]) as answers,
        ):
            _, err = self.run_cli("list", "--offline", directory=self.repo)
        self.assertEqual(answers.call_count, 1)
        self.assertNotIn("Linear project", err)

    def test_terminal_utility_commands_create_board_without_questions(self):
        for command, arguments in (
            ("show", ["tabc"]),
            ("search", ["text"]),
            ("prs", []),
            ("mockup-view", ["/tmp/image.png"]),
            ("automation", ["list"]),
            ("review-wait", ["tabc", "--folder", "/tmp/review"]),
            ("pr-wait", ["tabc", "--folder", "/tmp/review"]),
            ("plan-wait", ["tabc", "--pane", "w1:p7"]),
            ("pr-review-wait", ["acme/api#7"]),
        ):
            with (
                self.subTest(command=command),
                mock.patch("herdboard.cli._interactive", return_value=True),
                mock.patch("builtins.input", side_effect=AssertionError("Unexpected prompt")),
                mock.patch.dict(cli.COMMANDS, {command: lambda ctx: {"exit_code": 0}}),
                mock.patch.object(cli, "_render"),
                mock.patch.object(cli, "_update_sidebar"),
            ):
                self.run_cli(command, *arguments, directory=self.repo)
                self.assertIsNotNone(self.board())
                shutil.rmtree(self.board())

    def test_missing_board_recovery_does_not_recommend_manual_init(self):
        with mock.patch.object(cli, "_ensure_repo_board"):
            for arguments in (("link", "API"), ("pin", "ENG-1"), ("archive", "--done"), ("add", "card")):
                with self.subTest(arguments=arguments):
                    data = self.run_json(*arguments, directory=self.repo, expect=1)
                    self.assertNotIn("herdboard init", data["error"]["message"])
                    self.assertIn("retry", data["error"]["message"].lower())
        self.assertFalse((self.todos / "inbox.md").exists())

    def test_eof_cancels_setup_without_board(self):
        with (
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("builtins.input", side_effect=EOFError),
        ):
            _, err = self.run_cli("list", directory=self.repo, expect=1)
        self.assertIn("cancel", err.lower())
        self.assertIsNone(self.board())

    def test_board_starts_after_terminal_setup(self):
        from herdboard.board import HerdboardApp

        seen = {}
        with (
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("builtins.input", side_effect=["pr", ""]),
            mock.patch.object(HerdboardApp, "__init__", lambda app, store, view, **kw: seen.update(view=view, **kw)),
            mock.patch.object(HerdboardApp, "run"),
        ):
            self.run_cli("board", directory=self.repo)
        self.assertEqual(seen["view"], "repo")
        self.assertEqual(seen["repo_board"], self.board())
        self.assertEqual(Store(load_config(self.config)).board_meta(self.board()).merge, "pr")

    def test_json_keeps_global_merge_default(self):
        self.config.write_text(self.config.read_text() + 'merge = "pr"\n')
        with (
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("builtins.input", side_effect=AssertionError("Unexpected prompt")),
        ):
            self.run_json("list", directory=self.repo)
        store = Store(load_config(self.config))
        self.assertIsNone(store.board_meta(self.board()).merge)
        self.assertEqual(store.config.merge, "pr")

    def test_racing_creation_keeps_existing_settings(self):
        original = Store.init_board

        def race(store, identity, **kwargs):
            original(store, identity, merge="pr")
            return original(store, identity, **kwargs)

        with mock.patch.object(Store, "init_board", race):
            self.run_json("list", directory=self.repo)
        self.assertEqual(Store(load_config(self.config)).board_meta(self.board()).merge, "pr")

    def test_racing_creation_reports_unapplied_wizard_answers(self):
        original = Store.init_board

        def race(store, identity, **kwargs):
            original(store, identity, merge="pr")
            return original(store, identity, **kwargs)

        with (
            mock.patch.object(Store, "init_board", race),
            mock.patch("herdboard.cli._interactive", return_value=True),
            mock.patch("builtins.input", side_effect=["local", ""]),
        ):
            _, err = self.run_cli("list", "--source", "todo", directory=self.repo)
        self.assertIn("answers were not applied", err)
        self.assertEqual(Store(load_config(self.config)).board_meta(self.board()).merge, "pr")
