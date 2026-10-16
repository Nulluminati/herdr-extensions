"""The board's pull request list: G opens it, and its keys start, refresh and finish PR reviews."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/herdboard/src"))
from herdboard.board import (AgentScreen, ClaudeModelScreen, ConfirmScreen, HerdboardApp, ModelScreen,  # noqa: E402
                             PullRequestDetailScreen, PullRequestsScreen)
from herdboard.config import Config  # noqa: E402
from herdboard.pr_reviews import PrReviewError, PullRequest  # noqa: E402
from herdboard.store import Store  # noqa: E402


def pull_request(number, title, **changes):
    pr = PullRequest(number, title, f"https://github.com/acme/api/pull/{number}", "octo", False, "a" * 40,
                     "branch", False, False)
    for name, value in changes.items():
        setattr(pr, name, value)
    return pr


class PullRequestBoardTests(unittest.IsolatedAsyncioTestCase):
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
        self.card = self.store.add(self.path, "Board card")
        self.service = Mock()
        self.service.list.return_value = ("acme/api", [
            pull_request(7, "Fix login timeout", draft=True, requested=True),
            pull_request(8, "Add cache", status="reviewed", new_commits=True, notes=3)])
        self.service.review.return_value = {"key": "acme/api#7", "number": 7, "workspace_id": "w9",
                                            "agent": "claude", "model": "sonnet", "prompted": False}
        self.service.refresh.return_value = {"key": "acme/api#8", "number": 8, "kept": 3, "dropped": [],
                                             "agent": "pi", "model": "zai/glm-5.3", "thinking": "high"}
        self.service.finish.return_value = {"key": "acme/api#7", "number": 7, "branch_deleted": True}
        self.service.go.return_value = {"key": "acme/api#7", "workspace_id": "w9"}
        self.notices = []

    def app(self, **options):
        app = HerdboardApp(self.store, "inbox", lambda _: [self.path], reload_interval=60,
                           pr_service=self.service, **options)
        app.animation_level = "none"
        app.notify = lambda message, **kwargs: self.notices.append((message, kwargs.get("severity")))
        return app

    async def settle(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def open(self, app, pilot):
        await pilot.press("G")
        await self.settle(app, pilot)
        self.assertIsInstance(app.screen, PullRequestsScreen)
        return app.screen

    async def test_capital_g_opens_and_closes_the_list(self):
        app = self.app()
        async with app.run_test() as pilot:
            screen = await self.open(app, pilot)
            self.assertEqual(screen.rows(), [
                ("◌", "#7", "Fix login timeout", "octo", "◆ review requested", ""),
                ("●", "#8", "Add cache", "octo", "✓ reviewed  ↑ new commits", "3 ✎")])
            await pilot.press("G")
            await pilot.pause()
            self.assertIs(app.screen, app.screen_stack[0])
            await self.open(app, pilot)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, PullRequestsScreen)

    async def test_nerd_icons(self):
        self.store.config.icon_style = "nerd"
        app = self.app()
        async with app.run_test() as pilot:
            screen = await self.open(app, pilot)
            row = screen.rows()[1]
        self.assertEqual(row[0], "\uf407")
        self.assertEqual(row[4], "\uf00c reviewed  \uf062 new commits")

    async def test_board_keys_do_not_run_behind_the_list(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.open(app, pilot)
            await pilot.press("x")
            await pilot.pause()
            self.assertIsInstance(app.screen, PullRequestsScreen)
            self.assertEqual(self.store.show(self.card.ref).title, "Board card")

    async def test_enter_and_a_click_open_the_details(self):
        self.store.config.icon_style = "plain"
        app = self.app()
        async with app.run_test(size=(120, 30)) as pilot:
            await self.open(app, pilot)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, PullRequestDetailScreen)
            self.assertEqual(app.screen.pr.number, 7)
            self.assertIn("Fix login timeout", app.screen.text())
            await pilot.press("escape")
            await pilot.pause()
            # Line 1 of the table is its header, so PR #8 is on line 3.
            await pilot.click("#pr-list", offset=(12, 3))
            await pilot.pause()
            self.assertIsInstance(app.screen, PullRequestDetailScreen)
            self.assertEqual(app.screen.pr.number, 8)
            self.assertIn("3 notes", app.screen.text())

    async def test_s_in_the_details_reviews(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.open(app, pilot)
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()
            self.assertIsInstance(app.screen, AgentScreen)

    async def test_s_chooses_claude_and_its_model_then_reviews(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.open(app, pilot)
            await pilot.press("s")
            await pilot.pause()
            self.assertIsInstance(app.screen, AgentScreen)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, ClaudeModelScreen)
            await pilot.press("down", "down", "enter")
            await self.settle(app, pilot)
        self.service.review.assert_called_once_with(7, "claude", "sonnet", None)
        self.assertIn(("PR #7: claude (sonnet) reviews in workspace w9", None), self.notices)

    async def test_s_with_pi_takes_its_model_and_thinking(self):
        app = self.app()
        with patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3"])):
            async with app.run_test() as pilot:
                await self.open(app, pilot)
                await pilot.press("j", "s")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, ModelScreen)
                app.screen.query_one("#thinking").value = "high"
                await pilot.press("down", "enter")
                await self.settle(app, pilot)
        self.service.review.assert_called_once_with(8, "pi", "zai/glm-5.3", "high")

    async def test_f_asks_then_finishes(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.open(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            await pilot.press("n")
            await pilot.pause()
            self.service.finish.assert_not_called()
            await pilot.press("f")
            await pilot.pause()
            await pilot.press("y")
            await self.settle(app, pilot)
        self.service.finish.assert_called_once_with(7, False)

    async def test_unsent_notes_ask_again_to_force(self):
        self.service.finish.side_effect = [PrReviewError("2 notes", "unsent_notes"),
                                           {"key": "acme/api#7", "number": 7, "branch_deleted": True}]
        app = self.app()
        async with app.run_test() as pilot:
            await self.open(app, pilot)
            await pilot.press("f")
            await pilot.pause()
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await self.settle(app, pilot)
        self.assertEqual(self.service.finish.call_args_list[-1].args, (7, True))

    async def test_u_refreshes_then_offers_a_new_review_with_the_same_agent(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.open(app, pilot)
            await pilot.press("j", "u")
            await self.settle(app, pilot)
            self.service.refresh.assert_called_once_with(8)
            self.assertIsInstance(app.screen, ConfirmScreen)
            await pilot.press("y")
            await self.settle(app, pilot)
        self.service.review.assert_called_once_with(8, "pi", "zai/glm-5.3", "high")

    async def test_g_goes_to_the_workspace_and_errors_are_notices(self):
        self.service.go.side_effect = PrReviewError("Herdr has no open workspace", "workspace_closed")
        app = self.app()
        async with app.run_test() as pilot:
            await self.open(app, pilot)
            await pilot.press("g")
            await self.settle(app, pilot)
        self.service.go.assert_called_once_with(7)
        self.assertIn(("Herdr has no open workspace", "error"), self.notices)

    async def test_a_list_failure_is_a_notice(self):
        self.service.list.side_effect = PrReviewError("gh failed", "gh_failed")
        app = self.app()
        async with app.run_test() as pilot:
            screen = await self.open(app, pilot)
            self.assertEqual(screen.rows(), [])
        self.assertIn(("gh failed", "error"), self.notices)

    async def test_the_board_can_open_on_the_list(self):
        app = self.app(open_prs=True)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestsScreen)


if __name__ == "__main__":
    unittest.main()
