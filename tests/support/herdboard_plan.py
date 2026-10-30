import json
from pathlib import Path
import subprocess


class PlanHerdr:
    """Fake Herdr: worktree workspaces, a new plan tab, a planner agent on a script, and notifications."""

    def __init__(self, root=None):
        self.root = root
        self.calls = []
        # Each status poll takes the next state; None means the planner pane is gone, and "error" that
        # Herdr did not answer.
        self.states = ["working", "idle", None]
        # Called on each poll, before the state is returned: it can write the plan.
        self.on_poll = None
        # Called while the planner agent starts: it can write the plan.
        self.on_start = None
        # Each pane check takes the next answer; False means the pane is gone. The pane exists by default.
        self.panes = []
        # Panes and names of live work agents, which `start` recorded.
        self.work_agents = set()
        # Worktree workspaces that Herdr has open, as `start` records them.
        self.worktrees = []
        self.branches = {}
        # Pane labels by pane ID, from `pane rename`; such panes are in the plan tab.
        self.labels = {}

    def reply(self, argv, result=None, error=None):
        if error:
            return subprocess.CompletedProcess(argv, 1, "", json.dumps({"error": {"code": error, "message": error}}))
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": result or {}}), "")

    def __call__(self, argv, timeout=120):
        args = argv[1:]
        self.calls.append(args)
        operation = " ".join(args[:2])
        if operation == "tab create":
            return self.reply(argv, {"tab": {"tab_id": "w1:t4"}, "root_pane": {"pane_id": "w1:p7"}})
        if operation == "workspace list":
            return self.reply(argv, {"workspaces": []})
        if operation == "worktree list":
            return self.reply(argv, {"worktrees": self.worktrees})
        if operation in ("worktree create", "worktree open"):
            branch = args[args.index("--branch") + 1] if "--branch" in args else args[args.index("--path") + 1]
            if branch not in self.branches:
                self.branches[branch] = len(self.branches)
            index = self.branches[branch]
            path = self.root / ("work" if index == 0 else f"work-{index}")
            if "--path" in args:
                path = Path(args[args.index("--path") + 1])
            workspace = f"w{9 + index}"
            if operation == "worktree create":
                # A real worktree, so `git status` and the upstream work in the tests.
                cwd = args[args.index("--cwd") + 1]
                base = args[args.index("--base") + 1] if "--base" in args else "HEAD"
                subprocess.run(["git", "-C", cwd, "worktree", "add", "-qb", branch, str(path), base],
                               check=True, capture_output=True)
            result = {"workspace": {"workspace_id": workspace}, "root_pane": {"pane_id": workspace + ":p1"},
                      "worktree": {"path": str(path)}}
            self.worktrees = [w for w in self.worktrees if w["path"] != str(path)]
            self.worktrees.append({"branch": branch, "path": str(path), "open_workspace_id": workspace})
            return self.reply(argv, result)
        if operation == "workspace get":
            return self.reply(argv, {"workspace": {"workspace_id": args[2]},
                                     "root_pane": {"pane_id": args[2] + ":p1"}})
        if operation == "pane list":
            return self.reply(argv, {"panes": [{"pane_id": args[3] + ":p1"},
                                               *({"pane_id": pane, "tab_id": "w1:t4", "label": label}
                                                 for pane, label in self.labels.items())]})
        if operation == "pane split":
            return self.reply(argv, {"pane": {"pane_id": "w1:p8"}})
        if operation == "pane rename":
            self.labels[args[2]] = args[3]
        if operation == "agent get" and args[2] in self.work_agents:
            return self.reply(argv, {"agent": {"agent_status": "working", "pane_id": args[2]}})
        if operation == "agent get":
            if self.on_poll:
                self.on_poll()
            state = self.states.pop(0) if self.states else None
            if state is None:
                return self.reply(argv, error="agent_not_found")
            if state == "error":
                return self.reply(argv, error="server_unavailable")
            return self.reply(argv, {"agent": {"agent_status": state, "pane_id": args[2]}})
        if operation == "pane get":
            if not (self.panes.pop(0) if self.panes else True):
                return self.reply(argv, error="pane_not_found")
            return self.reply(argv, {"pane": {"pane_id": args[2], "tab_id": "w1:t4", "workspace_id": "w1"}})
        if operation == "workspace create":
            return self.reply(argv, {"workspace": {"workspace_id": "x1"},
                                     "root_pane": {"pane_id": "x1:p1"}})
        if operation == "workspace close":
            return self.reply(argv, {"type": "ok"})
        if operation == "agent start" and self.on_start:
            self.on_start()
        return self.reply(argv, {"type": "ok"})

    def find(self, *prefix):
        return [call for call in self.calls if call[:len(prefix)] == list(prefix)]
