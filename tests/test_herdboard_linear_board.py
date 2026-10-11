import os
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

from herdboard.board import DetailScreen, HerdboardApp  # noqa: E402
from herdboard.linear_links import ProjectLink  # noqa: E402
from herdboard.linear_source import LinearSource  # noqa: E402
from herdboard.linear_writes import LinearWriter  # noqa: E402
from herdboard.views import Views  # noqa: E402
from test_herdboard_board import SIZE, BoardTestCase, fill, settle  # noqa: E402
from test_herdboard_linear_source import DEAD, LinearEnvTestCase  # noqa: E402


class LinearBoardCase(BoardTestCase, LinearEnvTestCase):
    def setUp(self):
        LinearEnvTestCase.setUp(self)
        BoardTestCase.setUp(self)
        self.gate = None
        self.store.update_links(self.repo_path, lambda _: [ProjectLink("p-plat", "Platform")])
        self.store.add(self.repo_path, "local todo", "Todo")

    def make_linear_app(self, view="repo", active=True, linear_interval=300.0, project=None, **kwargs):
        source = self.source_in_use = LinearSource(self.config)
        views = Views(self.store, self.config, source, self.identity, project)
        self.fetches = self.running = self.most_running = 0
        lock = threading.Lock()

        def fetch(name):
            with lock:
                self.fetches += 1
                self.running += 1
                self.most_running = max(self.most_running, self.running)
            try:
                if self.gate is not None:
                    self.gate.wait(5)
                return views.linear(name)
            finally:
                with lock:
                    self.running -= 1

        app = HerdboardApp(
            self.store, view, paths_for=views.paths, repo_board=self.repo_path,
            reload_interval=60.0, linear_for=fetch, linear_active=active,
            available=views.available, linear_interval=linear_interval,
            linear_writer=LinearWriter(self.config, source), **kwargs,
        )
        app.animation_level = "none"
        return app

    def title(self, app):
        return app.query_one("#title-bar").plain

    def notifications(self, app):
        return [(n.message, n.severity) for n in app._notifications]

    def select(self, app, ref):
        for col, data in enumerate(app.columns):
            for row, card in enumerate(data.cards):
                if card.ref == ref:
                    app.col, app.row = col, row
                    app._show_selection()
                    return
        self.fail(f"{ref} is not on the board")


class LinearBoardTests(LinearBoardCase):
    async def test_todos_show_first_then_linear_cards_after_the_worker_returns(self):
        self.gate = threading.Event()
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertEqual(self.headers(app)[1], "Todo 1")
            self.assertTrue(any("local todo" in c for c in self.cards(app)))
            self.assertIn("Linear: loading", self.title(app))
            self.gate.set()
            await settle(pilot)
            self.assertEqual(self.headers(app), ["Backlog 0", "Todo 3", "In progress 0", "Review 1", "Done 0"])
            self.assertRegex(self.title(app), r"Linear: \d\d:\d\d$")
            self.assertIn("view: repo", self.title(app))

    async def test_linear_cards_are_marked_and_show_pr(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            cards = self.cards(app)
            marked = [c for c in cards if "Issue 1" in c]
            self.assertEqual(len(marked), 1)
            self.assertIn("◆ ENG-1 · PR", marked[0])
            self.assertTrue(any("◆ ENG-2" in c and "PR" not in c for c in cards))
            self.assertFalse(any("◆" in c for c in cards if "local todo" in c))

    async def test_title_shows_stale_since_when_offline(self):
        await self.prime()
        self.endpoint = DEAD
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertRegex(self.title(app), r"Linear: stale since \d\d:\d\d$")
            self.assertTrue(any("Issue 1" in c for c in self.cards(app)))

    async def prime(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)

    async def test_title_off_without_linear(self):
        app = self.make_linear_app(active=False)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertIn("Linear: off", self.title(app))
            self.assertEqual(self.fake.requests, [])
            await pilot.press("v", "v", "v")
            await pilot.pause()
            self.assertEqual(app.view, "repo")

    async def test_view_cycles_through_mine(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            seen = []
            for _ in range(5):
                await pilot.press("v")
                await settle(pilot)
                seen.append(app.view)
            self.assertEqual(seen, ["global", "all", "mine", "repo", "global"])
            await pilot.press("v", "v")
            await settle(pilot)
            self.assertEqual(app.view, "mine")
            self.assertTrue(any("Issue 5" in c for c in self.cards(app)))
            self.assertFalse(any("local todo" in c for c in self.cards(app)))

    async def test_global_view_has_no_linear_cards(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("v")
            await settle(pilot)
            self.assertEqual(app.view, "global")
            self.assertFalse(any("◆" in c for c in self.cards(app)))

    async def test_write_keys_still_work_on_todos(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, [c.ref for c in app.columns[1].cards if c.source == "todo"][0])
            await pilot.press("L")
            await settle(pilot)
            self.assertEqual(app.columns[2].name, "In progress")
            self.assertEqual([c.title for c in app.columns[2].cards], ["local todo"])

    async def test_enter_shows_issue_detail(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-2")
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, DetailScreen)
            text = app.screen.query_one("#detail").plain
            for part in ["Issue 2", "Platform / Review", "ENG-2", "state: In Review", "team: ENG",
                         "https://linear.app/workspace/issue/ENG-2", "labels: repo:api, bug",
                         "due: 2026-10-20", "Line one", "Line two"]:
                self.assertIn(part, text)

    async def test_add_always_adds_a_todo(self):
        app = self.make_linear_app("mine")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("a")
            await pilot.pause()
            await fill(pilot, "from mine")
            titles = [c.title for c in self.store.list_cards([self.inbox_path])]
            self.assertEqual(titles, ["from mine"])
            self.assertTrue(any(c.ref == "ENG-1" for d in app.columns for c in d.cards))

    async def test_reload_key_and_interval_fetch_again(self):
        app = self.make_linear_app(linear_interval=0.3)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            first = len(self.requests("ProjectIssues"))
            await pilot.pause(0.5)
            await settle(pilot)
            second = len(self.requests("ProjectIssues"))
            self.assertGreater(second, first)
            await pilot.press("r")
            await settle(pilot)
            self.assertGreater(len(self.requests("ProjectIssues")), second)

    async def test_inaccessible_project_notifies(self):
        self.store.update_links(self.repo_path, lambda links: links + [ProjectLink("gone", "Old project")])
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            messages = [m for m, s in self.notifications(app) if s == "warning"]
            self.assertTrue(any("Old project" in m for m in messages))
            self.assertTrue(any("Issue 1" in c for c in self.cards(app)))

    async def test_authentication_error_notifies_and_keeps_todos(self):
        os.environ["LINEAR_API_KEY"] = "wrong-key"
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertTrue(any("uthentication" in m and s == "error" for m, s in self.notifications(app)))
            self.assertIn("Linear: authentication failed", self.title(app))
            self.assertTrue(any("local todo" in c for c in self.cards(app)))
            self.assertFalse(any("◆" in c for c in self.cards(app)))


class LinearBoardFixTests(LinearBoardCase):
    async def test_revoked_key_drops_linear_cards_and_keeps_todos(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertTrue(any("◆" in c for c in self.cards(app)))
            self.fake.responses.append((200, {"errors": [{"message": "Authentication required",
                                       "extensions": {"code": "AUTHENTICATION_ERROR"}}]}, {}))
            await pilot.press("r")
            await settle(pilot)
            self.assertIn("Linear: authentication failed", self.title(app))
            self.assertFalse(any("◆" in c for c in self.cards(app)))
            self.assertTrue(any("local todo" in c for c in self.cards(app)))
            self.assertTrue(any("uthentication" in m and s == "error" for m, s in self.notifications(app)))
            self.assertEqual(app.linear_results, {})
            # The next refresh asks Linear again and recovers.
            await pilot.press("r")
            await settle(pilot)
            self.assertRegex(self.title(app), r"Linear: \d\d:\d\d$")
            self.assertTrue(any("◆" in c for c in self.cards(app)))

    async def test_board_recovers_after_a_network_failure(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertRegex(self.title(app), r"Linear: \d\d:\d\d$")
            client = self.source_in_use.client
            client.endpoint = DEAD
            await pilot.press("r")
            await settle(pilot)
            self.assertRegex(self.title(app), r"Linear: stale since \d\d:\d\d$")
            self.assertTrue(any("Issue 1" in c for c in self.cards(app)))
            client.endpoint = self.endpoint
            count = len(self.fake.requests)
            await pilot.press("r")
            await settle(pilot)
            self.assertGreater(len(self.fake.requests), count)
            self.assertRegex(self.title(app), r"Linear: \d\d:\d\d$")

    async def test_file_checks_and_reloads_never_call_linear(self):
        app = self.make_linear_app("project", project="Mobile")
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertEqual(len(self.requests("Projects")), 1)
            self.assertTrue(any("Issue 5" in c for c in self.cards(app)))
            count = len(self.fake.requests)
            for _ in range(3):
                await app.check_files()
                await app.reload()
            await pilot.pause()
            self.assertEqual(len(self.fake.requests), count)
            await pilot.press("r")
            await settle(pilot)
            self.assertEqual(len(self.requests("Projects")), 1)

    async def test_one_fetch_runs_at_a_time_and_one_refresh_is_queued(self):
        self.fake.delay = 0.2
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            for _ in range(3):
                await pilot.press("r")
            await pilot.pause(0.1)
            await settle(pilot)
            await pilot.pause(0.1)
            await settle(pilot)
            self.assertEqual(self.most_running, 1)
            self.assertEqual(self.fetches, 2)
            self.assertRegex(self.title(app), r"Linear: \d\d:\d\d$")


if __name__ == "__main__":
    unittest.main()
