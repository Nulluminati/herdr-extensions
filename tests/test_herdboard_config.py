import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard.config import load_config  # noqa: E402
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


if __name__ == "__main__":
    unittest.main()
