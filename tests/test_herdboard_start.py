"""Start work with an isolated store and fake Herdr."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/herdboard/src"))
from herdboard.config import Config, config_path
from herdboard.identity import repo_identity
from herdboard.store import Store
from herdboard import start
from test_herdboard_cli import CliTestCase


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
            return subprocess.CompletedProcess(argv, 1, "", json.dumps({"error": {"code": "broken", "message": "test failure"}}))
        result = {}
        if operation == "workspace list":
            result = {"workspaces": []}
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
            result = {"workspace": {"workspace_id": workspace}, "root_pane": {"pane_id": workspace + ":p1"}, "worktree": {"path": str(path)}}
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


class StartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.repo = self.root / "repo"
        self.repo.mkdir()
        for args in (["init", "-q"], ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "init"]):
            subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True)
        self.config = Config(todos_dir=self.root / "todos")
        self.store = Store(self.config)
        self.board = self.store.init_board(repo_identity(self.repo))
        self.card = self.store.add(self.board, "Fix login timeout #bug 📅 2026-10-20 [[note]]")
        self.fake = FakeHerdr(self.root)
        self.runner = patch.object(start, "command", self.fake)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        start.register_checkout(self.repo)
        self.starter = start.Starter(self.store, self.repo)

    def test_create_move_and_reuse_live_agent(self):
        result = self.starter.start(self.card.ref)
        self.assertEqual(result["branch"], self.card.ref + "-fix-login-timeout")
        self.assertEqual(self.store.show(self.card.ref).column, "In progress")
        self.assertEqual(result["pane_id"], "w9:p1")
        again = self.starter.start(self.card.ref)
        self.assertEqual(result["worktree_path"], again["worktree_path"])
        self.assertEqual(sum(c[:2] == ["agent", "start"] for c in self.fake.calls), 1)
        self.assertEqual(sum(c[:2] == ["worktree", "create"] for c in self.fake.calls), 1)

    def test_start_keeps_the_user_where_they_are(self):
        self.starter.start(self.card.ref)
        self.starter.start(self.card.ref)
        self.assertFalse(any(c[:2] == ["workspace", "focus"] for c in self.fake.calls))
        opened = [c for c in self.fake.calls if c[:2] in (["worktree", "create"], ["worktree", "open"])]
        self.assertTrue(opened)
        self.assertTrue(all("--no-focus" in c and "--focus" not in c for c in opened))

    def test_no_repo_keeps_a_live_worktree(self):
        result = self.starter.start(self.card.ref, no_agent=True)
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(self.card.ref, no_repo=True, no_agent=True)
        self.assertEqual(raised.exception.code, "usage")
        self.assertIn(result["worktree_path"], str(raised.exception))
        record = json.loads((start.state_dir() / "starts.json").read_text())[self.card.ref]
        self.assertEqual(record.get("mode"), "repo")
        self.assertEqual(record["branch"], result["branch"])
        self.assertTrue(Path(result["worktree_path"]).exists())

    def test_new_repo_keeps_a_live_worktree_of_an_inbox_card(self):
        card = self.store.add(self.store.ensure_inbox(), "Global task")
        result = self.starter.start(card.ref, repo=self.board.stem, no_agent=True)
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(card.ref, new_repo=self.root / "new", no_agent=True)
        self.assertEqual(raised.exception.code, "usage")
        self.assertIn(result["worktree_path"], str(raised.exception))
        record = json.loads((start.state_dir() / "starts.json").read_text())[card.ref]
        self.assertEqual(record["branch"], result["branch"])
        self.assertFalse((self.root / "new").exists())

    def test_go_to_workspace_focuses_the_open_workspace(self):
        self.starter.start(self.card.ref, no_agent=True)
        self.fake.calls.clear()
        result = start.go_to_workspace(self.card.ref)
        self.assertEqual(result, {"ref": self.card.ref, "workspace_id": "w9"})
        self.assertIn(["workspace", "focus", "w9"], self.fake.calls)

    def test_go_to_workspace_stops_for_a_card_without_work(self):
        with self.assertRaises(start.StartError) as caught:
            start.go_to_workspace(self.card.ref)
        self.assertEqual(caught.exception.code, "not_started")
        self.assertEqual(self.fake.calls, [])

    def test_go_to_workspace_stops_when_herdr_has_no_open_workspace(self):
        self.starter.start(self.card.ref, no_agent=True)
        for worktree in self.fake.worktrees:
            worktree["open_workspace_id"] = None
        self.fake.calls.clear()
        with self.assertRaises(start.StartError) as caught:
            start.go_to_workspace(self.card.ref)
        self.assertEqual(caught.exception.code, "workspace_closed")
        self.assertFalse(any(c[:2] == ["workspace", "focus"] for c in self.fake.calls))

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def upstream(self, branch):
        return self.git("rev-parse", "--abbrev-ref", branch + "@{u}")

    def create_call(self):
        return next(c for c in self.fake.calls if c[:2] == ["worktree", "create"])

    def test_base_defaults_to_the_checkout_branch(self):
        checkout_branch = self.git("symbolic-ref", "--short", "HEAD")
        result = self.starter.start(self.card.ref, no_agent=True)
        self.assertEqual(result["base"], checkout_branch)
        call = self.create_call()
        self.assertEqual(call[call.index("--base") + 1], checkout_branch)
        self.assertEqual(self.upstream(result["branch"]), checkout_branch)
        record = json.loads((start.state_dir() / "starts.json").read_text())[self.card.ref]
        self.assertEqual(record["base"], checkout_branch)

    def test_explicit_base_is_the_upstream(self):
        self.git("branch", "feature-x")
        result = self.starter.start(self.card.ref, no_agent=True, base="feature-x")
        self.assertEqual(result["base"], "feature-x")
        call = self.create_call()
        self.assertEqual(call[call.index("--base") + 1], "feature-x")
        self.assertEqual(self.upstream(result["branch"]), "feature-x")

    def test_unknown_base_fails_before_the_worktree(self):
        for base in ("nope", "origin/main", self.card.ref + "-fix-login-timeout"):
            with self.subTest(base=base), self.assertRaises(start.StartError) as raised:
                self.starter.start(self.card.ref, no_agent=True, base=base)
            self.assertEqual(raised.exception.code, "base_unknown")
        self.assertFalse(any(c[:2] == ["worktree", "create"] for c in self.fake.calls))

    def test_a_detached_checkout_needs_an_explicit_base(self):
        self.git("checkout", "-q", "--detach")
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(self.card.ref, no_agent=True)
        self.assertEqual(raised.exception.code, "base_unknown")
        self.assertIn("--base", str(raised.exception))

    def test_a_restart_keeps_the_recorded_base(self):
        self.git("branch", "feature-x")
        result = self.starter.start(self.card.ref, no_agent=True, base="feature-x")
        again = self.starter.start(self.card.ref, no_agent=True)
        self.assertEqual(again["base"], "feature-x")
        self.assertEqual(self.upstream(result["branch"]), "feature-x")

    def test_a_restart_with_another_base_changes_the_upstream(self):
        checkout_branch = self.git("symbolic-ref", "--short", "HEAD")
        self.git("branch", "feature-x")
        result = self.starter.start(self.card.ref, no_agent=True, base="feature-x")
        again = self.starter.start(self.card.ref, no_agent=True, base=checkout_branch)
        self.assertEqual(again["base"], checkout_branch)
        self.assertEqual(self.upstream(result["branch"]), checkout_branch)

    def test_an_existing_branch_gets_the_upstream(self):
        branch = start.todo_branch(self.card)
        self.git("branch", branch)
        result = self.starter.start(self.card.ref, no_agent=True)
        self.assertEqual(self.upstream(branch), result["base"])

    def test_the_record_keeps_the_agent_kind(self):
        self.starter.start(self.card.ref, agent="pi")
        record = json.loads((start.state_dir() / "starts.json").read_text())[self.card.ref]
        self.assertEqual(record["agent"], "pi")

    def test_stored_closed_workspace_opens(self):
        self.starter.start(self.card.ref, no_agent=True)
        self.fake.worktrees[0]["open_workspace_id"] = None
        self.starter.start(self.card.ref, no_agent=True)
        self.assertTrue(any(c[:2] == ["worktree", "open"] for c in self.fake.calls))

    def test_start_after_a_plan_reuses_the_worktree_and_its_root_pane(self):
        branch = start.todo_branch(self.card)
        base = self.git("symbolic-ref", "--short", "HEAD")
        path = self.root / "work"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-qb", branch, str(path), base],
                       check=True, capture_output=True)
        start._write(start.state_dir() / "starts.json", {self.card.ref: {
            "ref": self.card.ref, "repository": repo_identity(self.repo).identity, "checkout": str(self.repo),
            "branch": branch, "base": base, "workspace_id": "w9", "worktree_path": str(path),
            "steps_done": ["repository", "checkout", "branch", "base", "worktree", "upstream"]}})
        self.fake.worktrees = [{"branch": branch, "path": str(path), "open_workspace_id": "w9"}]
        self.fake.calls.clear()
        result = self.starter.start(self.card.ref)
        self.assertEqual(result["worktree_path"], str(path))
        self.assertFalse(any(c[:2] == ["worktree", "create"] for c in self.fake.calls))
        self.assertEqual(self.store.show(self.card.ref).column, "In progress")
        agent = next(c for c in self.fake.calls if c[:2] == ["agent", "start"])
        self.assertEqual(agent[agent.index("--pane") + 1], "w9:p1", "the agent takes the root pane, not the plan tab")

    def test_existing_git_worktree_opens(self):
        path = self.root / "existing"
        branch = start.todo_branch(self.card)
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-qb", branch, str(path)], check=True, capture_output=True)
        result = self.starter.start(self.card.ref, no_agent=True)
        self.assertEqual(result["worktree_path"], str(path))
        self.assertFalse(any(c[:2] == ["worktree", "create"] for c in self.fake.calls))
        start.register_checkout(path)
        entries = json.loads((start.state_dir() / "checkouts.json").read_text())[repo_identity(self.repo).identity]
        self.assertEqual([e["path"] for e in entries], [str(self.repo)])

    def test_resume_after_agent_failure(self):
        self.fake.fail = "agent start"
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(self.card.ref)
        self.assertIn("move", raised.exception.steps_done)
        self.starter.start(self.card.ref)
        self.assertEqual(sum(c[:2] == ["worktree", "create"] for c in self.fake.calls), 1)

    def test_create_failure_preserves_progress(self):
        self.fake.fail = "worktree create"
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(self.card.ref)
        self.assertEqual(raised.exception.steps_done, ["repository", "checkout", "branch", "base"])
        self.starter.start(self.card.ref, no_agent=True)

    def test_global_requires_repo_even_inside_repository(self):
        card = self.store.add(self.store.ensure_inbox(), "Global task")
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(card.ref, no_agent=True)
        self.assertEqual(raised.exception.code, "repo_required")
        result = self.starter.start(card.ref, repo=self.board.stem, no_agent=True)
        self.assertEqual(result["workspace_id"], "w9")

    def test_a_typed_todo_branch_starts_with_its_commit_type(self):
        for kind, prefix in (("bug", "fix/"), ("feature", "feat/"), ("chore", "chore/"), ("spike", "spike/")):
            with self.subTest(kind=kind):
                card = self.store.add(self.board, f"Fix login #type/{kind} #auth")
                self.assertEqual(start.todo_branch(card), f"{prefix}{card.ref}-fix-login")
        self.assertLessEqual(len(start.todo_branch(self.store.add(self.board, "long " * 30 + "#type/refactor"))), 50)

    def test_invalid_branch_and_slug_limit(self):
        self.assertLessEqual(len(start.todo_branch(self.store.add(self.board, "long " * 30))), 50)
        with self.assertRaises(start.StartError) as raised:
            start.validate_branch("bad..branch")
        self.assertEqual(raised.exception.code, "invalid_branch")

    def test_registry_stores_main_and_latest_existing(self):
        start.register_checkout(self.repo)
        data = json.loads((start.state_dir() / "checkouts.json").read_text())
        paths = data[repo_identity(self.repo).identity]
        self.assertEqual(paths[0]["path"], str(self.repo))
        self.assertIn("last_seen", paths[0])

    def test_removed_worktree_marks_card_without_move(self):
        self.starter.start(self.card.ref, no_agent=True)
        (self.root / "work").rmdir()
        card = self.store.show(self.card.ref)
        start.mark_removed([card])
        self.assertTrue(card.worktree_removed)
        self.assertFalse(card.worktree)
        self.assertEqual(card.column, "In progress")

    def reviewed_record(self, head):
        """A started card whose worktree is the test repository, with a saved agent review of `head`."""
        self.starter.start(self.card.ref, no_agent=True)
        path = start.state_dir() / "starts.json"
        records = json.loads(path.read_text())
        records[self.card.ref].update(worktree_path=str(self.repo),
                                      agent_reviewed={"head": head, "findings": "findings.md"})
        path.write_text(json.dumps(records))

    def head(self):
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
                              capture_output=True, text=True).stdout.strip()

    def test_a_saved_review_of_the_branch_head_marks_the_card_reviewed(self):
        self.reviewed_record(self.head())
        card = self.store.show(self.card.ref)
        start.mark_removed([card])
        self.assertEqual(card.reviewed, "reviewed")

    def test_commits_after_the_review_mark_new_commits(self):
        self.reviewed_record(self.head())
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test", "-c", "user.email=test@example.com",
                        "commit", "--allow-empty", "-qm", "more"], check=True, capture_output=True)
        card = self.store.show(self.card.ref)
        start.mark_removed([card])
        self.assertEqual(card.reviewed, "new commits")

    def test_a_card_without_a_saved_review_or_a_git_error_is_not_reviewed(self):
        self.starter.start(self.card.ref, no_agent=True)
        card = self.store.show(self.card.ref)
        card.reviewed = "reviewed"
        start.mark_removed([card])
        self.assertIsNone(card.reviewed)
        path = start.state_dir() / "starts.json"
        records = json.loads(path.read_text())
        records[self.card.ref]["agent_reviewed"] = {"head": "abc", "findings": "findings.md"}
        path.write_text(json.dumps(records))
        # The fake worktree is not a Git repository.
        start.mark_removed([card])
        self.assertIsNone(card.reviewed)

    def test_resume_does_not_repeat_a_completed_move(self):
        self.fake.fail = "agent start"
        with self.assertRaises(start.StartError):
            self.starter.start(self.card.ref)
        self.store.move(self.card.ref, "Review")
        with patch.object(self.store, "move", wraps=self.store.move) as move:
            self.starter.start(self.card.ref)
        move.assert_not_called()

    def test_resume_after_move_failure(self):
        from herdboard.model import ConflictError
        with patch.object(self.store, "move", side_effect=ConflictError("changed")):
            with self.assertRaises(ConflictError) as raised:
                self.starter.start(self.card.ref)
        self.assertIn("worktree", raised.exception.steps_done)
        self.starter.start(self.card.ref)
        self.assertEqual(sum(c[:2] == ["worktree", "create"] for c in self.fake.calls), 1)

    def test_temporary_ref_assigns_all_ids_without_reordering(self):
        self.board.write_text(self.board.read_text().replace(" ^" + self.card.ref, ""))
        result = self.starter.start(self.board.stem + ":1", no_agent=True)
        self.assertRegex(result["ref"], r"^t[0-9a-z]{3}$")
        self.assertTrue(result["branch"].startswith(result["ref"]))
        self.assertNotIn(self.board.stem + ":1", json.loads((start.state_dir() / "starts.json").read_text()))

    def test_temporary_reference_for_identified_card_reuses_id_record(self):
        self.starter.start(self.card.ref, no_agent=True)
        self.starter.start(self.board.stem + ":1", no_agent=True)
        records = json.loads((start.state_dir() / "starts.json").read_text())
        self.assertEqual(list(records), [self.card.ref])
        self.assertEqual(sum(c[:2] == ["worktree", "create"] for c in self.fake.calls), 1)

    def test_unknown_checkout_prompts_or_errors(self):
        (start.state_dir() / "checkouts.json").unlink()
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(self.card.ref, no_agent=True)
        self.assertEqual(raised.exception.code, "checkout_unknown")
        prompt = unittest.mock.Mock(return_value=str(self.repo))
        starter = start.Starter(self.store, self.repo, prompt_checkout=prompt)
        starter.start(self.card.ref, no_agent=True)
        prompt.assert_called_once_with(repo_identity(self.repo).identity)

    def test_explicit_checkout_is_remembered_and_mismatch_refused(self):
        (start.state_dir() / "checkouts.json").unlink()
        self.starter.start(self.card.ref, checkout=self.repo, no_agent=True)
        self.assertTrue((start.state_dir() / "checkouts.json").exists())
        with self.assertRaises(start.StartError):
            self.starter.start(self.card.ref, checkout=self.root, no_agent=True)

    def test_most_recent_existing_checkout_wins(self):
        other = self.root / "other"
        other.mkdir()
        for path in (self.repo, other):
            if path == other:
                subprocess.run(["git", "-C", str(path), "init", "-q"], check=True, capture_output=True)
            subprocess.run(["git", "-C", str(path), "remote", "add", "origin", "https://github.com/acme/api"], check=True, capture_output=True)
        start.register_checkout(self.repo)
        start.register_checkout(other)
        hosted = repo_identity(self.repo).identity
        self.assertEqual(self.starter._checkout(hosted, None), other)
        import shutil
        shutil.rmtree(other)
        self.assertEqual(self.starter._checkout(hosted, None), self.repo)

    def test_herdr_unavailable_does_not_change_todo(self):
        with patch.object(start, "command", side_effect=FileNotFoundError("herdr")):
            with self.assertRaises(start.StartError) as raised:
                self.starter.start(self.card.ref)
        self.assertEqual(raised.exception.code, "herdr_unavailable")
        self.assertEqual(self.store.show(self.card.ref).column, "Backlog")

    def test_unreachable_server_is_herdr_unavailable(self):
        for code in ("server_unavailable", "server_not_running"):
            with self.subTest(code=code), patch.object(start, "command", return_value=subprocess.CompletedProcess([], 1, "", json.dumps({"error": {"code": code, "message": "Server is unreachable"}}))):
                with self.assertRaises(start.StartError) as raised:
                    self.starter.start(self.card.ref)
                self.assertEqual(raised.exception.code, "herdr_unavailable")

    def test_one_start_lock(self):
        import fcntl
        from herdboard.model import ConflictError
        with (start.state_dir() / (".start-" + self.card.ref + ".lock")).open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaises(ConflictError):
                self.starter.start(self.card.ref)
        self.assertEqual(self.fake.calls, [])

    def test_removed_worktree_replaces_record(self):
        self.starter.start(self.card.ref)
        (self.root / "work").rmdir()
        self.fake.worktrees = []
        self.fake.agent = False
        self.starter.start(self.card.ref, no_agent=True)
        self.assertEqual(sum(c[:2] == ["worktree", "create"] for c in self.fake.calls), 2)
        card = self.store.show(self.card.ref)
        start.mark_removed([card])
        self.assertFalse(card.worktree_removed)
        self.assertTrue(card.worktree)

    def test_agent_message_is_safe_for_herdr_shell_encoding(self):
        self.starter.start(self.card.ref)
        call = next(c for c in self.fake.calls if c[:2] == ["agent", "start"])
        message = call[-1]
        self.assertNotIn("\n", message)
        self.assertNotIn("\r", message)
        self.assertIn(self.card.ref, message)
        self.assertIn("Fix login timeout", message)
        self.assertIn("Read the brief at", message)

    def test_no_move_when_already_started(self):
        self.store.move(self.card.ref, "In progress")
        with patch.object(self.store, "move") as move:
            self.starter.start(self.card.ref, no_agent=True)
        move.assert_not_called()

    def test_timeout_and_not_ready_are_success(self):
        normal = self.fake
        for code in ("timeout", "agent_not_ready"):
            with self.subTest(code=code):
                card = self.store.add(self.board, code)
                def runner(argv, timeout=120):
                    if argv[1:3] == ["agent", "start"]:
                        return subprocess.CompletedProcess(argv, 1, "", json.dumps({"error": {"code": code}}))
                    return normal(argv, timeout)
                with patch.object(start, "command", runner):
                    result = self.starter.start(card.ref)
                self.assertIn("agent", result["steps_done"])

    def test_registration_skips_held_lock_and_corrupt_registry(self):
        import fcntl
        with (start.state_dir() / ".starts.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            start.register_checkout(self.repo)
        (start.state_dir() / "checkouts.json").write_text("bad json")
        start.register_checkout(self.repo)

    def test_malformed_registry_entries_are_ignored(self):
        identity = repo_identity(self.repo).identity
        start._write(start.state_dir() / "checkouts.json", {identity: [{}, {"path": 7}, {"path": str(self.repo), "last_seen": 1}]})
        self.assertEqual(self.starter._checkout(identity, None), self.repo)
        start.register_checkout(self.repo)

    def test_registry_lock_is_free_during_herdr_and_other_card_start(self):
        other = self.store.add(self.board, "Other task")
        normal = self.fake
        checked = False
        def runner(argv, timeout=120):
            nonlocal checked
            if argv[1:3] == ["worktree", "create"] and not checked:
                checked = True
                with start._lock():
                    pass
                second = self.starter.start(other.ref, no_agent=True)
                self.assertEqual(second["workspace_id"], "w9")
            return normal(argv, timeout)
        with patch.object(start, "command", runner):
            result = self.starter.start(self.card.ref, no_agent=True)
        self.assertEqual(result["workspace_id"], "w10")
        self.assertEqual(set(start._read(start.state_dir() / "starts.json")), {self.card.ref, other.ref})

    def test_resume_prefers_record_checkout_over_newest(self):
        other = self.root / "other"
        subprocess.run(["git", "clone", "-q", str(self.repo), str(other)], check=True, capture_output=True)
        for path in (self.repo, other):
            subprocess.run(["git", "-C", str(path), "remote", "remove", "origin"], capture_output=True)
            subprocess.run(["git", "-C", str(path), "remote", "add", "origin", "https://github.com/acme/api"], check=True, capture_output=True)
        self.board.write_text(self.board.read_text().replace(str(self.repo), "github.com/acme/api"))
        start.register_checkout(self.repo)
        self.fake.fail = "worktree create"
        with self.assertRaises(start.StartError):
            self.starter.start(self.card.ref, no_agent=True)
        start.register_checkout(other)
        self.starter.start(self.card.ref, no_agent=True)
        call = [c for c in self.fake.calls if c[:2] == ["worktree", "create"]][-1]
        self.assertEqual(call[call.index("--cwd") + 1], str(self.repo))
        (self.root / "work").rmdir()
        self.fake.worktrees = []
        self.starter.start(self.card.ref, no_agent=True)
        call = [c for c in self.fake.calls if c[:2] == ["worktree", "create"]][-1]
        self.assertEqual(call[call.index("--cwd") + 1], str(self.repo))

    def test_prunable_git_worktree_creates_after_prune(self):
        import shutil
        path = self.root / "dead"
        subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "-qb", start.todo_branch(self.card), str(path)], check=True, capture_output=True)
        shutil.rmtree(path)
        self.starter.start(self.card.ref, no_agent=True)
        self.assertTrue(any(c[:2] == ["worktree", "create"] for c in self.fake.calls))
        self.assertNotIn(str(path), start._git(self.repo, "worktree", "list", "--porcelain"))

    def test_complete_run_moves_again(self):
        self.starter.start(self.card.ref, no_agent=True)
        records = start._read(start.state_dir() / "starts.json")
        self.assertEqual(records[self.card.ref]["steps_done"], [])
        self.store.move(self.card.ref, "Review")
        self.starter.start(self.card.ref, no_agent=True)
        self.assertEqual(self.store.show(self.card.ref).column, "In progress")

    def test_start_column_validation_precedes_worktree(self):
        self.config.start_column = "Missing"
        with self.assertRaises(start.HerdboardError):
            self.starter.start(self.card.ref, no_agent=True)
        self.assertFalse(any(c[:2] == ["worktree", "create"] for c in self.fake.calls))

    def test_ensure_id_failure_writes_no_record(self):
        self.board.write_text(self.board.read_text().replace(" ^" + self.card.ref, ""))
        with patch.object(self.store, "ensure_id", side_effect=start.IoError("readonly")):
            with self.assertRaises(start.IoError):
                self.starter.start(self.board.stem + ":1", no_agent=True)
        self.assertEqual(start._read(start.state_dir() / "starts.json"), {})

    def test_corrupt_starts_is_io_error(self):
        (start.state_dir() / "starts.json").write_text("bad json")
        with self.assertRaises(start.IoError):
            self.starter.start(self.card.ref)

    def test_malformed_start_record_is_io_error(self):
        start._write(start.state_dir() / "starts.json", {self.card.ref: []})
        with self.assertRaises(start.IoError):
            self.starter.start(self.card.ref)

    def test_the_agent_gets_one_line_and_a_brief_file(self):
        body = ['$(touch x) `touch y` single\' double" \x7f', "a" * 5000]
        self.store.move(self.card.ref, "In progress")
        card = self.store.show(self.card.ref)
        card.body = ["\t" + line for line in body]
        card.title = "Fix $(touch z) the build"
        with patch.object(self.store, "show", return_value=card):
            self.starter.start(self.card.ref, agent="pi")
        call = next(c for c in self.fake.calls if c[:2] == ["agent", "start"])
        self.assertEqual(call[:-1], ["agent", "start", "hb-" + self.card.ref,
                                    "--kind", "pi", "--pane", "w9:p1", "--", "--"])
        message = call[-1]
        brief = start.state_dir() / "briefs" / f"{self.card.ref}.md"
        self.assertIn(f"Start work on herdboard card {self.card.ref}: Fix $(touch z) the build.", message)
        self.assertIn(str(brief), message)
        self.assertNotIn("\n", message)
        text = brief.read_text()
        self.assertIn(body[0], text)
        self.assertIn("a" * 5000, text, "the brief keeps long details whole")
        self.assertIn(f"Branch: `{self.starter_branch()}`", text)
        self.assertIn(f"--json move {self.card.ref} Review", text)

    def test_the_brief_has_the_plan_and_the_attachments(self):
        from herdboard import attachments
        source = self.root / "spec.md"
        source.write_text("1. Raise the timeout.\n")
        attachments.add(self.store, self.card.ref, source, name="plan.md")
        attachments.add(self.store, self.card.ref, source)
        self.starter.start(self.card.ref)
        text = (start.state_dir() / "briefs" / f"{self.card.ref}.md").read_text()
        self.assertIn("## Plan\n\nYou and the user agreed", text)
        self.assertIn("1. Raise the timeout.\n", text)
        self.assertIn(str(attachments.folder(self.config, self.card.ref) / "spec.md"), text)

    def test_a_plan_that_cannot_be_read_does_not_stop_the_start(self):
        from herdboard import activity, attachments
        plan = attachments.plan_path(self.config, self.card.ref)
        plan.parent.mkdir(parents=True)
        plan.write_bytes(b"\xff\xfe")
        self.starter.start(self.card.ref)
        text = (start.state_dir() / "briefs" / f"{self.card.ref}.md").read_text()
        self.assertNotIn("## Plan", text)
        self.assertTrue(any(entry["message"].startswith("The agent does not get the plan")
                            for entry in activity.read(self.config, self.card.ref)))

    def starter_branch(self):
        return start._read(start.state_dir() / "starts.json")[self.card.ref]["branch"]

    def test_the_brief_command_works_outside_uv_run(self):
        venv_script = str(Path(sys.prefix) / "bin" / "herdboard")
        with patch.object(start.shutil, "which", return_value=venv_script):
            self.assertEqual(start.herdboard_command(), f"{start.shlex.quote(sys.executable)} -m herdboard")
        with patch.object(start.shutil, "which", return_value=None):
            self.assertEqual(start.herdboard_command(), f"{start.shlex.quote(sys.executable)} -m herdboard")
        with patch.object(start.shutil, "which", return_value="/home/n/.local/bin/herdboard"):
            self.assertEqual(start.herdboard_command(), "herdboard")

    def test_the_brief_command_names_a_config_file_that_is_not_the_default(self):
        custom = self.root / "my config.toml"
        with patch.object(start.shutil, "which", return_value="/home/n/.local/bin/herdboard"):
            self.assertEqual(start.herdboard_command(custom), f"herdboard --config {start.shlex.quote(str(custom))}")
            self.assertEqual(start.herdboard_command(config_path()), "herdboard")
            self.assertEqual(start.herdboard_command(None), "herdboard")
            # An agent that Herdr starts may not have HERDBOARD_CONFIG, so the command names that file.
            with patch.dict(os.environ, {"HERDBOARD_CONFIG": str(custom)}):
                self.assertEqual(start.herdboard_command(config_path()),
                                 f"herdboard --config {start.shlex.quote(str(custom))}")

    def test_the_brief_uses_the_starter_config_file(self):
        custom = self.root / "custom.toml"
        start.Starter(self.store, self.repo, config_file=custom).start(self.card.ref)
        text = (start.state_dir() / "briefs" / f"{self.card.ref}.md").read_text()
        self.assertIn(f"--config {custom} --json move {self.card.ref} Review", text)

    def test_the_agent_pane_exports_a_config_file_that_is_not_the_default(self):
        custom = self.root / "my config.toml"
        start.Starter(self.store, self.repo, config_file=custom).start(self.card.ref)
        export = ["pane", "run", "w9:p1", f"export HERDBOARD_CONFIG={start.shlex.quote(str(custom))}"]
        self.assertIn(export, self.fake.calls)
        # The shell has the variable before Herdr starts the agent in it.
        agent = next(i for i, c in enumerate(self.fake.calls) if c[:2] == ["agent", "start"])
        self.assertLess(self.fake.calls.index(export), agent)

    def test_the_agent_pane_exports_no_default_config_file(self):
        start.Starter(self.store, self.repo, config_file=config_path()).start(self.card.ref)
        self.starter.start(self.card.ref)
        self.assertFalse(any(c[:2] == ["pane", "run"] for c in self.fake.calls))

    def test_a_failed_export_does_not_stop_the_start(self):
        custom = self.root / "custom.toml"
        real = self.fake.__call__

        def broken(argv, timeout=120):
            if argv[1:3] == ["pane", "run"]:
                raise OSError("no herdr")
            return real(argv, timeout)

        with patch.object(start, "command", broken):
            start.Starter(self.store, self.repo, config_file=custom).start(self.card.ref)
        self.assertTrue(any(c[:2] == ["agent", "start"] for c in self.fake.calls))

    def agent_args(self):
        call = next(c for c in self.fake.calls if c[:2] == ["agent", "start"])
        return call[call.index("--pane") + 3:]

    def test_pi_gets_the_model_and_thinking_level(self):
        self.starter.start(self.card.ref, agent="pi", model="openai-codex/gpt-6.1-sol", thinking="medium")
        args = self.agent_args()
        self.assertEqual(args[:5], ["--model", "openai-codex/gpt-6.1-sol", "--thinking", "medium", "--"])
        self.assertTrue(args[5].startswith("Start work on herdboard card"))

    def test_claude_gets_the_model_after_its_prompt(self):
        self.starter.start(self.card.ref, agent="claude", model="opus")
        args = self.agent_args()
        self.assertTrue(args[0].startswith("Start work on herdboard card"))
        self.assertEqual(args[1:], ["--model", "opus"])

    def test_thinking_is_only_for_pi(self):
        with self.assertRaises(start.StartError) as caught:
            self.starter.start(self.card.ref, agent="claude", thinking="high")
        self.assertEqual(caught.exception.code, "usage")

    def test_bad_thinking_level_or_model(self):
        for kwargs in ({"thinking": "huge"}, {"model": "two words"}):
            with self.subTest(**kwargs), self.assertRaises(start.StartError):
                self.starter.start(self.card.ref, agent="pi", **kwargs)

    def test_config_defaults_for_the_model(self):
        self.store.config.start_model = "zai/glm-5.3"
        self.store.config.start_thinking = "low"
        self.starter.start(self.card.ref, agent="pi")
        self.assertEqual(self.agent_args()[:4], ["--model", "zai/glm-5.3", "--thinking", "low"])

    def test_no_model_keeps_the_agents_own_default(self):
        self.starter.start(self.card.ref, agent="pi")
        self.assertEqual(self.agent_args()[0], "--")

    def test_pi_model_choices_come_from_pi_settings(self):
        home = Path(self.root) / "home"
        (home / ".pi/agent").mkdir(parents=True)
        (home / ".pi/agent/settings.json").write_text(json.dumps({
            "enabledModels": ["zai/glm-5.3", "openai-codex/gpt-6.1-sol"],
            "defaultProvider": "openai-codex", "defaultModel": "gpt-6.1-sol", "defaultThinkingLevel": "high",
        }))
        with patch.dict(os.environ, {"HOME": str(home)}):
            default, models = start.pi_model_choices(self.store.config)
        self.assertEqual(default, "openai-codex/gpt-6.1-sol, high")
        self.assertEqual(models, ["zai/glm-5.3", "openai-codex/gpt-6.1-sol"])
        self.store.config.pi_models = ["a/b"]
        with patch.dict(os.environ, {"HOME": str(home)}):
            self.assertEqual(start.pi_model_choices(self.store.config)[1], ["a/b"])

    def test_pi_model_choices_without_pi_settings(self):
        with patch.dict(os.environ, {"HOME": str(Path(self.root) / "nohome")}):
            self.assertEqual(start.pi_model_choices(self.store.config), (None, []))

    def test_claude_gets_the_message_as_its_prompt(self):
        self.starter.start(self.card.ref, agent="claude")
        call = next(c for c in self.fake.calls if c[:2] == ["agent", "start"])
        self.assertEqual(call[-2], "--")
        self.assertTrue(call[-1].startswith(f"Start work on herdboard card {self.card.ref}:"))

    def test_the_agent_pane_shows_the_card_in_the_sidebar(self):
        self.starter.start(self.card.ref)
        call = next(c for c in self.fake.calls if c[:2] == ["pane", "report-metadata"])
        self.assertEqual(call[2], "w9:p1")
        self.assertEqual(call[call.index("--source") + 1], "user:herdboard")
        token = call[call.index("--token") + 1]
        self.assertTrue(token.startswith("herdboard_card=▶ "))
        self.assertTrue(token.endswith(" · In progress"))

    def test_no_sidebar_card_without_an_agent(self):
        self.starter.start(self.card.ref, no_agent=True)
        self.assertFalse(any(c[:2] == ["pane", "report-metadata"] for c in self.fake.calls))

    def test_a_card_change_updates_the_sidebar(self):
        self.starter.start(self.card.ref)
        self.fake.calls.clear()
        with patch.object(start, "command", self.fake):
            start.update_card_token(self.store.move(self.card.ref, "Review"))
        call = next(c for c in self.fake.calls if c[:2] == ["pane", "report-metadata"])
        self.assertTrue(call[call.index("--token") + 1].endswith(" · Review"))

    def test_a_deleted_card_clears_the_sidebar(self):
        self.starter.start(self.card.ref)
        self.fake.calls.clear()
        with patch.object(start, "command", self.fake):
            start.update_card_token(self.store.delete(self.card.ref), removed=True)
        call = next(c for c in self.fake.calls if c[:2] == ["pane", "report-metadata"])
        self.assertEqual(call[call.index("--clear-token") + 1], "herdboard_card")

    def test_a_card_that_was_never_started_calls_no_herdr(self):
        other = self.store.add(self.board, "Not started")
        self.fake.calls.clear()
        with patch.object(start, "command", self.fake):
            start.update_card_token(other)
        self.assertEqual(self.fake.calls, [])

    def test_agent_liveness_uses_current_pane_and_stored_name(self):
        self.starter.start(self.card.ref)
        with patch.object(start, "herdr", wraps=start.herdr) as runner:
            self.starter.start(self.card.ref)
        self.assertTrue(any(c.args[:3] == ("agent", "get", "w9:p1") for c in runner.call_args_list))
        self.assertTrue(any(c.args[:3] == ("agent", "get", "hb-" + self.card.ref) for c in runner.call_args_list))

    def test_agent_in_new_root_or_by_name_is_not_started_twice(self):
        self.starter.start(self.card.ref)
        normal = self.fake
        for target in ("w9:p2", "hb-" + self.card.ref):
            with self.subTest(target=target):
                def runner(argv, timeout=120):
                    if argv[1:3] == ["workspace", "get"]:
                        return subprocess.CompletedProcess(argv, 0, '{"result":{"root_pane":{"pane_id":"w9:p2"}}}', "")
                    if argv[1:3] == ["agent", "get"]:
                        if argv[3] == target:
                            return subprocess.CompletedProcess(argv, 0, '{"result":{"agent":{"pane_id":"w9:p2"}}}', "")
                        return subprocess.CompletedProcess(argv, 1, '', '{"error":{"code":"agent_not_found"}}')
                    return normal(argv, timeout)
                with patch.object(start, "command", runner):
                    result = self.starter.start(self.card.ref)
                self.assertEqual(result["pane_id"], "w9:p2")
        self.assertEqual(sum(c[:2] == ["agent", "start"] for c in self.fake.calls), 1)

    def test_registration_skips_unwritable_registry(self):
        with patch.object(start, "_write", side_effect=start.IoError("readonly")):
            start.register_checkout(self.repo)

    def test_bad_nested_herdr_shapes_are_command_errors(self):
        normal = self.fake
        cases = ((["worktree", "create"], {"workspace": [], "root_pane": {}}),
                 (["worktree", "list"], {"worktrees": [{}]}),
                 (["worktree", "list"], {"worktrees": [{"path": str(self.repo), "open_workspace_id": {}}]}))
        for operation, value in cases:
            with self.subTest(operation=operation):
                def runner(argv, timeout=120):
                    if argv[1:3] == operation:
                        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": value}), "")
                    return normal(argv, timeout)
                with patch.object(start, "command", runner):
                    with self.assertRaises(start.HerdboardError) as raised:
                        self.starter.start(self.card.ref)
                self.assertEqual(raised.exception.code, "herdr_error")

    def test_root_pane_fallback_uses_first_available_pane(self):
        self.starter.start(self.card.ref, no_agent=True)
        normal = self.fake
        def runner(argv, timeout=120):
            if argv[1:3] == ["workspace", "get"]:
                return subprocess.CompletedProcess(argv, 0, '{"result":{}}', "")
            if argv[1:3] == ["pane", "list"]:
                return subprocess.CompletedProcess(argv, 0, '{"result":{"panes":[{"pane_id":"w9:p2"},{"pane_id":"w9:p3"}]}}', "")
            return normal(argv, timeout)
        with patch.object(start, "command", runner):
            result = self.starter.start(self.card.ref, no_agent=True)
        self.assertEqual(result["pane_id"], "w9:p2")

    def test_herdr_non_json_failure_and_bad_shapes(self):
        for output, status in (("syntax error", 2), ("[]", 0), ('{"result":[]}', 0), ('{"result":{}}', 0)):
            with self.subTest(output=output), patch.object(start, "command", return_value=subprocess.CompletedProcess([], status, output, "")):
                with self.assertRaises(start.HerdboardError) as raised:
                    self.starter.start(self.card.ref)
                self.assertEqual(raised.exception.code, "herdr_error")
                if status:
                    self.assertIn(output, str(raised.exception))

    def test_bare_and_submodule_checkout_fallback(self):
        bare = self.root / "bare.git"
        subprocess.run(["git", "clone", "--bare", "-q", str(self.repo), str(bare)], check=True, capture_output=True)
        work = self.root / "bare-work"
        subprocess.run(["git", "-C", str(bare), "worktree", "add", "-q", str(work)], check=True, capture_output=True)
        self.assertEqual(start._main_checkout(work), work)
        sub = self.repo / "sub"
        subprocess.run(["git", "-C", str(self.repo), "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(bare), str(sub)], check=True, capture_output=True)
        self.assertEqual(start._main_checkout(sub), sub)

    def test_resolved_worktree_paths_reuse_workspace(self):
        self.starter.start(self.card.ref, no_agent=True)
        self.fake.worktrees[0]["path"] = str(self.root / "work" / ".." / "work")
        self.starter.start(self.card.ref, no_agent=True)
        self.assertFalse(any(c[:2] == ["worktree", "open"] for c in self.fake.calls))

    def test_repo_board_name_wins_over_relative_directory(self):
        card = self.store.add(self.store.ensure_inbox(), "Global")
        with patch.object(start.Path, "exists", return_value=True):
            self.assertEqual(self.starter._repository(card, self.board.stem), repo_identity(self.repo).identity)


class NoRepositoryStartTests(unittest.TestCase):
    """A card without a repository runs in its own folder, and its output goes in the attachments."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.config = Config(todos_dir=self.root / "todos", projects_dir=self.root / "Projects")
        self.store = Store(self.config)
        self.card = self.store.add(self.store.ensure_inbox(), "Research corvid memory #feature")
        self.fake = FakeHerdr(self.root)
        self.runner = patch.object(start, "command", self.fake)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.starter = start.Starter(self.store, self.root)

    def folder(self):
        return start.work_folder(self.config, self.card.ref)

    def brief(self, ref=None):
        return (start.state_dir() / "briefs" / f"{ref or self.card.ref}.md").read_text(encoding="utf-8")

    def record(self, key=None):
        return json.loads((start.state_dir() / "starts.json").read_text())[key or self.card.ref]

    def test_no_repo_creates_the_card_folder_and_opens_it(self):
        result = self.starter.start(self.card.ref, no_repo=True)
        self.assertEqual(self.folder().is_dir(), True)
        self.assertEqual(result["worktree_path"], str(self.folder()))
        self.assertIsNone(result["branch"])
        self.assertIsNone(result["base"])
        create = next(c for c in self.fake.calls if c[:2] == ["workspace", "create"])
        self.assertEqual(create[create.index("--cwd") + 1], str(self.folder()))
        self.assertEqual(create[create.index("--label") + 1], self.card.ref)
        self.assertIn("--focus", create)
        self.assertEqual(self.store.show(self.card.ref).column, "In progress")
        self.assertEqual(self.record()["mode"], "repoless")
        self.assertEqual(self.record()["steps_done"], [])
        agent = next(c for c in self.fake.calls if c[:2] == ["agent", "start"])
        self.assertIn(str(self.card.ref), " ".join(agent))

    def test_a_repoless_start_uses_the_repoless_brief(self):
        self.starter.start(self.card.ref, no_repo=True)
        text = self.brief()
        self.assertIn(f"Folder: `{self.folder()}`", text)
        self.assertIn("Attachments:", text)
        self.assertIn("deletes the folder", text)
        self.assertIn("findings.md", text)
        self.assertNotIn("Commit every change", text)
        self.assertNotIn("Repository:", text)

    def test_a_second_start_reuses_the_open_workspace(self):
        self.starter.start(self.card.ref, no_repo=True)
        again = self.starter.start(self.card.ref, no_repo=True)
        self.assertEqual(again["workspace_id"], "x1")
        self.assertEqual(sum(c[:2] == ["workspace", "create"] for c in self.fake.calls), 1)
        self.assertEqual(sum(c[:2] == ["agent", "start"] for c in self.fake.calls), 1)

    def test_a_stored_repoless_mode_needs_no_flag(self):
        self.starter.start(self.card.ref, no_repo=True)
        self.starter.start(self.card.ref)
        self.assertEqual(self.record()["mode"], "repoless")
        self.assertFalse(any(c[:2] == ["worktree", "create"] for c in self.fake.calls))

    def test_a_repository_is_still_required_without_flags(self):
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(self.card.ref)
        self.assertEqual(raised.exception.code, "repo_required")
        self.assertIn("--no-repo", str(raised.exception))
        self.assertIn("--new-repo", str(raised.exception))

    def test_a_prompt_chooses_the_target(self):
        prompt = unittest.mock.Mock(return_value=("repoless", None))
        starter = start.Starter(self.store, self.root, prompt_target=prompt)
        result = starter.start(self.card.ref)
        prompt.assert_called_once()
        self.assertEqual(result["mode"], "repoless")

    def test_conflicting_flags_are_usage(self):
        for options in ({"new_repo": self.root / "new", "no_repo": True},
                        {"new_repo": self.root / "new", "repo": "board"}):
            with self.subTest(options=options), self.assertRaises(start.StartError) as raised:
                self.starter.start(self.card.ref, **options)
            self.assertEqual(raised.exception.code, "usage")

    def test_new_repo_is_usage_on_a_repository_board(self):
        repo = self.root / "repo"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True, capture_output=True)
        board = self.store.init_board(repo_identity(repo))
        card = self.store.add(board, "On a repository board")
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(card.ref, new_repo=self.root / "new")
        self.assertEqual(raised.exception.code, "usage")

    def test_new_repo_is_usage_for_a_linear_issue(self):
        with self.assertRaises(start.StartError) as raised:
            self.starter._new_repo_path(self.card, self.root / "new", {"id": "issue-1"})
        self.assertEqual(raised.exception.code, "usage")

    def test_go_to_workspace_focuses_the_card_folder_workspace(self):
        self.starter.start(self.card.ref, no_repo=True, no_agent=True)
        self.fake.calls.clear()
        self.assertEqual(start.go_to_workspace(self.card.ref), {"ref": self.card.ref, "workspace_id": "x1"})
        self.assertIn(["workspace", "focus", "x1"], self.fake.calls)

    def test_mark_removed_reads_the_card_folder(self):
        self.starter.start(self.card.ref, no_repo=True, no_agent=True)
        card = self.store.show(self.card.ref)
        start.mark_removed([card])
        self.assertTrue(card.worktree)
        self.assertFalse(card.worktree_removed)
        self.folder().rmdir()
        start.mark_removed([card])
        self.assertTrue(card.worktree_removed)

    def test_archive_blocker_names_the_work_folder(self):
        self.starter.start(self.card.ref, no_repo=True, no_agent=True)
        reason = start.archive_blocker(self.card.ref)
        self.assertIn("work folder", reason)
        self.assertIn(str(self.folder()), reason)


class NewRepositoryStartTests(unittest.TestCase):
    """`--new-repo` creates a local repository and its board, then starts like any card."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.config = Config(todos_dir=self.root / "todos", projects_dir=self.root / "Projects")
        self.store = Store(self.config)
        self.inbox = self.store.ensure_inbox()
        self.card = self.store.add(self.inbox, "Build corvid tracker #feature")
        self.fake = FakeHerdr(self.root)
        self.runner = patch.object(start, "command", self.fake)
        self.runner.start()
        self.addCleanup(self.runner.stop)
        self.starter = start.Starter(self.store, self.root)

    def git(self, directory, *args):
        return subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True,
                              text=True).stdout.strip()

    def test_a_new_repository_is_created_and_the_card_moves_to_its_board(self):
        path = self.root / "corvid-tracker"
        result = self.starter.start(self.card.ref, new_repo=path)
        self.assertTrue(path.is_dir())
        self.assertEqual(self.git(path, "symbolic-ref", "--short", "HEAD"), "main")
        self.assertEqual(self.git(path, "rev-list", "--count", "HEAD"), "1")
        board = self.store.board_for_repo(repo_identity(path))
        self.assertIsNotNone(board)
        moved = self.store.show(self.card.ref)
        self.assertEqual(moved.path.resolve(), board.resolve())
        self.assertEqual(moved.column, "In progress")
        self.assertEqual(result["base"], "main")
        self.assertTrue(result["branch"].startswith(self.card.ref))
        self.assertEqual(result["mode"], "repo")
        record = json.loads((start.state_dir() / "starts.json").read_text())[self.card.ref]
        self.assertEqual(record["mode"], "repo")
        self.assertNotIn("new_repo_path", record)

    def test_the_default_path_comes_from_the_card_title(self):
        result = self.starter.start(self.card.ref, new_repo=True)
        self.assertTrue((self.root / "Projects" / "build-corvid-tracker").is_dir())
        self.assertEqual(result["mode"], "repo")

    def test_a_non_empty_folder_is_refused(self):
        path = self.root / "taken"
        path.mkdir()
        (path / "file.txt").write_text("x")
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(self.card.ref, new_repo=path)
        self.assertEqual(raised.exception.code, "path_exists")
        self.assertFalse((path / ".git").exists())

    def test_an_empty_folder_is_used(self):
        path = self.root / "empty"
        path.mkdir()
        self.starter.start(self.card.ref, new_repo=path, no_agent=True)
        self.assertEqual(self.git(path, "symbolic-ref", "--short", "HEAD"), "main")

    def test_a_card_with_sub_cards_is_refused(self):
        self.store.add(self.inbox, "Child task", parent=self.card.ref)
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(self.card.ref, new_repo=self.root / "new")
        self.assertEqual(raised.exception.code, "new_repo_family")
        self.assertFalse((self.root / "new").exists())

    def test_a_sub_card_is_refused(self):
        parent = self.store.add(self.inbox, "Parent task")
        child = self.store.add(self.inbox, "Child task", parent=parent.ref)
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(child.ref, new_repo=self.root / "new")
        self.assertEqual(raised.exception.code, "new_repo_family")

    def test_a_failure_after_the_repository_resumes_on_the_next_start(self):
        path = self.root / "resume"
        self.fake.fail = "agent start"
        with self.assertRaises(start.StartError):
            self.starter.start(self.card.ref, new_repo=path)
        self.assertTrue((path / ".git").is_dir())
        self.assertEqual(sum(c[:2] == ["worktree", "create"] for c in self.fake.calls), 1)
        self.starter.start(self.card.ref)
        self.assertEqual(sum(c[:2] == ["worktree", "create"] for c in self.fake.calls), 1)
        self.assertEqual(self.store.show(self.card.ref).column, "In progress")

    def test_a_failed_first_commit_resumes_on_the_next_start(self):
        path = self.root / "resume-commit"
        real = start._git
        failed = []

        def flaky(directory, *args):
            if args[:2] == ("commit", "--allow-empty") and not failed:
                failed.append(True)
                return None
            return real(directory, *args)

        with patch.object(start, "_git", flaky):
            with self.assertRaises(start.StartError) as raised:
                self.starter.start(self.card.ref, new_repo=path)
        self.assertEqual(raised.exception.code, "new_repo_failed")
        self.assertTrue((path / ".git").is_dir())
        self.assertEqual(list(path.iterdir()), [path / ".git"])
        # The second start reuses the repository of the failed run, and makes its first commit.
        self.starter.start(self.card.ref, no_agent=True)
        self.assertEqual(self.git(path, "rev-list", "--count", "HEAD"), "1")
        self.assertEqual(self.git(path, "symbolic-ref", "--short", "HEAD"), "main")

    def test_planning_a_new_repository_for_a_card_with_sub_cards_is_refused(self):
        self.store.add(self.inbox, "Child task", parent=self.card.ref)
        with self.assertRaises(start.StartError) as raised:
            self.starter.prepare(self.card, None, self.card.ref, new_repo=self.root / "new",
                                 move_parent=False, check_column=False)
        self.assertEqual(raised.exception.code, "new_repo_family")
        self.assertFalse((self.root / "new").exists())

    def test_planning_first_records_the_path(self):
        path = self.root / "planned"
        prepared = self.starter.prepare(self.card, None, self.card.ref, new_repo=path, move_parent=False,
                                       check_column=False)
        self.assertEqual(prepared["mode"], "new_repo")
        self.assertEqual(prepared["new_repo_path"], str(path))
        self.assertTrue(start.work_folder(self.config, self.card.ref).is_dir())
        self.assertFalse(path.exists())
        self.starter.start(self.card.ref)
        self.assertTrue((path / ".git").is_dir())
        self.assertFalse(start.work_folder(self.config, self.card.ref).exists())


class StartCliTests(CliTestCase):
    """`start` and `plan` pass the new flags, and print a repoless start without a branch."""

    STARTED = {"ref": "t123", "workspace_id": "x1", "pane_id": "x1:p1",
               "worktree_path": "/todos/.herdboard/work/t123", "branch": None, "base": None,
               "agent_name": None, "steps_done": [], "mode": "repoless"}

    def test_start_no_repo_reaches_the_starter_and_prints_the_folder(self):
        with patch("herdboard.cli.Starter") as starter:
            starter.return_value.start.return_value = self.STARTED
            out, _ = self.run_cli("start", "t123", "--no-repo", directory=self.plain)
        self.assertTrue(starter.return_value.start.call_args.kwargs["no_repo"])
        self.assertEqual(out, "t123: no repository, in /todos/.herdboard/work/t123\n"
                              "Workspace: x1; pane: x1:p1; agent: none\n"
                              "Steps done: \n")

    def test_start_new_repo_passes_the_path(self):
        with patch("herdboard.cli.Starter") as starter:
            starter.return_value.start.return_value = {**self.STARTED, "mode": "repo", "branch": "t123-x"}
            self.run_json("start", "t123", "--new-repo", "/tmp/x", directory=self.plain)
            self.assertEqual(starter.return_value.start.call_args.kwargs["new_repo"], "/tmp/x")
            self.run_json("start", "t123", "--new-repo", directory=self.plain)
            self.assertIs(starter.return_value.start.call_args.kwargs["new_repo"], True)

    def test_plan_passes_the_new_flags(self):
        planned = {"ref": "t123", "agent": "claude", "model": None, "pane_id": "x1:p2",
                   "checkout": None, "plan": "/p/plan.md", "workspace_id": "x1",
                   "worktree_path": "/todos/.herdboard/work/t123", "branch": None, "base": None,
                   "mode": "repoless"}
        with patch("herdboard.cli.Planner") as planner:
            planner.return_value.plan.return_value = planned
            self.run_json("plan", "t123", "--no-repo", directory=self.plain)
            self.assertTrue(planner.return_value.plan.call_args.kwargs["no_repo"])
            self.run_json("plan", "t123", "--new-repo", "/tmp/x", directory=self.plain)
            self.assertEqual(planner.return_value.plan.call_args.kwargs["new_repo"], "/tmp/x")


if __name__ == "__main__":
    unittest.main()
