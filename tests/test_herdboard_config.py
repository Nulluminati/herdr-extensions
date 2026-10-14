import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard.config import config_path, load_config  # noqa: E402
from herdboard.model import HerdboardError  # noqa: E402


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

    def test_defaults_without_file(self):
        config = load_config()
        self.assertEqual(config.todos_dir, self.dir / "home" / "todos")
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

    def test_tag_style_defaults_to_round(self):
        self.assertEqual(load_config().tag_style, "round")

    def test_tag_style_block(self):
        path = self.write(self.dir / "a.toml", 'tag_style = "block"\n')
        self.assertEqual(load_config(path).tag_style, "block")

    def test_tag_style_must_be_known(self):
        path = self.write(self.dir / "a.toml", 'tag_style = "square"\n')
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
        with self.assertRaisesRegex(HerdboardError, "api_key"):
            load_config(path)

    def test_invalid_values(self):
        for text in ('linear = 1', '[linear]\nenabled = "yes"', '[linear.columns]\nstarted = 1',
                     '[linear.columns]\nunknown = "X"', '[linear]\nstate_names = 1'):
            with self.subTest(text=text):
                path = self.write(self.dir / "a.toml", text)
                with self.assertRaises(HerdboardError):
                    load_config(path)

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
