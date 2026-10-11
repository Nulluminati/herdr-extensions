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
from herdboard.config import Config
from herdboard.identity import repo_identity
from herdboard.store import Store
from herdboard import start


class FakeHerdr:
    def __init__(self, root):
        self.root = root
        self.calls = []
        self.worktrees = []
        self.fail = None
        self.agent = False
        self.branches = {}

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
            result = {"workspace": {"workspace_id": workspace}, "root_pane": {"pane_id": workspace + ":p1"}, "worktree": {"path": str(path)}}
            self.worktrees = [w for w in self.worktrees if w["path"] != str(path)]
            self.worktrees.append({"branch": branch, "path": str(path), "open_workspace_id": workspace})
        elif operation == "workspace get":
            result = {"workspace": {"workspace_id": "w9"}, "root_pane": {"pane_id": "w9:p1"}}
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
        self.assertTrue(any(c[:2] == ["workspace", "focus"] for c in self.fake.calls))

    def test_stored_closed_workspace_opens(self):
        self.starter.start(self.card.ref, no_agent=True)
        self.fake.worktrees[0]["open_workspace_id"] = None
        self.starter.start(self.card.ref, no_agent=True)
        self.assertTrue(any(c[:2] == ["worktree", "open"] for c in self.fake.calls))

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
        self.assertEqual(raised.exception.steps_done, ["repository", "checkout", "branch"])
        self.starter.start(self.card.ref, no_agent=True)

    def test_global_requires_repo_even_inside_repository(self):
        card = self.store.add(self.store.ensure_inbox(), "Global task")
        with self.assertRaises(start.StartError) as raised:
            self.starter.start(card.ref, no_agent=True)
        self.assertEqual(raised.exception.code, "repo_required")
        result = self.starter.start(card.ref, repo=self.board.stem, no_agent=True)
        self.assertEqual(result["workspace_id"], "w9")

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
        self.assertEqual(card.column, "In progress")

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

    def test_agent_message_is_safe_for_herdr_shell_encoding(self):
        self.starter.start(self.card.ref)
        call = next(c for c in self.fake.calls if c[:2] == ["agent", "start"])
        message = call[-1]
        self.assertNotIn("\n", message)
        self.assertNotIn("\r", message)
        self.assertIn(self.card.ref, message)
        self.assertIn(self.card.title, message)
        self.assertIn("herdboard CLI", message)

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

    def test_body_shell_safety_pi_arguments_and_truncation(self):
        body = ['$(touch x) `touch y` single\' double" \x7f']
        self.store.move(self.card.ref, "In progress")
        card = self.store.show(self.card.ref)
        card.body = body
        with patch.object(self.store, "show", return_value=card):
            self.starter.start(self.card.ref, agent="pi")
        call = next(c for c in self.fake.calls if c[:2] == ["agent", "start"])
        self.assertEqual(call[:-1], ["agent", "start", "hb-" + self.card.ref,
                                    "--kind", "pi", "--pane", "w9:p1", "--", "--"])
        self.assertEqual(json.loads(call[-1])["body"], body)
        self.assertNotIn("\x7f", call[-1])
        card.body = ["a" * 5000]
        self.fake.agent = False
        with patch.object(self.store, "show", return_value=card):
            self.starter.start(self.card.ref)
        message = json.loads([c for c in self.fake.calls if c[:2] == ["agent", "start"]][-1][-1])
        self.assertEqual(len("\n".join(message["body"])), 4000 + len("\n(truncated)"))
        self.assertIn("(truncated)", message["body"][-1])

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


if __name__ == "__main__":
    unittest.main()
