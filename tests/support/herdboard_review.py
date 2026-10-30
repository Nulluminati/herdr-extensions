import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from herdboard import review, start
from herdboard.config import Config
from herdboard.identity import repo_identity
from herdboard.store import Store
from support.herdboard_start import FakeHerdr


class PaneHerdr(FakeHerdr):
    """FakeHerdr with panes per workspace, agent focus and the hunkdiff review action."""

    def __init__(self, root):
        super().__init__(root)
        self.panes = {}
        self.focused = None
        self.hunk = "installed"
        self.next_pane = 2

    def reply(self, argv, result=None, error=None):
        if error:
            return subprocess.CompletedProcess(argv, 1, json.dumps({"error": {"code": error, "message": error}}), "")
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": result or {}}), "")

    def __call__(self, argv, timeout=120):
        args = argv[1:]
        operation = " ".join(args[:2])
        if operation == "pane list":
            self.calls.append(args)
            workspace = args[args.index("--workspace") + 1]
            panes = self.panes.setdefault(workspace, [workspace + ":p1"])
            return self.reply(argv, {"panes": [{"pane_id": p} for p in panes]})
        if operation == "agent focus":
            self.calls.append(args)
            if not self.agent:
                return self.reply(argv, error="agent_not_found")
            self.focused = args[2]
            return self.reply(argv, {"type": "ok"})
        if args[:3] == ["plugin", "action", "invoke"]:
            self.calls.append(args)
            if self.hunk == "missing":
                return self.reply(argv, error="plugin_not_found")
            workspace = self.focused.split(":")[0]
            panes = self.panes.setdefault(workspace, [workspace + ":p1"])
            if not any(p.endswith(":hunk") for p in panes):
                panes.append(f"{workspace}:p{self.next_pane}:hunk")
                self.next_pane += 1
            return self.reply(argv, {"type": "ok"})
        if args[:3] == ["plugin", "pane", "close"]:
            self.calls.append(args)
            for panes in self.panes.values():
                if args[3] in panes:
                    panes.remove(args[3])
            return self.reply(argv, {"type": "ok"})
        return super().__call__(argv, timeout)


class ReviewTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("HERDR_PANE_ID", None)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q"], ["-c", "user.name=Test", "-c", "user.email=test@example.com",
                                      "commit", "--allow-empty", "-qm", "init"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.config = Config(todos_dir=self.root / "todos")
        self.store = Store(self.config)
        self.board = self.store.init_board(repo_identity(self.repo))
        self.card = self.store.add(self.board, "Fix login timeout")
        self.fake = PaneHerdr(self.root)
        for module in (start, review):
            patcher = patch.object(module, "command", self.fake) if hasattr(module, "command") else None
            if patcher:
                patcher.start()
                self.addCleanup(patcher.stop)
        patcher = patch.object(review, "PANE_WAIT_SECONDS", 0.05)
        patcher.start()
        self.addCleanup(patcher.stop)
        start.register_checkout(self.repo)
        self.starter = start.Starter(self.store, self.repo)
        self.reviewer = review.Reviewer(self.store, self.repo)

    def record(self):
        return json.loads((start.state_dir() / "starts.json").read_text())[self.card.ref]

    def calls(self, *prefix):
        return [c for c in self.fake.calls if c[:len(prefix)] == list(prefix)]
