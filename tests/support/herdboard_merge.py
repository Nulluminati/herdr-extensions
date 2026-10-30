import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch
from herdboard import activity, merge, pr_write, start
from support.herdboard_review import PaneHerdr, ReviewTestCase
from support.git_env import isolated


class GitHerdr(PaneHerdr):
    """PaneHerdr whose worktrees are real Git worktrees, removed the way Herdr removes them."""

    def __call__(self, argv, timeout=120):
        args = argv[1:]
        operation = " ".join(args[:2])
        if operation == "worktree remove":
            self.calls.append(args)
            workspace = args[args.index("--workspace") + 1]
            entry = next((w for w in self.worktrees if w["open_workspace_id"] == workspace), None)
            if entry is None:
                return self.reply(argv, error="workspace_not_found")
            removed = subprocess.run(["git", "-C", entry["path"], "worktree", "remove", entry["path"]],
                                     capture_output=True)
            if removed.returncode:
                return self.reply(argv, error="dirty_worktree_requires_force")
            self.worktrees.remove(entry)
            self.panes.pop(workspace, None)
            self.agent = False
            return self.reply(argv, {"type": "worktree_removed", "workspace_id": workspace})
        if operation == "agent prompt":
            self.calls.append(args)
            return self.reply(argv, {"type": "ok"})
        result = super().__call__(argv, timeout)
        if operation == "worktree create":
            path = json.loads(result.stdout)["result"]["worktree"]["path"]
            branch = args[args.index("--branch") + 1]
            cwd = args[args.index("--cwd") + 1]
            subprocess.run(["git", "-C", cwd, "worktree", "add", path, branch], check=True, capture_output=True)
        return result


class MergeTestCase(ReviewTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(patch.dict(os.environ, isolated()))
        self.fake.__class__ = GitHerdr
        self.merger = merge.Merger(self.store, self.repo)

    def git(self, directory, *args):
        return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def commit(self, directory, name, text, message):
        (Path(directory) / name).write_text(text)
        self.git(directory, "add", name)
        self.git(directory, "commit", "-qm", message)
        return self.git(directory, "rev-parse", "HEAD")

    def started(self, base=None):
        result = self.starter.start(self.card.ref, agent="pi", base=base)
        self.worktree = Path(result["worktree_path"])
        self.branch = result["branch"]
        return result

    def main_branch(self):
        return self.git(self.repo, "symbolic-ref", "--short", "HEAD")

    def branch_exists(self, branch):
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "--verify", "--quiet",
                               f"refs/heads/{branch}"], capture_output=True).returncode == 0

    def records(self):
        path = start.state_dir() / "starts.json"
        return json.loads(path.read_text()) if path.exists() else {}

    def log(self):
        return [e["message"] for e in activity.read(self.config, self.card.ref)]


class FakeGh:
    """`gh` for pull requests: list, create, edit and view, with the URL of one pull request."""

    def __init__(self):
        self.calls = []
        self.existing = []
        self.state = "OPEN"
        self.missing = False
        self.url = "https://github.com/acme/api/pull/7"
        self.title = "fix: keep sessions for 30 minutes"
        self.body = "## Why\nSessions ended too soon.\n"
        # The text of each --body-file, read when gh runs.
        self.bodies = []

    def __call__(self, directory, *args):
        if self.missing:
            raise merge.MergeError("gh is not installed", "gh_unavailable")
        self.calls.append(list(args))
        if "--body-file" in args:
            self.bodies.append(Path(args[args.index("--body-file") + 1]).read_text())
        if args[:2] == ("pr", "list"):
            out = json.dumps([{"url": url} for url in self.existing])
        elif args[:2] == ("pr", "create"):
            out = self.url + "\n"
        elif args[:2] == ("pr", "edit"):
            out = self.url + "\n"
        elif args[:2] == ("pr", "view"):
            fields = args[args.index("--json") + 1].split(",") if "--json" in args else []
            if "title" in fields or "body" in fields:
                out = json.dumps({"title": self.title, "body": self.body})
            else:
                out = json.dumps({"state": self.state})
        else:
            raise AssertionError(args)
        return subprocess.CompletedProcess(["gh", *args], 0, out, "")


class PullRequestTestCase(MergeTestCase):
    def setUp(self):
        super().setUp()
        self.origin = self.root / "origin.git"
        subprocess.run(["git", "init", "-q", "--bare", str(self.origin)], check=True, capture_output=True)
        self.git(self.repo, "remote", "add", "origin", str(self.origin))
        self.git(self.repo, "push", "-q", "origin", self.main_branch())
        self.gh = FakeGh()
        self.enterContext(patch.object(merge, "_gh", self.gh))
        self.workers = []
        self.enterContext(patch.object(pr_write, "start_worker",
                                       lambda arguments, log: self.workers.append((arguments, log))))

    def remote_branches(self):
        return self.git(self.origin, "for-each-ref", "--format=%(refname:short)", "refs/heads")
