"""The tab bar at the top: Board and Pull requests tabs, with the keys of the current tab after them."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/herdboard/src"))
from textual.widgets import Footer  # noqa: E402

from herdboard.board import HerdboardApp, PullRequestsScreen, TabBar  # noqa: E402
from herdboard.config import Config  # noqa: E402
from herdboard.pr_reviews import Page  # noqa: E402
from herdboard.store import Store  # noqa: E402


class TabBarTests(unittest.IsolatedAsyncioTestCase):
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
        self.store.add(self.path, "Board card")
        self.service = Mock()
        self.service.page.return_value = ("acme/api", Page([], 0, None))

    def app(self):
        app = HerdboardApp(self.store, "inbox", lambda _: [self.path], reload_interval=60, pr_service=self.service)
        app.animation_level = "none"
        return app

    async def settle(self, app, pilot):
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()

    def bar(self, app) -> TabBar:
        return app.screen.query_one(TabBar)

    async def test_the_board_has_tabs_and_its_keys_at_the_top_and_no_footer(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            bar = self.bar(app)
            self.assertEqual(bar.active, "board")
            self.assertEqual(bar.tab_labels(), ["Board", "Pull requests G"])
            keys = bar.keys_text().plain.replace("\xa0", " ")
            for part in ("a Add", "/ Filter", "s Start", "? Help", "q Quit"):
                self.assertIn(part, keys)
            self.assertNotIn("PRs", keys)
            self.assertEqual(bar.region.y, 0)
            self.assertEqual(list(app.screen.query(Footer)), [])

    async def test_the_pull_request_tab_has_its_own_keys(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestsScreen)
            bar = self.bar(app)
            self.assertEqual(bar.active, "prs")
            self.assertEqual(bar.tab_labels(), ["Board G", "Pull requests"])
            keys = bar.keys_text().plain.replace("\xa0", " ")
            for part in ("/ Search", "n Next", "s Review", "esc Back"):
                self.assertIn(part, keys)
            self.assertNotIn("a Add", keys)
            self.assertEqual(list(app.screen.query(Footer)), [])

    async def test_a_click_on_the_other_tab_switches(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            await pilot.click("#tab-prs")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestsScreen)
            await pilot.click("#tab-prs")
            await self.settle(app, pilot)
            self.assertIsInstance(app.screen, PullRequestsScreen)
            await pilot.click("#tab-board")
            await self.settle(app, pilot)
            self.assertNotIsInstance(app.screen, PullRequestsScreen)

    async def test_switching_back_keeps_the_list_and_does_not_read_github_again(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            first = app.screen
            await pilot.press("G")
            await self.settle(app, pilot)
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertIs(app.screen, first)
            self.assertEqual(self.service.page.call_count, 1)

    async def test_switching_back_after_a_minute_reads_github_again(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            await pilot.press("G")
            await self.settle(app, pilot)
            app.get_screen("pull-requests").read_at -= 61
            await pilot.press("G")
            await self.settle(app, pilot)
            self.assertEqual(self.service.page.call_count, 2)

    async def test_a_read_in_the_background_shows_no_loading_spinner(self):
        import threading
        from textual.widgets import LoadingIndicator
        release = threading.Event()

        def slow(text, after):
            release.wait(5)
            return "acme/api", Page([], 0, None)

        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            screen = app.screen
            self.service.page.side_effect = slow
            screen.action_load()
            await pilot.pause(0.2)
            self.assertFalse(screen.loading)
            self.assertEqual(list(screen.query(LoadingIndicator)), [])
            release.set()
            await self.settle(app, pilot)

    async def test_the_same_data_keeps_the_table_and_its_selection(self):
        from herdboard.pr_reviews import PullRequest
        prs = [PullRequest(n, f"PR {n}", "u", "octo", False, "a" * 40, "b", False, False) for n in range(9, 0, -1)]
        self.service.page.return_value = ("acme/api", Page(prs, 9, None))
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await pilot.press("G")
            await self.settle(app, pilot)
            screen = app.screen
            await pilot.press("j", "j", "j")
            table = screen.query_one("#pr-list")
            screen.action_load()
            await self.settle(app, pilot)
            self.assertEqual(table.cursor_row, 3)
            self.assertEqual(screen.current().number, 6)

    async def test_each_key_can_be_clicked(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            text = self.bar(app).keys_text()
            clicks = {span.style.meta.get("@click") for span in text.spans if hasattr(span.style, "meta")}
        self.assertIn("app.simulate_key('a')", clicks)


if __name__ == "__main__":
    unittest.main()
