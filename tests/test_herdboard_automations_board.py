"""The Automations tab: G goes there after the Notes tab, the list of automations, and its keys."""
import asyncio
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/herdboard/src"))
from textual.widgets import Input, Select, TextArea  # noqa: E402

from herdboard import automations, board  # noqa: E402
from herdboard.board import (AutomationFormScreen, AutomationsScreen, ConfirmScreen, HerdboardApp,  # noqa: E402
                             KeyBar, NotesScreen, PullRequestsScreen, TabBar)
from herdboard.config import Config  # noqa: E402
from herdboard.pr_reviews import Page  # noqa: E402
from herdboard.store import Store  # noqa: E402


class AutomationsBoardTestCase(unittest.IsolatedAsyncioTestCase):
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

    def tab(self, app) -> str:
        return app.screen.query_one(TabBar).active

    def add(self, **fields):
        fields.setdefault("title", "Check security alerts")
        fields.setdefault("schedule", "every weekday at 07:30")
        return automations.add(self.store, **fields)

    async def open_tab(self, app, pilot):
        await pilot.press("G", "G", "G")
        await self.settle(app, pilot)
        self.assertIsInstance(app.screen, AutomationsScreen)
        return app.screen


class TabTests(AutomationsBoardTestCase):
    async def test_G_cycles_board_prs_notes_automations_board(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            seen = []
            for _ in range(5):
                seen.append(self.tab(app))
                await pilot.press("G")
                await self.settle(app, pilot)
                kinds = [type(screen) for screen in app.screen_stack]
                self.assertLessEqual(sum(kind in kinds for kind in (PullRequestsScreen, NotesScreen,
                                                                     AutomationsScreen)), 1, kinds)
                self.assertLessEqual(len(app.screen_stack), 2)
            self.assertEqual(seen, ["board", "prs", "notes", "automations", "board"])

    async def test_a_click_on_each_tab_goes_there_from_each_tab(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.settle(app, pilot)
            for source in ("board", "prs", "notes", "automations"):
                for target in ("board", "prs", "notes", "automations"):
                    app.show_tab(source)
                    await self.settle(app, pilot)
                    self.assertEqual(self.tab(app), source)
                    await pilot.click(f"#tab-{target}")
                    await self.settle(app, pilot)
                    self.assertEqual(self.tab(app), target, f"{source} -> {target}")
                    self.assertLessEqual(len(app.screen_stack), 2)

    async def test_the_tab_is_the_last_one(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.open_tab(app, pilot)
            self.assertEqual(app.screen.query_one(TabBar).tab_labels(),
                             ["▦ Board", "⑂ Pull requests 0", "✎ Notes", "↻ Automations"])

    async def test_the_board_keys_do_not_work_on_the_tab(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.open_tab(app, pilot)
            await pilot.press("d")
            await self.settle(app, pilot)
            card = self.store.list_cards([self.path])[0]
            self.assertFalse(card.checked)
            keys = " ".join(app.screen.query_one(KeyBar).keys_text().plain.split())
            self.assertIn("n Run now", keys)
            self.assertNotIn("Done", keys)
            self.assertNotIn("Start", keys)

    async def test_escape_goes_back_to_the_board(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.open_tab(app, pilot)
            await pilot.press("escape")
            await self.settle(app, pilot)
            self.assertEqual(self.tab(app), "board")


class ListTests(AutomationsBoardTestCase):
    async def test_rows_show_state_id_title_board_schedule_and_runs(self):
        added = self.add()
        paused = self.add(title="Weekly cleanup", schedule="every week on friday")
        automations.set_enabled(self.store, paused.id, False)
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_tab(app, pilot)
            rows = screen.rows()
            self.assertEqual(len(rows), 2)
            self.assertEqual(rows[0][1:5], (added.id, "Check security alerts", "inbox", "every weekday at 07:30"))
            self.assertEqual(rows[0][0], "on")
            self.assertEqual(rows[1][0], "paused")
            self.assertEqual(rows[1][5], "-")

    async def test_an_empty_list_says_how_to_add_one(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_tab(app, pilot)
            self.assertIn("a adds", screen.status_text())

    async def test_space_pauses_and_resumes(self):
        added = self.add()
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_tab(app, pilot)
            await pilot.press("space")
            await self.settle(app, pilot)
            self.assertFalse(automations.get(self.store.config, added.id).enabled)
            self.assertEqual(screen.rows()[0][0], "paused")
            await pilot.press("space")
            await self.settle(app, pilot)
            self.assertTrue(automations.get(self.store.config, added.id).enabled)

    async def test_n_runs_after_confirm(self):
        added = self.add()
        app = self.app()
        with patch.object(board.automations, "run",
                          return_value={"id": added.id, "card": "t123", "result": "started", "skipped": 0}) as run:
            async with app.run_test(size=(160, 30)) as pilot:
                await self.open_tab(app, pilot)
                await pilot.press("n")
                await self.settle(app, pilot)
                self.assertIsInstance(app.screen, ConfirmScreen)
                await pilot.press("y")
                await self.settle(app, pilot)
                self.assertEqual(run.call_count, 1)
                self.assertEqual(run.call_args.args[1].id, added.id)

    async def test_n_does_nothing_without_confirm(self):
        self.add()
        app = self.app()
        with patch.object(board.automations, "run") as run:
            async with app.run_test(size=(160, 30)) as pilot:
                await self.open_tab(app, pilot)
                await pilot.press("n", "n")
                await self.settle(app, pilot)
                run.assert_not_called()

    async def test_x_removes_after_confirm(self):
        self.add()
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            screen = await self.open_tab(app, pilot)
            await pilot.press("x")
            await self.settle(app, pilot)
            await pilot.press("y")
            await self.settle(app, pilot)
            self.assertEqual(automations.load(self.store.config), [])
            self.assertEqual(screen.rows(), [])

    async def test_P_plans_with_the_chosen_agent(self):
        added = self.add()
        app = self.app()
        planned = {"id": added.id, "agent": "claude", "model": "opus", "pane_id": "w1:p1", "plan": "x",
                   "workspace_id": "w1", "worktree_path": "/w", "branch": None, "base": None}
        with patch("herdboard.automation_plan.AutomationPlanner.plan", return_value=planned) as plan:
            async with app.run_test(size=(160, 30)) as pilot:
                await self.open_tab(app, pilot)
                await pilot.press("P")
                await self.settle(app, pilot)
                await pilot.press("enter")  # claude
                await self.settle(app, pilot)
                await pilot.press("down", "enter")  # opus
                await self.settle(app, pilot)
                plan.assert_called_once_with(added.id, agent="claude", model="opus", thinking=None)


class FormTests(AutomationsBoardTestCase):
    async def test_add_validates_the_schedule_then_saves(self):
        app = self.app()
        async with app.run_test(size=(160, 40)) as pilot:
            screen = await self.open_tab(app, pilot)
            await pilot.press("a")
            await self.settle(app, pilot)
            form = app.screen
            self.assertIsInstance(form, AutomationFormScreen)
            form.query_one("#automation-title", Input).value = "Say hello"
            form.query_one("#automation-schedule", Input).value = "now and then"
            form.query_one("#automation-body", TextArea).text = "Write hello.txt"
            await pilot.press("ctrl+s")
            await self.settle(app, pilot)
            self.assertIs(app.screen, form)
            self.assertIn("every weekday", form.error_text())
            form.query_one("#automation-schedule", Input).value = "every day at 10:00"
            await pilot.press("ctrl+s")
            await self.settle(app, pilot)
            self.assertIs(app.screen, screen)
            saved = automations.load(self.store.config)
            self.assertEqual([(a.title, a.schedule, a.body, a.board) for a in saved],
                             [("Say hello", "every day at 10:00", "Write hello.txt", "inbox")])
            self.assertEqual(screen.rows()[0][2], "Say hello")

    async def test_edit_keeps_the_fields_and_saves_changes(self):
        added = self.add(tags=["security"], may_merge_prs=True, body="Look")
        app = self.app()
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_tab(app, pilot)
            await pilot.press("e")
            await self.settle(app, pilot)
            form = app.screen
            self.assertEqual(form.query_one("#automation-title", Input).value, "Check security alerts")
            self.assertEqual(form.query_one("#automation-tags", Input).value, "security")
            self.assertEqual(form.query_one("#automation-board", Select).value, "inbox")
            form.query_one("#automation-title", Input).value = "Check all alerts"
            await pilot.press("ctrl+s")
            await self.settle(app, pilot)
            saved = automations.get(self.store.config, added.id)
            self.assertEqual(saved.title, "Check all alerts")
            self.assertEqual(saved.tags, ["security"])
            self.assertTrue(saved.may_merge_prs)
            self.assertEqual(saved.body, "Look")


if __name__ == "__main__":
    unittest.main()
