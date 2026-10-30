"""The key bar at the bottom: the keys of the top screen, dimmed when they do not apply to the selection."""
import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from herdboard.board import DetailScreen, HerdboardApp, KeyBar, PullRequestDetailScreen, PullRequestsScreen
from herdboard.config import Config
from herdboard.pr_reviews import Page, PullRequest
from herdboard.store import Store


def pull_request(number, **changes):
    pr = PullRequest(number, f"PR {number}", f"https://github.com/acme/api/pull/{number}", "octo", False,
                     "a" * 40, "branch", False, False)
    for name, value in changes.items():
        setattr(pr, name, value)
    return pr


def bar_keys(app) -> dict[str, bool]:
    """Each key description in the top screen's key bar, and whether it is dimmed."""
    keys = {}
    for label in app.screen.query_one(KeyBar).key_labels():
        cap = label.spans[0].style
        keys[label.plain.split("  ", 1)[1].replace(" ", " ")] = bool(cap.dim)
    return keys


class KeyBarTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state"),
                                      "HERDR_BIN_PATH": str(root / "no-herdr")})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Config(todos_dir=root / "todos"))
        self.path = self.store.ensure_inbox()
        self.service = Mock()
        self.service.page.return_value = ("acme/api", Page([
            pull_request(7), pull_request(8, status="reviewed", review={"workspace_id": "w9"})], 2, "next"))
        self.service.details.return_value = {"body": "", "issues": [], "reviews": [], "checks": []}

    def app(self):
        app = HerdboardApp(self.store, "inbox", lambda _: [self.path], reload_interval=60, pr_service=self.service)
        app.animation_level = "none"
        return app

    async def settle(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    async def test_card_keys_are_dimmed_on_an_empty_column(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            keys = bar_keys(app)
        for key in ("Detail", "Done", "Edit", "Delete", "Start", "Merge", "Finish", "Move left"):
            self.assertTrue(keys[key], key)
        for key in ("Add", "Filter", "View", "Reload", "Help", "Quit"):
            self.assertFalse(keys[key], key)

    async def test_the_selected_card_sets_the_dimmed_keys(self):
        self.store.add(self.path, "Not started")
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            keys = bar_keys(app)
            for key in ("Detail", "Done", "Edit", "Delete", "Start", "Plan", "Archive", "Move left"):
                self.assertFalse(keys[key], key)
            for key in ("Go to workspace", "Review", "Merge", "Finish"):
                self.assertTrue(keys[key], key)

            app.current_card().worktree = True
            app._show_selection()
            await self.settle(app, pilot)
            keys = bar_keys(app)
            for key in ("Go to workspace", "Review", "Merge", "Finish", "Start"):
                self.assertFalse(keys[key], key)
            for key in ("Plan", "Archive"):
                self.assertTrue(keys[key], key)

    async def test_a_dimmed_key_still_runs_and_says_why(self):
        app = self.app()
        notices = []
        app.notify = lambda message, **kwargs: notices.append(message)
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            await pilot.press("G")
            await self.settle(app, pilot)
            await pilot.press("p")
            await self.settle(app, pilot)
        self.assertIn("This is the first page", notices)

    async def test_card_detail_shows_its_own_keys(self):
        self.store.add(self.path, "No checklist")
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, DetailScreen)
            keys = bar_keys(app)
        self.assertEqual(keys, {"Close": False, "Edit": False, "Open attachment": True, "Check": True})

    async def test_card_detail_check_applies_to_a_checklist(self):
        self.store.add(self.path, "With checklist", body=["- [ ] first"])
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertFalse(bar_keys(app)["Check"])

    async def test_pull_request_keys_follow_the_selected_pull_request(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestsScreen)
            keys = bar_keys(app)
            for key in ("Details", "Review", "Browser", "Next", "Reload"):
                self.assertFalse(keys[key], key)
            for key in ("Go", "Update", "Finish", "Prev"):
                self.assertTrue(keys[key], key)

            await pilot.press("j")
            await self.settle(app, pilot)
            keys = bar_keys(app)
            for key in ("Go", "Update", "Finish"):
                self.assertFalse(keys[key], key)

            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestDetailScreen)
            keys = bar_keys(app)
            for key in ("Review", "Go to workspace", "New commits", "Finish", "Open in browser", "Close"):
                self.assertFalse(keys[key], key)

    async def test_pull_request_detail_dims_the_keys_of_a_pull_request_without_a_review(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            await pilot.press("enter")
            await self.settle(app, pilot)
            keys = bar_keys(app)
        for key in ("Go to workspace", "New commits", "Finish"):
            self.assertTrue(keys[key], key)
        for key in ("Review", "Open in browser"):
            self.assertFalse(keys[key], key)

    async def test_a_dialog_key_bar_sits_at_the_bottom(self):
        self.store.add(self.path, "Card")
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            await pilot.press("enter")
            await self.settle(app, pilot)
            self.assertEqual(app.screen.query_one(KeyBar).region.bottom, 30)


if __name__ == "__main__":
    unittest.main()
