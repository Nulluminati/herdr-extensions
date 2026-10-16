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
from herdboard.pr_reviews import Page, PrReviewError, PullRequest  # noqa: E402
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
        self.service.page.return_value = ("acme/api", Page([
            pull_request(7, "Fix login timeout", draft=True, requested=True, additions=12, deletions=3,
                         labels=[{"name": "bug", "color": "d73a4a"}]),
            pull_request(8, "Add cache", status="reviewed", new_commits=True, notes=3, ci="failed",
                         decision="approved")], 2, None))
        self.service.details.return_value = {
            "body": "Sessions **end** too soon.",
            "issues": [{"number": 3, "title": "Logout fails", "url": "u3", "state": "OPEN"}],
            "reviews": [{"author": "octo", "state": "approved", "at": "2026-10-16T18:00:00Z"}],
            "checks": [{"name": "lint", "state": "passed"}, {"name": "unit tests", "state": "failed"}]}
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
            rows = screen.rows()
            self.assertEqual([row[:2] + row[3:] for row in rows], [
                ("◌", "#7", "octo", "+12 −3", "", "◆ your review", "", ""),
                ("●", "#8", "octo", "+0 −0", "✗ failed", "✔ approved", "✓ reviewed  ↑ new commits", "3 ✎")])
            self.assertTrue(rows[0][2].startswith("Fix login timeout"))
            self.assertIn(" bug ", rows[0][2])
            self.assertEqual(screen.headers(), ["", "PR", "Title", "Author", "Size", "CI", "GitHub review",
                                                "Agent review (local)", "Notes"])
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
        self.assertEqual(row[5], "\uf00d failed")
        self.assertEqual(row[6], "\uf058 approved")
        self.assertEqual(row[7], "\uf086 reviewed  \uf062 new commits")

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
        self.service.page.side_effect = PrReviewError("gh failed", "gh_failed")
        app = self.app()
        async with app.run_test() as pilot:
            screen = await self.open(app, pilot)
            self.assertEqual(screen.rows(), [])
        self.assertIn(("gh failed", "error"), self.notices)

    async def test_the_details_read_the_description_when_they_open(self):
        app = self.app()
        async with app.run_test() as pilot:
            await self.open(app, pilot)
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.service.details.assert_called_once_with(7)
            text = app.screen.text()
            for part in ("Sessions **end** too soon.", "#3 Logout fails", "octo approved", "unit tests",
                         "1 failed, 1 passed", "GitHub review", "Agent review (local)"):
                self.assertIn(part, text)
            self.assertLess(text.index("unit tests"), text.index("lint"))
        self.service.page.assert_called_with("", None)

    async def test_n_and_p_move_between_pages(self):
        first = Page([pull_request(n, f"PR {n}") for n in (9, 8)], 5, "c1")
        second = Page([pull_request(n, f"PR {n}") for n in (7, 6)], 5, "c2")
        self.service.page.side_effect = lambda text, after: ("acme/api", {None: first, "c1": second}[after])
        app = self.app()
        with patch("herdboard.pr_reviews.PAGE_SIZE", 2):
            async with app.run_test() as pilot:
                await self.paging(app, pilot)
        self.assertEqual([c.args[1] for c in self.service.page.call_args_list], [None, "c1", None])

    async def paging(self, app, pilot):
        """Page 1, then 2, then back to 1. A previous page on page 1 reads nothing."""
        screen = await self.open(app, pilot)
        self.assertIn("page 1 of 3", screen.status_text())
        await pilot.press("n")
        await self.settle(app, pilot)
        self.assertEqual([row[1] for row in screen.rows()], ["#7", "#6"])
        self.assertIn("page 2 of 3", screen.status_text())
        await pilot.press("p")
        await self.settle(app, pilot)
        self.assertEqual([row[1] for row in screen.rows()], ["#9", "#8"])
        await pilot.press("p")
        await self.settle(app, pilot)

    async def test_the_last_page_has_no_next(self):
        app = self.app()
        async with app.run_test() as pilot:
            screen = await self.open(app, pilot)
            await pilot.press("n")
            await self.settle(app, pilot)
            self.assertNotIn("page", screen.status_text())
        self.assertEqual(self.service.page.call_count, 1)

    async def test_slash_searches_github_and_escape_clears_it(self):
        app = self.app()
        async with app.run_test() as pilot:
            screen = await self.open(app, pilot)
            await pilot.press("slash")
            await pilot.pause()
            for key in "author:octo":
                await pilot.press(key)
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.service.page.assert_called_with("author:octo", None)
            self.assertIn("search: author:octo", screen.status_text())
            self.assertIsInstance(app.screen, PullRequestsScreen)
            await pilot.press("escape")
            await self.settle(app, pilot)
            self.service.page.assert_called_with("", None)
            self.assertIsInstance(app.screen, PullRequestsScreen)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, PullRequestsScreen)

    async def test_the_board_can_open_on_the_list(self):
        app = self.app(open_prs=True)
        async with app.run_test() as pilot:
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestsScreen)


if __name__ == "__main__":
    unittest.main()
