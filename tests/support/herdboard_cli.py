import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdboard import cli


def git(directory, *args):
    subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True)


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}))
        self.todos = root / "todos"
        self.config = root / "config.toml"
        self.config.write_text(f'todos_dir = "{self.todos}"\n')
        self.repo = root / "api"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "remote", "add", "origin", "https://github.com/acme/api.git")
        self.plain = root / "plain"
        self.plain.mkdir()

    def run_cli(self, *args, directory=None, expect=0):
        argv = ["--config", str(self.config)]
        argv += ["-C", str(directory if directory is not None else self.plain)]
        argv += list(args)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        self.assertEqual(code, expect, f"stdout={out.getvalue()!r} stderr={err.getvalue()!r}")
        return out.getvalue(), err.getvalue()

    def run_json(self, *args, directory=None, expect=0):
        out, _ = self.run_cli("--json", *args, directory=directory, expect=expect)
        return json.loads(out)

    def init_repo_board(self):
        return self.run_json("init", directory=self.repo)["board"]
