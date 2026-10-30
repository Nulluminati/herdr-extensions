import json
import subprocess
from pathlib import Path


class FakeHerdr:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.worktrees = []
        self.fail = None
        self.agent = False
        self.branches = {}
        self.closed = set()
        self.created = 0

    def __call__(self, argv, timeout=120):
        args = argv[1:]
        self.calls.append(args)
        operation = " ".join(args[:2])
        if self.fail == operation:
            self.fail = None
            return subprocess.CompletedProcess(
                argv, 1, "", json.dumps({"error": {"code": "broken", "message": "test failure"}})
            )
        result = {}
        if operation == "workspace list":
            result = {
                "workspaces": [
                    {"workspace_id": w["open_workspace_id"], "worktree": {"checkout_path": w["path"]}}
                    for w in self.worktrees
                    if isinstance(w.get("open_workspace_id"), str) and w["open_workspace_id"] not in self.closed
                ]
            }
        elif operation == "agent list":
            result = {"agents": [{"name": None, "pane_id": "w9:p1", "workspace_id": "w9"}] if self.agent else []}
        elif operation == "worktree list":
            result = {"worktrees": self.worktrees}
        elif operation in ("worktree create", "worktree open"):
            branch = args[args.index("--branch") + 1] if "--branch" in args else args[args.index("--path") + 1]
            if branch not in self.branches:
                self.branches[branch] = len(self.branches)
            index = self.branches[branch]
            path = self.root / ("work" if index == 0 else f"work-{index}")
            if "--path" in args:
                path = Path(args[args.index("--path") + 1])
            workspace = f"w{9 + index}"
            path.mkdir(exist_ok=True)
            if operation == "worktree create":
                # Herdr creates the branch from --base (else the checkout's branch) when it is new.
                cwd = args[args.index("--cwd") + 1]
                base = args[args.index("--base") + 1] if "--base" in args else "HEAD"
                subprocess.run(["git", "-C", cwd, "branch", branch, base], capture_output=True)
            result = {
                "workspace": {"workspace_id": workspace},
                "root_pane": {"pane_id": workspace + ":p1"},
                "worktree": {"path": str(path)},
            }
            self.worktrees = [w for w in self.worktrees if w["path"] != str(path)]
            self.worktrees.append({"branch": branch, "path": str(path), "open_workspace_id": workspace})
        elif operation == "workspace get":
            if args[2] in self.closed:
                return subprocess.CompletedProcess(argv, 1, "", '{"error":{"code":"workspace_not_found"}}')
            result = {"workspace": {"workspace_id": args[2]}, "root_pane": {"pane_id": args[2] + ":p1"}}
        elif operation == "workspace create":
            self.created += 1
            workspace = f"x{self.created}"
            result = {"workspace": {"workspace_id": workspace}, "root_pane": {"pane_id": workspace + ":p1"}}
        elif operation == "workspace close":
            self.closed.add(args[2])
            result = {"type": "ok"}
        elif operation == "agent get":
            if not self.agent:
                return subprocess.CompletedProcess(argv, 1, "", '{"error":{"code":"agent_not_found"}}')
            result = {"agent": {"agent_status": "working", "pane_id": "w9:p1"}}
        elif operation == "agent start":
            self.agent = True
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": result}), "")
