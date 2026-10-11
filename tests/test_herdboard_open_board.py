import importlib.util
import json
import os
from pathlib import Path
import subprocess
import tomllib
import unittest
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "plugins/herdboard"
SCRIPT = PLUGIN / "bin/open_board.py"
spec = importlib.util.spec_from_file_location("open_board", SCRIPT)
if spec is None or spec.loader is None:
    raise RuntimeError("Cannot load open_board script")
open_board = importlib.util.module_from_spec(spec)
spec.loader.exec_module(open_board)


class OpenBoardTests(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.environment = patch.dict(os.environ, {"HERDR_BIN_PATH": "herdr"}, clear=False)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        os.environ.pop("HERDR_PLUGIN_CONTEXT_JSON", None)

    def fake_run(self, argv, **kwargs):
        self.calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "{}", "")

    def run_script(self):
        with patch.object(open_board.subprocess, "run", self.fake_run):
            return open_board.main()

    def test_opens_the_board_tab_in_the_focused_pane_directory(self):
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"focused_pane_cwd": "/work/api"})

        self.assertEqual(self.run_script(), 0)

        self.assertEqual(self.calls, [[
            "herdr", "plugin", "pane", "open", "--plugin", "nulluminati.herdboard",
            "--entrypoint", "board", "--placement", "tab", "--cwd", "/work/api", "--focus",
        ]])

    def test_falls_back_to_the_workspace_directory(self):
        os.environ["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps({"workspace_cwd": "/work"})

        self.run_script()

        self.assertEqual(self.calls[0][self.calls[0].index("--cwd") + 1], "/work")

    def test_opens_without_a_directory_when_herdr_gives_none(self):
        self.run_script()

        self.assertNotIn("--cwd", self.calls[0])

    def test_manifest_declares_the_action_and_the_pane(self):
        manifest = tomllib.loads((PLUGIN / "herdr-plugin.toml").read_text())

        self.assertEqual(manifest["id"], "nulluminati.herdboard")
        self.assertEqual([action["id"] for action in manifest["actions"]], ["board"])
        self.assertEqual(manifest["actions"][0]["command"], ["python3", "bin/open_board.py"])
        self.assertEqual([pane["id"] for pane in manifest["panes"]], ["board"])


if __name__ == "__main__":
    unittest.main()
