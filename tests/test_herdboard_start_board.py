"""Start actions run through a thread worker, not the event loop."""
import asyncio
import subprocess
import threading
import time
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/herdboard/src"))
from herdboard.board import ClaudeModelScreen, ModelScreen, HerdboardApp, ReviewKindScreen
from herdboard.config import Config
from herdboard.store import Store
from herdboard.start import StartError

class StartBoardTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Config(todos_dir=root / "todos"))
        self.path = self.store.ensure_inbox()
        self.card = self.store.add(self.path, "Start test")
        self.merge = Mock(return_value={"ref": self.card.ref, "mode": "local", "base": "main", "commit": "abcdef12",
                                        "worktree_removed": True, "branch_deleted": True})
        self.finish = Mock(return_value={"ref": self.card.ref, "worktree_removed": True, "branch_deleted": False})
        self.review = Mock(return_value={"ref": self.card.ref, "workspace_id": "w1", "pane_id": "w1:p1",
                                         "review_pane": "w1:p2", "agent_restarted": False})
        self.agent_review = Mock(return_value={"ref": self.card.ref, "reviewer": "claude", "model": "opus",
                                               "pane_id": "w1:p3", "folder": "/f", "then_hunk": False})
        self.plan = Mock(return_value={"ref": self.card.ref, "agent": "claude", "model": "opus", "pane_id": "w1:p7",
                                       "checkout": "/repo", "plan": "/plan.md"})
        self.go = Mock(return_value={"ref": self.card.ref, "workspace_id": "w1"})
        self.callback = Mock(return_value={"ref": self.card.ref, "workspace_id": "w1", "pane_id": "w1:p1", "worktree_path": "/tmp/work", "branch": "test", "agent_name": "hb-test", "steps_done": []})
    def app(self):
        app = HerdboardApp(self.store, "inbox", lambda _: [self.path], reload_interval=60, start_card=self.callback,
                           review_card=self.review, merge_card=self.merge, finish_card=self.finish,
                           agent_review_card=self.agent_review, plan_card=self.plan, go_card=self.go)
        app.animation_level = "none"
        return app
    async def test_default_and_agent_dialog(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.callback.assert_called_with(self.card.ref, None)
            await pilot.press("S")
            await pilot.pause()
            self.assertIsNot(app.screen, app.base)
            await pilot.press("down", "enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ModelScreen)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.callback.assert_called_with(self.card.ref, "pi")

    async def test_choose_a_pi_model_and_thinking_level(self):
        app = self.app()
        with patch("herdboard.board.pi_model_choices", return_value=("openai-codex/gpt-6.1-sol, high", ["zai/glm-5.3", "openai-codex/gpt-6-sol"])):
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, ModelScreen)
                self.assertEqual(screen.labels, ["pi default (openai-codex/gpt-6.1-sol, high)", "zai/glm-5.3", "openai-codex/gpt-6-sol"])
                screen.query_one("#thinking").value = "medium"
                await pilot.press("down", "enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.callback.assert_called_with(self.card.ref, "pi", model="zai/glm-5.3", thinking="medium")

    async def test_shift_r_then_human_opens_the_review(self):
        app = self.app()
        notices = []
        app.notify = lambda message, **kwargs: notices.append((message, kwargs.get("severity")))
        async with app.run_test() as pilot:
            await pilot.press("R")
            await pilot.pause()
            self.assertIsInstance(app.screen, ReviewKindScreen)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
        self.review.assert_called_once_with(self.card.ref)
        self.agent_review.assert_not_called()
        self.assertIn((f"Review of {self.card.ref} opened in w1", None), notices)

    async def test_an_agent_review_by_claude_with_a_chosen_model(self):
        app = self.app()
        notices = []
        app.notify = lambda message, **kwargs: notices.append((message, kwargs.get("severity")))
        async with app.run_test() as pilot:
            await pilot.press("R")
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ClaudeModelScreen)
            self.assertEqual(app.screen.labels, ["claude default", "opus", "sonnet", "haiku"])
            await pilot.press("down", "enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
        self.agent_review.assert_called_once_with(self.card.ref, reviewer="claude", model="opus", thinking=None,
                                                  then_hunk=False)
        self.review.assert_not_called()
        self.assertIn((f"Agent review of {self.card.ref} by claude (opus) started in w1:p3", None), notices)

    async def test_both_with_a_pi_reviewer(self):
        app = self.app()
        with patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3"])):
            async with app.run_test() as pilot:
                await pilot.press("R")
                await pilot.pause()
                await pilot.press("down", "down", "enter")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, ModelScreen)
                app.screen.query_one("#thinking").value = "high"
                await pilot.press("down", "enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.agent_review.assert_called_once_with(self.card.ref, reviewer="pi", model="zai/glm-5.3",
                                                  thinking="high", then_hunk=True)

    async def test_shift_p_plans_with_a_chosen_claude_model(self):
        app = self.app()
        notices = []
        app.notify = lambda message, **kwargs: notices.append((message, kwargs.get("severity")))
        async with app.run_test() as pilot:
            await pilot.press("P")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ClaudeModelScreen)
            await pilot.press("down", "enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
        self.plan.assert_called_once_with(self.card.ref, agent="claude", model="opus", thinking=None)
        self.callback.assert_not_called()
        self.assertIn((f"claude (opus) plans {self.card.ref} in a new tab", None), notices)

    async def test_shift_p_plans_with_pi(self):
        app = self.app()
        with patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3"])):
            async with app.run_test() as pilot:
                await pilot.press("P")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, ModelScreen)
                await pilot.press("down", "enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.plan.assert_called_once_with(self.card.ref, agent="pi", model="zai/glm-5.3", thinking=None)

    async def test_the_board_workers_read_the_board_config_file(self):
        config = Path(self.temp.name) / "custom.toml"
        app = HerdboardApp(self.store, "inbox", lambda _: [self.path], reload_interval=60, config_file=config)
        self.assertEqual(app._planner().config_file, config)
        self.assertEqual(app._agent_reviewer().config_file, config)
        self.assertEqual(app._merger().config_file, config)

    async def test_escape_in_the_planner_menu_does_nothing(self):
        await self.run_keys("P", "escape")
        self.plan.assert_not_called()

    async def test_escape_in_the_review_menu_does_nothing(self):
        await self.run_keys("R", "escape")
        self.review.assert_not_called()
        self.agent_review.assert_not_called()

    async def test_a_running_agent_review_shows_on_the_card(self):
        from herdboard import start
        path = self.path.parent / "worktree"
        path.mkdir()
        start._write(start.state_dir() / "starts.json", {self.card.ref: {
            "worktree_path": str(path), "agent_review": {"kind": "pi", "pane_id": "w1:p3"}}})
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertTrue(any("agent review: pi" in w.plain for w in app.query(".card")))
            start._write(start.state_dir() / "starts.json", {self.card.ref: {"worktree_path": str(path)}})
            await app.check_files()
            await pilot.pause()
            self.assertFalse(any("agent review" in w.plain for w in app.query(".card")))

    async def run_keys(self, *keys):
        app = self.app()
        notices = []
        app.notify = lambda message, **kwargs: notices.append((message, kwargs.get("severity")))
        async with app.run_test() as pilot:
            for key in keys:
                await pilot.press(key)
                await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
        return notices

    async def test_m_merges_after_y(self):
        notices = await self.run_keys("m", "y")
        self.merge.assert_called_once_with(self.card.ref)
        self.assertIn((f"Merged {self.card.ref} into main at abcdef1, then finished it", None), notices)

    def open_pr(self, **extra):
        from herdboard import start
        path = self.path.parent / "worktree"
        path.mkdir(exist_ok=True)
        start._write(start.state_dir() / "starts.json", {self.card.ref: {
            "worktree_path": str(path), "pr_url": "https://github.com/acme/api/pull/7", "pr_state": "open",
            "pr_checked": time.time(), **extra}})

    async def test_m_on_an_open_pr_asks_before_it_rewrites_the_description(self):
        self.open_pr()
        self.merge.return_value = {"ref": self.card.ref, "mode": "pr", "base": "main",
                                   "pr_url": "https://github.com/acme/api/pull/7", "pr_action": "writing",
                                   "writer": "the card's agent", "folder": "/f"}
        notices = await self.run_keys("m", "y", "y")
        self.merge.assert_called_once_with(self.card.ref, rewrite=True)
        self.assertIn((f"The card's agent writes the pull request description of {self.card.ref}", None), notices)

    async def test_m_on_an_open_pr_can_only_push(self):
        self.open_pr()
        self.merge.return_value = {"ref": self.card.ref, "mode": "pr", "base": "main",
                                   "pr_url": "https://github.com/acme/api/pull/7", "pr_action": "pushed"}
        notices = await self.run_keys("m", "y", "n")
        self.merge.assert_called_once_with(self.card.ref, rewrite=False)
        self.assertIn((f"Pushed {self.card.ref} to its pull request https://github.com/acme/api/pull/7", None),
                      notices)

    async def test_a_written_pr_description_shows_on_the_card(self):
        self.open_pr(pr_write={"folder": "/f", "pane_id": "w1:p1", "own_pane": False})
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertTrue(any("PR: writing" in w.plain for w in app.query(".card")))

    async def test_m_then_n_does_not_merge(self):
        await self.run_keys("m", "n")
        self.merge.assert_not_called()

    async def test_f_finishes_after_y(self):
        notices = await self.run_keys("f", "y")
        self.finish.assert_called_once_with(self.card.ref)
        self.assertIn((f"Finished {self.card.ref}; branch kept", None), notices)

    async def test_g_goes_to_the_card_workspace(self):
        notices = await self.run_keys("g")
        self.go.assert_called_once_with(self.card.ref)
        self.assertIn((f"Went to the workspace w1 of {self.card.ref}", None), notices)

    async def test_g_shows_why_there_is_no_workspace(self):
        self.go.side_effect = StartError("No open workspace", "workspace_closed")
        notices = await self.run_keys("g")
        self.assertIn(("No open workspace", "error"), notices)

    async def test_a_merge_error_is_shown(self):
        self.merge.side_effect = StartError("Rebase conflict", "rebase_conflict")
        notices = await self.run_keys("m", "y")
        self.assertIn(("Rebase conflict", "error"), notices)

    async def test_a_review_error_is_shown(self):
        self.review.side_effect = StartError("No hunk", "hunk_unavailable")
        app = self.app()
        notices = []
        app.notify = lambda message, **kwargs: notices.append((message, kwargs.get("severity")))
        async with app.run_test() as pilot:
            await pilot.press("R")
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
        self.assertIn(("No hunk", "error"), notices)

    async def test_claude_skips_the_model_dialog(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("S")
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
        self.callback.assert_called_with(self.card.ref, "claude")
    async def test_refresh_marks_removed_worktree_without_board_change(self):
        from herdboard import start
        path = self.path.parent / "worktree"
        path.mkdir()
        start._write(start.state_dir() / "starts.json", {self.card.ref: {"worktree_path": str(path)}})
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            self.assertFalse(app.current_card().worktree_removed)
            path.rmdir()
            await app.check_files()
            self.assertTrue(app.current_card().worktree_removed)
            self.assertTrue(any("worktree removed" in w.plain for w in app.query(".card")))
            self.assertEqual(self.store.show(self.card.ref).column, "Backlog")

    async def test_refresh_reads_pr_states_and_shows_the_markers(self):
        from herdboard import merge, start
        path = self.path.parent / "worktree"
        path.mkdir()
        start._write(start.state_dir() / "starts.json", {self.card.ref: {
            "worktree_path": str(path), "pr_url": "https://github.com/acme/api/pull/7", "pr_state": "open"}})
        app = self.app()

        def merged():
            records = start._read(start.state_dir() / "starts.json")
            records[self.card.ref]["pr_state"] = "merged"
            start._write(start.state_dir() / "starts.json", records)

        with patch.object(merge, "refresh_pr_states", side_effect=merged) as refresh:
            async with app.run_test() as pilot:
                await pilot.pause()
                self.assertTrue(any("PR open" in w.plain for w in app.query(".card")))
                await app.check_files()
                refresh.assert_called()
                self.assertTrue(any("PR merged" in w.plain for w in app.query(".card")))
                self.store.done(self.card.ref)
                await app.check_files()
                await pilot.pause()
                self.assertTrue(any("worktree still exists" in w.plain for w in app.query(".card")))

    async def test_real_starter_runs_in_thread_for_inbox_view(self):
        await self._real_starter("inbox")

    async def test_real_starter_runs_in_thread_for_repository_view(self):
        await self._real_starter("repo")

    async def _real_starter(self, view):
        from herdboard import start
        from herdboard.identity import repo_identity
        from test_herdboard_start import FakeHerdr
        repo = self.path.parent / "repo"
        repo.mkdir()
        for args in (["init", "-q"], ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "init"]):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        board = self.store.init_board(repo_identity(repo))
        card = self.store.add(board, "Real start")
        start.register_checkout(repo)
        fake = FakeHerdr(repo.parent)
        threads = []
        event_thread = threading.get_ident()
        def runner(argv, timeout=120):
            threads.append(threading.get_ident())
            return fake(argv, timeout)
        app = HerdboardApp(self.store, view, lambda _: [board], directory=repo, repo_board=board, reload_interval=60)
        app.animation_level = "none"
        original_start = start.Starter.start
        repositories = []
        def started(starter, *args, **kwargs):
            repositories.append(kwargs.get("repo"))
            return original_start(starter, *args, **kwargs)
        with patch.object(start, "command", runner), patch.object(start.Starter, "start", started):
            async with app.run_test() as pilot:
                await pilot.press("s")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertFalse(app.starting)
        self.assertEqual(repositories, [board if view == "repo" else None])
        self.assertTrue(threads)
        self.assertTrue(all(t != event_thread for t in threads))
        self.assertEqual(self.store.show(card.ref).column, "In progress")
        self.assertEqual(start._read(start.state_dir() / "starts.json")[card.ref]["repository"], repo_identity(repo).identity)

    async def test_mark_removed_runs_off_event_loop(self):
        from herdboard import board
        threads = []
        event_thread = threading.get_ident()
        original = board.mark_removed
        def marked(cards):
            threads.append(threading.get_ident())
            return original(cards)
        app = self.app()
        with patch.object(board, "mark_removed", marked):
            async with app.run_test() as pilot:
                await pilot.pause()
                await app.check_files()
        self.assertTrue(threads)
        self.assertTrue(all(t != event_thread for t in threads))

    async def test_error_notifies(self):
        self.callback.side_effect = StartError("No checkout", "checkout_unknown")
        app = self.app()
        with patch.object(app, "notify") as notify:
            async with app.run_test() as pilot:
                await pilot.press("s")
                await app.workers.wait_for_complete()
                await pilot.pause()
                self.assertTrue(any("No checkout" in str(call) for call in notify.call_args_list))
