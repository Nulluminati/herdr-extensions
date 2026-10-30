import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from support.paths import ROOT

SRC = ROOT / "plugins/herdboard/src"

# `sys.modules["yaml"] = None` makes `import yaml` fail, like a missing package.
RUN = """
import sys
sys.modules["yaml"] = None
sys.path.insert(0, {src!r})
import herdboard.store, herdboard.cli
sys.exit(herdboard.cli.main(sys.argv[1:]))
"""


class LazyYamlTests(unittest.TestCase):
    def run_cli(self, *args):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "config.toml"
            config.write_text(f'todos_dir = "{root / "todos"}"\n')
            env = {**os.environ, "HOME": str(root), "XDG_STATE_HOME": str(root / "state"),
                   "LINEAR_API_KEY": ""}
            runs = []
            for command in (["add", "buy milk"], list(args)):
                runs.append(subprocess.run(
                    [sys.executable, "-c", RUN.format(src=str(SRC)), "--config", str(config),
                     "-C", str(root), *command],
                    capture_output=True, text=True, env=env))
            return runs

    def test_local_commands_work_without_pyyaml(self):
        add, listing = self.run_cli("list")
        for run in (add, listing):
            self.assertEqual(run.returncode, 0, run.stderr)
            self.assertEqual(run.stderr, "")
        self.assertIn("buy milk", listing.stdout)


if __name__ == "__main__":
    unittest.main()
