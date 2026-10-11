"""Start actions run through a thread worker, not the event loop."""
import asyncio
import subprocess
import threading
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/herdboard/src"))
from herdboard.board import HerdboardApp
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
        self.callback = Mock(return_value={"ref": self.card.ref, "workspace_id": "w1", "pane_id": "w1:p1", "worktree_path": "/tmp/work", "branch": "test", "agent_name": "hb-test", "steps_done": []})
    def app(self):
        app = HerdboardApp(self.store, "inbox", lambda _: [self.path], reload_interval=60, start_card=self.callback)
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
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.callback.assert_called_with(self.card.ref, "pi")
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
