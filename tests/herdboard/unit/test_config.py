import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdboard.config import LinearConfig, config_path, load_config
from herdboard.linear import LinearAuthError
from herdboard.model import HerdboardError


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        env = {"HOME": str(self.dir / "home"), "XDG_CONFIG_HOME": "", "HERDBOARD_CONFIG": ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_codex_settings_and_agent_thinking_levels(self):
        from herdboard.config import AGENTS, save_global_config, thinking_levels
        self.assertEqual(AGENTS, ("claude", "pi", "codex"))
        self.assertEqual(thinking_levels("claude"), ("low", "medium", "high", "xhigh", "max"))
        self.assertIn("off", thinking_levels("pi"))
        self.assertNotIn("off", thinking_levels("codex"))
        self.assertIn("ultra", thinking_levels("codex"))
        path = self.write(self.dir / "a.toml", 'start_agent = "codex"\nstart_thinking = "ultra"\ncodex_models = ["gpt-6.1"]\n')
        config = load_config(path)
        self.assertEqual((config.start_agent, config.start_thinking, config.codex_models),
                         ("codex", "ultra", ["gpt-6.1"]))
        save_global_config(path, start_agent="codex", start_thinking="ultra")
        self.assertEqual(load_config(path).start_agent, "codex")

    def test_codex_models_must_be_a_list_of_names(self):
        for value in ('"gpt-6.1"', '3', '["gpt-6.1", 2]', '[""]'):
            with self.subTest(value=value):
                path = self.write(self.dir / "a.toml", f'codex_models = {value}\n')
                with self.assertRaisesRegex(HerdboardError, "codex_models"):
                    load_config(path)

    def test_defaults_without_file(self):
        config = load_config()
        self.assertEqual(config.todos_dir, self.dir / "home" / "todos")
        self.assertEqual(config.projects_dir, self.dir / "home" / "Projects")
        self.assertEqual(config.columns, ["Backlog", "Todo", "In progress", "Review", "Done"])
        self.assertEqual(config.complete_markers, ["Complete"])
        self.assertIsNone(config.vault_root)

    def test_theme_defaults_to_the_terminal_palette(self):
        self.assertEqual(load_config().theme, "ansi-dark")

    def test_theme_from_file(self):
        path = self.write(self.dir / "a.toml", 'theme = "nord"\n')
        self.assertEqual(load_config(path).theme, "nord")

    def test_theme_must_be_a_name(self):
        for text in ("theme = 3", 'theme = ""'):
            with self.subTest(text=text):
                path = self.write(self.dir / "a.toml", text + "\n")
                with self.assertRaises(HerdboardError):
                    load_config(path)

    def test_mockup_model_defaults_to_a_codex_image_model(self):
        self.assertEqual(load_config().mockup_model, "gpt-6.1-sol")

    def test_mockup_model_from_file(self):
        path = self.write(self.dir / "a.toml", 'mockup_model = "gpt-7"\n')
        self.assertEqual(load_config(path).mockup_model, "gpt-7")

    def test_mockup_model_must_be_one_name(self):
        for text in ("mockup_model = 3", 'mockup_model = ""', 'mockup_model = "two words"'):
            with self.subTest(text=text):
                path = self.write(self.dir / "a.toml", text + "\n")
                with self.assertRaises(HerdboardError):
                    load_config(path)

    def test_tag_style_defaults_to_outline(self):
        self.assertEqual(load_config().tag_style, "outline")

    def test_tag_style_block_round_and_outline(self):
        for style in ("block", "round", "outline"):
            with self.subTest(style=style):
                path = self.write(self.dir / "a.toml", f'tag_style = "{style}"\n')
                self.assertEqual(load_config(path).tag_style, style)

    def test_tag_style_must_be_known(self):
        path = self.write(self.dir / "a.toml", 'tag_style = "square"\n')
        with self.assertRaises(HerdboardError):
            load_config(path)

    def test_card_style_defaults_to_full(self):
        self.assertEqual(load_config().card_style, "full")

    def test_card_style_compact(self):
        path = self.write(self.dir / "a.toml", 'card_style = "compact"\n')
        self.assertEqual(load_config(path).card_style, "compact")

    def test_card_style_must_be_known(self):
        for value in ('"small"', '""', '3'):
            with self.subTest(value=value):
                path = self.write(self.dir / "a.toml", f"card_style = {value}\n")
                with self.assertRaisesRegex(HerdboardError, "card_style"):
                    load_config(path)

    def test_icon_style_defaults_to_plain(self):
        self.assertEqual(load_config().icon_style, "plain")

    def test_icon_style_modes_and_tag_style_are_independent(self):
        for mode in ("plain", "nerd"):
            for tags in ("round", "block"):
                with self.subTest(mode=mode, tags=tags):
                    path = self.write(self.dir / "a.toml", f'icon_style = "{mode}"\ntag_style = "{tags}"\n')
                    config = load_config(path)
                    self.assertEqual(config.icon_style, mode)
                    self.assertEqual(config.tag_style, tags)

    def test_icon_style_must_be_a_known_nonempty_string(self):
        for value in ('"current"', '"auto"', '"NERD"', '""', '3', 'true', '[]'):
            with self.subTest(value=value):
                path = self.write(self.dir / "a.toml", f"icon_style = {value}\n")
                with self.assertRaisesRegex(HerdboardError, "icon_style"):
                    load_config(path)

    def test_save_display_creates_the_selected_config_file(self):
        from herdboard.config import save_display_config
        path = self.dir / "settings" / "config.toml"
        save_display_config(path, icon_style="nerd", tag_style="block")
        config = load_config(path)
        self.assertEqual((config.icon_style, config.tag_style), ("nerd", "block"))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_save_display_preserves_comments_tables_and_other_settings(self):
        from herdboard.config import save_display_config
        original = ('# My settings\nicon_style = "plain" # icons\n'
                    "'tag_style' = 'round' # tags\n"
                    'theme = "nord"\n\n[linear]\n# Keep this section\n'
                    'enabled = false\nicon_style = "unrelated"\n'
                    'api_key_command = "secret-tool lookup service herdboard" # keyring\n')
        path = self.write(self.dir / "a.toml", original)
        path.chmod(0o640)
        save_display_config(path, icon_style="nerd", tag_style="block")
        self.assertEqual(path.read_text(), original.replace('"plain"', '"nerd"').replace("'round'", '"block"'))
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual(load_config(path).theme, "nord")

    def test_save_display_adds_root_keys_before_tables_and_only_writes_changed_keys(self):
        from herdboard.config import save_display_config
        path = self.write(self.dir / "a.toml", 'tag_style = "block"\n[linear]\nenabled = false\n')
        save_display_config(path, icon_style="nerd")
        config = load_config(path)
        self.assertEqual((config.icon_style, config.tag_style), ("nerd", "block"))
        save_display_config(path, tag_style="round")
        self.assertEqual(load_config(path).icon_style, "nerd")

    def test_save_display_writes_card_style_and_outline_tags(self):
        from herdboard.config import save_display_config
        path = self.dir / "a.toml"
        save_display_config(path, card_style="compact", tag_style="outline")
        config = load_config(path)
        self.assertEqual((config.card_style, config.tag_style), ("compact", "outline"))

    def test_save_display_uses_the_config_environment_path(self):
        from herdboard.config import save_display_config
        path = self.dir / "env.toml"
        with mock.patch.dict(os.environ, {"HERDBOARD_CONFIG": str(path)}):
            save_display_config(icon_style="nerd")
        self.assertEqual(load_config(path).icon_style, "nerd")

    def test_save_display_rejects_unknown_values_before_creating_files(self):
        from herdboard.config import save_display_config
        path = self.dir / "settings" / "config.toml"
        for values in ({"icon_style": "auto"}, {"tag_style": "square"}, {"icon_style": 1},
                       {"card_style": "small"}):
            with self.subTest(values=values):
                with self.assertRaises(HerdboardError):
                    save_display_config(path, **values)
                self.assertFalse(path.exists())

    def test_save_display_does_not_replace_invalid_or_unreadable_configs(self):
        from herdboard.config import save_display_config
        for content in (b"invalid = [", b"# \xff"):
            with self.subTest(content=content):
                path = self.dir / "a.toml"
                path.write_bytes(content)
                with self.assertRaises(HerdboardError):
                    save_display_config(path, icon_style="nerd")
                self.assertEqual(path.read_bytes(), content)
        path = self.dir / "folder"
        path.mkdir()
        with self.assertRaises(HerdboardError):
            save_display_config(path, icon_style="nerd")

    def test_save_display_failure_keeps_the_file_and_cleans_up_the_temporary_file(self):
        from herdboard.config import save_display_config
        path = self.write(self.dir / "a.toml", '# Keep\nicon_style = "plain"\n')
        original = path.read_bytes()
        with mock.patch("herdboard.config.os.replace", side_effect=OSError("read-only directory")):
            with self.assertRaisesRegex(HerdboardError, "read-only directory"):
                save_display_config(path, icon_style="nerd")
        self.assertEqual(path.read_bytes(), original)
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [".a.toml.lock", "a.toml"])

    def test_save_display_preserves_a_symlink(self):
        from herdboard.config import save_display_config
        target = self.write(self.dir / "target.toml", 'icon_style = "plain"\n')
        path = self.dir / "linked.toml"
        path.symlink_to(target)
        save_display_config(path, icon_style="nerd")
        self.assertTrue(path.is_symlink())
        self.assertEqual(load_config(target).icon_style, "nerd")

    def test_concurrent_display_saves_keep_both_changes(self):
        from concurrent.futures import ThreadPoolExecutor

        from herdboard.config import save_display_config
        path = self.write(self.dir / "a.toml", '# Keep\n[linear]\nenabled = false\n')
        with ThreadPoolExecutor(max_workers=2) as pool:
            icon = pool.submit(save_display_config, path, icon_style="nerd")
            tag = pool.submit(save_display_config, path, tag_style="block")
            icon.result()
            tag.result()
        config = load_config(path)
        self.assertEqual((config.icon_style, config.tag_style), ("nerd", "block"))

    def test_save_global_writes_each_setting(self):
        from herdboard.config import save_global_config
        path = self.dir / "a.toml"
        save_global_config(path, theme="nord", start_agent="pi", start_model="zai/glm-5.3",
                           start_thinking="high", merge="pr", icon_style="nerd", tag_style="block",
                           card_style="compact")
        config = load_config(path)
        self.assertEqual(
            (config.theme, config.start_agent, config.start_model, config.start_thinking, config.merge,
             config.icon_style, config.tag_style, config.card_style),
            ("nord", "pi", "zai/glm-5.3", "high", "pr", "nerd", "block", "compact"))

    def test_save_global_none_removes_the_model_and_thinking_level_only(self):
        from herdboard.config import save_global_config
        original = ('# Mine\nstart_agent = "pi"\nstart_model = "zai/glm-5.3" # model\n'
                    'start_thinking = "high"\ntheme = "nord"\n\n[linear]\n# Keep\nenabled = false\n')
        path = self.write(self.dir / "a.toml", original)
        save_global_config(path, start_model=None, start_thinking=None)
        self.assertEqual(path.read_text(), original.replace('start_model = "zai/glm-5.3" # model\n', "")
                         .replace('start_thinking = "high"\n', ""))
        config = load_config(path)
        self.assertEqual((config.start_agent, config.start_model, config.start_thinking, config.theme),
                         ("pi", None, None, "nord"))

    def test_save_global_writes_only_the_named_settings(self):
        from herdboard.config import save_global_config
        original = '# Mine\nstart_model = "opus"\nmerge = "local" # merge\n'
        path = self.write(self.dir / "a.toml", original)
        save_global_config(path, merge="pr")
        self.assertEqual(path.read_text(), original.replace('"local"', '"pr"'))

    def test_save_global_without_changes_creates_no_file(self):
        from herdboard.config import save_global_config
        path = self.dir / "settings" / "config.toml"
        save_global_config(path)
        save_global_config(path, start_model=None, start_thinking=None)
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())

    def test_save_global_rejects_invalid_values_before_any_write(self):
        from herdboard.config import save_global_config
        original = 'icon_style = "plain"\n'
        path = self.write(self.dir / "a.toml", original)
        for values in ({"start_agent": "unsupported"}, {"start_agent": None}, {"start_model": "two words"},
                       {"start_model": ""}, {"start_model": 3}, {"start_thinking": "huge"},
                       {"merge": "squash"}, {"merge": None}, {"theme": ""}, {"theme": None},
                       {"theme": 3}, {"icon_style": "nerd", "card_style": "small"}):
            with self.subTest(values=values):
                with self.assertRaises(HerdboardError):
                    save_global_config(path, **values)
                self.assertEqual(path.read_text(), original)

    def test_check_global_accepts_valid_values_and_rejects_invalid_ones(self):
        from herdboard.config import check_global_config
        check_global_config(theme="nord", start_agent="pi", start_model=None, start_thinking=None, merge="pr")
        for values in ({"start_model": "zai/glm 5"}, {"merge": "squash"}, {"start_agent": None}):
            with self.subTest(values=values):
                with self.assertRaises(HerdboardError):
                    check_global_config(**values)

    def test_save_global_does_not_replace_an_invalid_config(self):
        from herdboard.config import save_global_config
        path = self.write(self.dir / "a.toml", "invalid = [")
        with self.assertRaisesRegex(HerdboardError, "invalid TOML"):
            save_global_config(path, start_agent="pi")
        self.assertEqual(path.read_text(), "invalid = [")

    def test_save_global_write_failure_keeps_the_file(self):
        from herdboard.config import save_global_config
        path = self.write(self.dir / "a.toml", 'merge = "local"\n')
        with mock.patch("herdboard.config.os.replace", side_effect=OSError("read-only directory")):
            with self.assertRaisesRegex(HerdboardError, "read-only directory"):
                save_global_config(path, merge="pr")
        self.assertEqual(path.read_text(), 'merge = "local"\n')
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), [".a.toml.lock", "a.toml"])

    def test_save_global_uses_the_config_environment_path(self):
        from herdboard.config import save_global_config
        path = self.dir / "env.toml"
        with mock.patch.dict(os.environ, {"HERDBOARD_CONFIG": str(path)}):
            save_global_config(start_agent="pi")
        self.assertEqual(load_config(path).start_agent, "pi")

    def test_concurrent_thinking_saves_keep_both_models(self):
        from concurrent.futures import ThreadPoolExecutor

        from herdboard.config import save_thinking_levels
        path = self.write(self.dir / "a.toml", 'icon_style = "nerd"\n')
        with ThreadPoolExecutor(max_workers=2) as pool:
            one = pool.submit(save_thinking_levels, path, "a/b", "high")
            two = pool.submit(save_thinking_levels, path, "c/d", "low")
            one.result()
            two.result()
        self.assertEqual(load_config(path).thinking, {"a/b": "high", "c/d": "low"})

    def test_save_global_uses_the_default_path(self):
        from herdboard.config import save_global_config
        save_global_config(theme="nord")
        self.assertEqual(load_config(self.dir / "home/.config/herdboard/config.toml").theme, "nord")

    def test_thinking_levels_per_model_load_from_the_thinking_table(self):
        path = self.write(self.dir / "a.toml",
                          '[thinking]\n"zai/glm-5.3" = "high"\n"opus" = "low"\n')
        self.assertEqual(load_config(path).thinking, {"zai/glm-5.3": "high", "opus": "low"})

    def test_thinking_levels_without_a_table_are_empty(self):
        self.assertEqual(load_config(self.write(self.dir / "a.toml", 'theme = "nord"\n')).thinking, {})

    def test_thinking_levels_reject_a_bad_level_or_a_flat_value(self):
        for text in ('[thinking]\n"a/b" = "huge"\n', '[thinking]\n"a/b" = 3\n',
                     '[thinking]\n"a/b" = ""\n', 'thinking = "high"\n'):
            with self.subTest(text=text):
                path = self.write(self.dir / "a.toml", text)
                with self.assertRaisesRegex(HerdboardError, "thinking"):
                    load_config(path)

    def test_save_thinking_levels_writes_and_removes_one_model(self):
        from herdboard.config import save_thinking_levels
        path = self.write(self.dir / "a.toml", '# Keep\nicon_style = "nerd"\n\n[linear]\n# Linear\nenabled = false\n')
        save_thinking_levels(path, "zai/glm-5.3", "high")
        save_thinking_levels(path, "opus", "low")
        self.assertEqual(load_config(path).thinking, {"zai/glm-5.3": "high", "opus": "low"})
        self.assertIn('# Keep\nicon_style = "nerd"\n', path.read_text())
        self.assertIn('[linear]\n# Linear\nenabled = false\n', path.read_text())
        save_thinking_levels(path, "opus", None)
        self.assertEqual(load_config(path).thinking, {"zai/glm-5.3": "high"})
        save_thinking_levels(path, "zai/glm-5.3", None)
        # The last model leaves no empty table behind.
        self.assertNotIn("thinking", path.read_text())

    def test_save_thinking_levels_rejects_a_bad_model_or_level(self):
        from herdboard.config import save_thinking_levels
        path = self.write(self.dir / "a.toml", 'icon_style = "nerd"\n')
        for model, level in (("two words", "high"), ("", "high"), ("a/b", "huge"), ("a/b", "")):
            with self.subTest(model=model, level=level):
                with self.assertRaises(HerdboardError):
                    save_thinking_levels(path, model, level)
                self.assertEqual(path.read_text(), 'icon_style = "nerd"\n')

    def test_save_thinking_levels_does_not_create_a_file_for_a_removal(self):
        from herdboard.config import save_thinking_levels
        path = self.dir / "settings" / "config.toml"
        save_thinking_levels(path, "a/b", None)
        self.assertFalse(path.exists())

    def test_concurrent_global_saves_keep_both_changes(self):
        from concurrent.futures import ThreadPoolExecutor

        from herdboard.config import save_global_config
        path = self.write(self.dir / "a.toml", '# Keep\n[linear]\nenabled = false\n')
        with ThreadPoolExecutor(max_workers=2) as pool:
            agent = pool.submit(save_global_config, path, start_agent="pi")
            merge = pool.submit(save_global_config, path, merge="pr")
            agent.result()
            merge.result()
        config = load_config(path)
        self.assertEqual((config.start_agent, config.merge), ("pi", "pr"))

    def test_details_pane_settings_default_to_on_and_a_quarter(self):
        config = load_config()
        self.assertEqual((config.details_pane, config.details_pane_ratio), (True, 0.25))

    def test_details_pane_settings_from_file(self):
        path = self.write(self.dir / "a.toml", "details_pane = false\ndetails_pane_ratio = 0.4\n")
        config = load_config(path)
        self.assertEqual((config.details_pane, config.details_pane_ratio), (False, 0.4))

    def test_details_pane_settings_must_be_valid(self):
        for text in ('details_pane = "yes"', "details_pane = 1", "details_pane_ratio = 0.05",
                     "details_pane_ratio = 0.7", 'details_pane_ratio = "0.3"', "details_pane_ratio = true"):
            with self.subTest(text=text):
                path = self.write(self.dir / "a.toml", text + "\n")
                with self.assertRaises(HerdboardError):
                    load_config(path)

    def test_save_global_writes_the_details_pane_settings(self):
        from herdboard.config import save_global_config
        path = self.dir / "a.toml"
        save_global_config(path, details_pane=False, details_pane_ratio=0.3)
        config = load_config(path)
        self.assertEqual((config.details_pane, config.details_pane_ratio), (False, 0.3))

    def test_check_global_rejects_bad_details_pane_values(self):
        from herdboard.config import check_global_config
        check_global_config(details_pane=True, details_pane_ratio=0.1)
        check_global_config(details_pane_ratio=0.6)
        for values in ({"details_pane": "true"}, {"details_pane": None}, {"details_pane_ratio": 0.61},
                       {"details_pane_ratio": True}, {"details_pane_ratio": None}):
            with self.subTest(values=values):
                with self.assertRaises(HerdboardError):
                    check_global_config(**values)

    def test_types_default(self):
        self.assertEqual(load_config().types, ["bug", "feature", "chore", "docs", "refactor"])

    def test_types_from_file(self):
        path = self.write(self.dir / "a.toml", 'types = ["bug", "spike"]\n')
        self.assertEqual(load_config(path).types, ["bug", "spike"])

    def test_types_must_be_single_words(self):
        for text in ('types = ["two words"]', 'types = [""]', 'types = ["a/b"]', 'types = ["#bug"]'):
            with self.subTest(text=text):
                path = self.write(self.dir / "a.toml", text + "\n")
                with self.assertRaises(HerdboardError):
                    load_config(path)

    def test_argument_path(self):
        path = self.write(self.dir / "a.toml", 'columns = ["A", "B"]\n')
        self.assertEqual(load_config(path).columns, ["A", "B"])

    def test_env_config(self):
        path = self.write(self.dir / "env.toml", 'columns = ["E"]\n')
        with mock.patch.dict(os.environ, {"HERDBOARD_CONFIG": str(path)}):
            self.assertEqual(load_config().columns, ["E"])

    def test_argument_beats_env(self):
        arg = self.write(self.dir / "arg.toml", 'columns = ["Arg"]\n')
        env = self.write(self.dir / "env.toml", 'columns = ["Env"]\n')
        with mock.patch.dict(os.environ, {"HERDBOARD_CONFIG": str(env)}):
            self.assertEqual(load_config(arg).columns, ["Arg"])

    def test_xdg_config_home(self):
        xdg = self.dir / "xdg"
        self.write(xdg / "herdboard/config.toml", 'columns = ["X"]\n')
        with mock.patch.dict(os.environ, {"XDG_CONFIG_HOME": str(xdg)}):
            self.assertEqual(load_config().columns, ["X"])

    def test_home_config(self):
        self.write(self.dir / "home/.config/herdboard/config.toml", 'columns = ["H"]\n')
        self.assertEqual(load_config().columns, ["H"])

    def test_tilde_expansion(self):
        path = self.write(
            self.dir / "a.toml", 'todos_dir = "~/t"\nvault_root = "~/Notes"\n'
        )
        config = load_config(path)
        self.assertEqual(config.todos_dir, self.dir / "home" / "t")
        self.assertEqual(config.vault_root, self.dir / "home" / "Notes")

    def test_an_explicit_relative_config_path_becomes_absolute(self):
        # Agents and workers run in other folders, so they get the full path.
        with mock.patch("os.getcwd", return_value=str(self.dir / "work")):
            self.assertEqual(config_path(Path("board.toml")), self.dir / "work" / "board.toml")
            self.assertEqual(config_path("~/b.toml"), self.dir / "home" / "b.toml")

    def test_a_relative_todos_dir_becomes_absolute(self):
        # Agents run in other folders, so every path that herdboard gives them must be absolute.
        path = self.write(self.dir / "a.toml", 'todos_dir = "t"\n')
        with mock.patch("os.getcwd", return_value=str(self.dir / "work")):
            config = load_config(path)
        self.assertEqual(config.todos_dir, self.dir / "work" / "t")

    def test_projects_dir_from_file(self):
        path = self.write(self.dir / "a.toml", 'projects_dir = "~/src"\n')
        config = load_config(path)
        self.assertEqual(config.projects_dir, self.dir / "home" / "src")

    def test_a_relative_projects_dir_becomes_absolute(self):
        path = self.write(self.dir / "a.toml", 'projects_dir = "p"\n')
        with mock.patch("os.getcwd", return_value=str(self.dir / "work")):
            config = load_config(path)
        self.assertEqual(config.projects_dir, self.dir / "work" / "p")

    def test_projects_dir_must_be_a_path(self):
        path = self.write(self.dir / "a.toml", "projects_dir = 3\n")
        with self.assertRaises(HerdboardError):
            load_config(path)

    def test_all_values(self):
        path = self.write(self.dir / "a.toml", 'complete_markers = ["Complete", "Shipped"]\n')
        self.assertEqual(load_config(path).complete_markers, ["Complete", "Shipped"])

    def test_archive_titles_default_and_added(self):
        self.assertEqual(load_config().archive_titles, ["Archive"])
        path = self.write(self.dir / "a.toml", 'archive_titles = ["Archiv", "Archive"]\n')
        self.assertEqual(load_config(path).archive_titles, ["Archive", "Archiv"])

    def test_archive_titles_wrong_type(self):
        path = self.write(self.dir / "a.toml", "archive_titles = 1\n")
        with self.assertRaises(HerdboardError):
            load_config(path)

    def test_non_utf8_config_is_io_error(self):
        path = self.dir / "bad.toml"
        path.write_bytes(b"\xff\xfe")
        with self.assertRaises(HerdboardError) as caught:
            load_config(path)
        self.assertEqual(caught.exception.code, "io")
        self.assertIn(str(path), str(caught.exception))

    @unittest.skipIf(os.geteuid() == 0, "root can read any file")
    def test_unreadable_config_is_io_error(self):
        path = self.write(self.dir / "locked.toml", "")
        path.chmod(0)
        self.addCleanup(path.chmod, 0o600)
        with self.assertRaises(HerdboardError) as caught:
            load_config(path)
        self.assertEqual(caught.exception.code, "io")
        self.assertIn(str(path), str(caught.exception))

    def test_unknown_keys_and_linear_ignored(self):
        path = self.write(
            self.dir / "a.toml",
            'mystery = 1\n[linear]\nenabled = true\n[linear.columns]\nbacklog = "Backlog"\n',
        )
        self.assertEqual(load_config(path).complete_markers, ["Complete"])

    def test_invalid_toml(self):
        path = self.write(self.dir / "bad.toml", "columns = [\n")
        with self.assertRaises(HerdboardError) as caught:
            load_config(path)
        self.assertIn(str(path), str(caught.exception))

    def test_wrong_types(self):
        for text in (
            "todos_dir = 3\n",
            'columns = "Todo"\n',
            "columns = [1]\n",
            "complete_markers = 1\n",
            "vault_root = [1]\n",
        ):
            path = self.write(self.dir / "t.toml", text)
            with self.subTest(text=text), self.assertRaises(HerdboardError) as caught:
                load_config(path)
            self.assertIn(str(path), str(caught.exception))


class LinearConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name)
        env = {"HOME": str(self.dir / "home"), "XDG_CONFIG_HOME": "", "HERDBOARD_CONFIG": ""}
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop("LINEAR_API_KEY", None)
        os.environ.pop("OTHER_KEY", None)

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def test_defaults(self):
        linear = load_config().linear
        self.assertIsNone(linear.enabled)
        self.assertEqual(linear.api_key_env, "LINEAR_API_KEY")
        self.assertEqual(linear.columns, {
            "backlog": "Backlog", "triage": "Backlog", "unstarted": "Todo",
            "started": "In progress", "completed": "Done", "canceled": "Done",
        })
        self.assertEqual(linear.state_names, {"In Review": "Review"})

    def test_active_follows_key_by_default(self):
        linear = load_config().linear
        self.assertFalse(linear.active)
        os.environ["LINEAR_API_KEY"] = "k"
        self.assertTrue(linear.active)

    def test_enabled_false_wins_and_true_needs_key(self):
        os.environ["LINEAR_API_KEY"] = "k"
        off = self.write(self.dir / "off.toml", "[linear]\nenabled = false\n")
        self.assertFalse(load_config(off).linear.active)
        on = self.write(self.dir / "on.toml", "[linear]\nenabled = true\n")
        self.assertTrue(load_config(on).linear.active)
        del os.environ["LINEAR_API_KEY"]
        self.assertFalse(load_config(on).linear.active)

    def test_custom_key_variable_and_overrides(self):
        path = self.write(self.dir / "a.toml", """
[linear]
api_key_env = "OTHER_KEY"

[linear.columns]
started = "Doing"

[linear.state_names]
"Ready" = "Todo"
""")
        os.environ["OTHER_KEY"] = "k"
        linear = load_config(path).linear
        self.assertTrue(linear.active)
        self.assertEqual(linear.columns["started"], "Doing")
        self.assertEqual(linear.columns["unstarted"], "Todo")
        self.assertEqual(linear.state_names, {"In Review": "Review", "Ready": "Todo"})

    def test_api_key_in_file_is_rejected(self):
        path = self.write(self.dir / "a.toml", '[linear]\napi_key = "lin_api_secret"\n')
        with self.assertRaisesRegex(HerdboardError, "api_key_command"):
            load_config(path)

    def test_invalid_values(self):
        for text in ('linear = 1', '[linear]\nenabled = "yes"', '[linear.columns]\nstarted = 1',
                     '[linear.columns]\nunknown = "X"', '[linear]\nstate_names = 1'):
            with self.subTest(text=text):
                path = self.write(self.dir / "a.toml", text)
                with self.assertRaises(HerdboardError):
                    load_config(path)

    def test_command_forms_and_expansion(self):
        for value in ('"~/bin/key lookup \'two words\'"', '["~/bin/key", "lookup", "two words"]'):
            with self.subTest(value=value):
                path = self.write(self.dir / "key.toml", f"[linear]\napi_key_command = {value}\n")
                self.assertEqual(load_config(path).linear.api_key_command,
                                 [str(self.dir / "home/bin/key"), "lookup", "two words"])

    def test_invalid_commands(self):
        for value in ('""', '"  "', '[]', '[""]', '[1]', '"\'unclosed"', '1', 'true'):
            with self.subTest(value=value):
                path = self.write(self.dir / "key.toml", f"[linear]\napi_key_command = {value}\n")
                with self.assertRaisesRegex(HerdboardError, "linear.api_key_command"):
                    load_config(path)

    def test_command_is_lazy_and_can_be_disabled(self):
        with mock.patch("herdboard.config.subprocess.run") as run:
            for enabled, active in ((None, True), (False, False), (True, True)):
                linear = LinearConfig(enabled=enabled, api_key_command=["key"])
                self.assertTrue(linear.key_configured)
                self.assertEqual(linear.key_source, "linear.api_key_command")
                self.assertEqual(linear.active, active)
            run.assert_not_called()

    def test_command_wins_and_caches_success_without_exposing_key(self):
        os.environ["LINEAR_API_KEY"] = "environment-key"
        linear = LinearConfig(api_key_command=["key", "lookup"])
        with mock.patch("herdboard.config.subprocess.run", return_value=
                        subprocess.CompletedProcess([], 0, "  command-secret\n", "")) as run:
            self.assertEqual(linear.api_key, "command-secret")
            self.assertEqual(linear.api_key, "command-secret")
            run.assert_called_once_with(["key", "lookup"], capture_output=True, text=True,
                                        timeout=30, stdin=subprocess.DEVNULL)
        self.assertNotIn("command-secret", repr(linear))
        self.assertEqual(linear, LinearConfig(api_key_command=["key", "lookup"]))

    def test_concurrent_command_reads_share_one_lookup(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor, TimeoutError

        linear = LinearConfig(api_key_command=["key"])
        first_started = threading.Event()
        second_started = threading.Event()
        release = threading.Event()

        def lookup(argv):
            if not first_started.is_set():
                first_started.set()
                self.assertTrue(release.wait(5))
            return "command-secret"

        def second_read():
            second_started.set()
            return linear.api_key

        with mock.patch("herdboard.config._run_key_command", side_effect=lookup) as run:
            with ThreadPoolExecutor(max_workers=2) as pool:
                first = pool.submit(lambda: linear.api_key)
                try:
                    self.assertTrue(first_started.wait(5))
                    second = pool.submit(second_read)
                    self.assertTrue(second_started.wait(5))
                    with self.assertRaises(TimeoutError):
                        second.result(timeout=0.2)
                finally:
                    release.set()
                self.assertEqual(first.result(timeout=5), "command-secret")
                self.assertEqual(second.result(timeout=5), "command-secret")
            run.assert_called_once_with(["key"])

    def test_command_with_embedded_null_is_auth_error(self):
        path = self.write(self.dir / "key.toml", '[linear]\napi_key_command = "key\\u0000name"\n')
        with self.assertRaises(LinearAuthError) as caught:
            _ = load_config(path).linear.api_key
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn("key\0name", str(caught.exception))

    def test_command_real_script(self):
        script = self.write(self.dir / "key script", "#!/bin/sh\necho lin_api_x\n")
        script.chmod(0o700)
        self.assertEqual(LinearConfig(api_key_command=[str(script)]).api_key, "lin_api_x")

    def test_command_failures_are_auth_errors_and_retry(self):
        failures = (FileNotFoundError("missing"), PermissionError("denied"),
                    ValueError("stdout-secret"),
                    UnicodeDecodeError("utf-8", b"stdout-secret\xff", 13, 14, "stdout-secret"),
                    subprocess.TimeoutExpired(["key"], 30, output="stdout-secret"),
                    subprocess.CompletedProcess([], 1, "stdout-secret", "locked\nsecond line"),
                    subprocess.CompletedProcess([], 0, " \n", ""))
        for failure in failures:
            with self.subTest(failure=type(failure).__name__, status=getattr(failure, "returncode", None)):
                linear = LinearConfig(api_key_command=["key", "two words"])
                with mock.patch("herdboard.config.subprocess.run", side_effect=[
                        failure, subprocess.CompletedProcess([], 0, "recovered", "")]) as run:
                    with self.assertRaises(LinearAuthError) as caught:
                        _ = linear.api_key
                    message = str(caught.exception)
                    self.assertEqual(caught.exception.exit_code, 3)
                    self.assertIn("key 'two words'", message)
                    self.assertNotIn("stdout-secret", message)
                    self.assertNotIn("second line", message)
                    self.assertEqual(linear.api_key, "recovered")
                    self.assertEqual(run.call_count, 2)

    def test_done_days(self):
        self.assertEqual(load_config().linear.done_days, 14)
        for text, expected in (("", 14), ("done_days = 0", 14), ("done_days = 30", 30)):
            with self.subTest(text=text):
                path = self.write(self.dir / "d.toml", f"[linear]\n{text}\n")
                self.assertEqual(load_config(path).linear.done_days, expected)
        for text in ("done_days = -1", 'done_days = "7"', "done_days = true", "done_days = 1.5"):
            with self.subTest(text=text):
                path = self.write(self.dir / "d.toml", f"[linear]\n{text}\n")
                with self.assertRaisesRegex(HerdboardError, "done_days"):
                    load_config(path)


if __name__ == "__main__":
    unittest.main()
