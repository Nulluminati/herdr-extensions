"""The Automations tab: G goes there after the Notes tab, the list of automations, and its keys."""

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from textual.containers import VerticalScroll
from textual.widgets import Input, Select, TextArea

from herdboard import automations, board
from herdboard.board import (
    AutomationFormScreen,
    AutomationsScreen,
    ConfirmScreen,
    HerdboardApp,
    KeyBar,
    ModelScreen,
    NotesScreen,
    PullRequestsScreen,
    TabBar,
)
from herdboard.config import Config
from herdboard.pr_reviews import Page
from herdboard.store import Store


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
                self.assertLessEqual(
                    sum(kind in kinds for kind in (PullRequestsScreen, NotesScreen, AutomationsScreen)), 1, kinds
                )
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
            self.assertEqual(
                app.screen.query_one(TabBar).tab_labels(), ["▦ Board", "⑂ Pull requests 0", "✎ Notes", "↻ Automations"]
            )

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

    async def test_the_key_bar_shows_back(self):
        app = self.app()
        async with app.run_test(size=(160, 30)) as pilot:
            await self.open_tab(app, pilot)
            keys = " ".join(app.screen.query_one(KeyBar).keys_text().plain.split())
            self.assertIn("Back", keys)


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
        with patch.object(
            board.automations, "run", return_value={"id": added.id, "card": "t123", "result": "started", "skipped": 0}
        ) as run:
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
        planned = {
            "id": added.id,
            "agent": "claude",
            "model": "opus",
            "pane_id": "w1:p1",
            "plan": "x",
            "workspace_id": "w1",
            "worktree_path": "/w",
            "branch": None,
            "base": None,
        }
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

    async def test_P_plans_with_codex_and_its_thinking_level(self):
        added = self.add()
        app = self.app()
        planned = {
            "id": added.id,
            "agent": "codex",
            "model": "gpt-6.1",
            "pane_id": "w1:p1",
            "plan": "x",
            "workspace_id": "w1",
            "worktree_path": "/w",
            "branch": None,
            "base": None,
        }
        with (
            patch("herdboard.automation_plan.AutomationPlanner.plan", return_value=planned) as plan,
            patch("herdboard.board.codex_model_choices", return_value=(None, ["gpt-6.1"])),
        ):
            async with app.run_test(size=(160, 30)) as pilot:
                await self.open_tab(app, pilot)
                await pilot.press("P")
                await self.settle(app, pilot)
                await pilot.press("down", "down", "enter")  # codex
                await self.settle(app, pilot)
                self.assertIsInstance(app.screen, ModelScreen)
                app.screen.query_one("#thinking").value = "ultra"
                await pilot.press("down", "enter")  # gpt-6.1
                await self.settle(app, pilot)
                plan.assert_called_once_with(added.id, agent="codex", model="gpt-6.1", thinking="ultra")


class TimerNoticeTests(AutomationsBoardTestCase):
    """The notice in the status line when the OS timer that runs the tick is not active."""

    def timer(self, active=True, backend="systemd", state="active"):
        return {
            "backend": backend,
            "active": active,
            "state": state,
            "installed": active,
            "files": [],
        }

    def status(self, **options):
        return patch.object(automations, "timer", return_value=self.timer(**options), create=True)

    async def test_an_active_timer_shows_no_notice(self):
        self.add()
        app = self.app()
        with self.status():
            async with app.run_test(size=(160, 30)) as pilot:
                screen = await self.open_tab(app, pilot)
                self.assertNotIn("do not run on their own", screen.status_text())
                self.assertIn("1 automation", screen.status_text())

    async def test_an_inactive_timer_shows_the_notice_and_its_fix(self):
        self.add()
        app = self.app()
        with self.status(active=False, state="inactive"):
            async with app.run_test(size=(160, 30)) as pilot:
                screen = await self.open_tab(app, pilot)
                text = screen.status_text()
                self.assertIn("Automations do not run on their own", text)
                self.assertIn("the systemd timer is inactive", text)
                self.assertIn("herdboard automation timer install", text)

    async def test_a_timer_status_error_shows_the_notice(self):
        self.add()
        app = self.app()
        error = automations.AutomationError(
            "Automations need systemd (Linux) or launchd (macOS)", "unsupported_platform"
        )
        with patch.object(automations, "timer", side_effect=error, create=True):
            async with app.run_test(size=(160, 30)) as pilot:
                screen = await self.open_tab(app, pilot)
                text = screen.status_text()
                self.assertIn("Automations do not run on their own", text)
                self.assertIn("need systemd", text)
                self.assertIn("herdboard automation timer install", text)

    async def test_a_paused_automation_alone_shows_no_notice(self):
        paused = self.add()
        automations.set_enabled(self.store, paused.id, False)
        app = self.app()
        with self.status(active=False, state="inactive"):
            async with app.run_test(size=(160, 30)) as pilot:
                screen = await self.open_tab(app, pilot)
                self.assertNotIn("do not run on their own", screen.status_text())

    async def test_the_notice_stays_when_the_list_is_read_again(self):
        self.add()
        app = self.app()
        with self.status(active=False, state="inactive") as timer:
            async with app.run_test(size=(160, 30)) as pilot:
                screen = await self.open_tab(app, pilot)
                reads = timer.call_count
                await pilot.press("r")
                await self.settle(app, pilot)
                self.assertIn("do not run on their own", screen.status_text())
                # A reload does not run systemctl again: the status is checked on mount and on resume.
                self.assertEqual(timer.call_count, reads)

    async def test_the_timer_is_read_again_when_the_tab_comes_back(self):
        self.add()
        app = self.app()
        with self.status(active=False, state="inactive") as timer:
            async with app.run_test(size=(160, 30)) as pilot:
                screen = await self.open_tab(app, pilot)
                await pilot.press("escape")
                await self.settle(app, pilot)
                await pilot.press("G", "G", "G")
                await self.settle(app, pilot)
                self.assertIs(app.screen, screen)
                self.assertGreater(timer.call_count, 1)
                self.assertIn("do not run on their own", screen.status_text())

    async def test_i_installs_the_timer_after_confirm(self):
        self.add()
        app = self.app()
        self.actions = []
        state = {"installed": False}

        def timer(action, config_file=None):
            self.actions.append(action)
            if action == "install":
                state["installed"] = True
            active = state["installed"]
            return self.timer(active=active, state="active" if active else "inactive")

        with patch.object(automations, "timer", side_effect=timer, create=True):
            async with app.run_test(size=(160, 30)) as pilot:
                screen = await self.open_tab(app, pilot)
                self.assertIn("do not run on their own", screen.status_text())
                await pilot.press("i")
                await self.settle(app, pilot)
                self.assertIsInstance(app.screen, ConfirmScreen)
                await pilot.press("y")
                await self.settle(app, pilot)
                self.assertIn("install", self.actions)
                self.assertEqual(self.actions[-1], "status")
                self.assertNotIn("do not run on their own", screen.status_text())


class FormTests(AutomationsBoardTestCase):
    async def test_long_board_names_fit_the_choice_and_dropdown(self):
        name = "nulluminati-herdr-extensions"
        app = self.app()
        async with app.run_test(size=(160, 40)) as pilot:
            await self.settle(app, pilot)
            form = AutomationFormScreen(
                "Add an automation", ["inbox", name], self.store.config.types, Mock(), self.store.config
            )
            app.push_screen(form)
            await pilot.pause()
            select = form.query_one("#automation-board", Select)
            select.value = name
            await pilot.pause()
            # Leave room for the dropdown arrow as well as the complete board name.
            self.assertGreaterEqual(select.content_size.width, len(name) + 4)
            select.focus()
            await pilot.press("enter")
            await pilot.pause()
            options = select.query_one("SelectOverlay")
            self.assertGreaterEqual(options.content_size.width, len(name))
            await pilot.resize_terminal(80, 24)
            await pilot.pause()
            self.assertGreaterEqual(select.content_size.width, len(name) + 4)
            self.assertGreaterEqual(options.content_size.width, len(name))

    async def test_small_terminal_can_reach_the_end_of_the_form(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_form(app, pilot)
            dialog = form.query_one(".dialog", VerticalScroll)
            self.assertLessEqual(dialog.region.right, 80)
            self.assertLessEqual(dialog.region.bottom, 24)
            self.assertGreater(dialog.max_scroll_y, 0)
            form.query_one("#automation-body", TextArea).focus()
            await pilot.pause()
            self.assertTrue(
                dialog.scrollable_content_region.contains_region(form.query_one("#automation-body", TextArea).region)
            )
            hint = list(form.query(".dim"))[-1]
            hint.scroll_visible(animate=False, immediate=True)
            await pilot.pause()
            self.assertGreater(dialog.scroll_y, 0)
            self.assertTrue(dialog.scrollable_content_region.contains_region(hint.region))

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
            self.assertEqual(
                [(a.title, a.schedule, a.body, a.board) for a in saved],
                [("Say hello", "every day at 10:00", "Write hello.txt", "inbox")],
            )
            self.assertEqual(screen.rows()[0][2], "Say hello")

    async def test_the_form_offers_codex_and_its_thinking_levels(self):
        app = self.app()
        async with app.run_test(size=(160, 40)) as pilot:
            await self.open_tab(app, pilot)
            await pilot.press("a")
            await self.settle(app, pilot)
            form = app.screen
            form.query_one("#automation-title", Input).value = "Say hello"
            form.query_one("#automation-schedule", Input).value = "every day at 10:00"
            form.query_one("#automation-agent", Select).value = "codex"
            await pilot.pause()
            form.query_one("#automation-thinking", Select).value = "ultra"
            await pilot.press("ctrl+s")
            await self.settle(app, pilot)
            saved = automations.load(self.store.config)
            self.assertEqual([(a.agent, a.thinking) for a in saved], [("codex", "ultra")])

    def options(self, form, name: str) -> list[str]:
        return [value for _, value in form.query_one(f"#automation-{name}", Select)._options]

    async def open_form(self, app, pilot, key: str = "a"):
        await self.open_tab(app, pilot)
        await pilot.press(key)
        await self.settle(app, pilot)
        self.assertIsInstance(app.screen, AutomationFormScreen)
        return app.screen

    async def test_the_model_and_thinking_options_follow_the_agent(self):
        from herdboard.config import CLAUDE_THINKING_LEVELS, CODEX_THINKING_LEVELS, PI_THINKING_LEVELS

        app = self.app()
        with (
            patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3"])),
            patch("herdboard.board.codex_model_choices", return_value=(None, ["gpt-6.1"])),
        ):
            async with app.run_test(size=(160, 40)) as pilot:
                form = await self.open_form(app, pilot)
                # "default" is the configured start agent, Claude.
                self.assertEqual(self.options(form, "model"), ["default", "opus", "sonnet", "haiku"])
                self.assertEqual(self.options(form, "thinking"), ["default", *CLAUDE_THINKING_LEVELS])
                for agent, models, levels in (
                    ("pi", ["zai/glm-5.3"], PI_THINKING_LEVELS),
                    ("codex", ["gpt-6.1"], CODEX_THINKING_LEVELS),
                    ("claude", ["opus", "sonnet", "haiku"], CLAUDE_THINKING_LEVELS),
                ):
                    with self.subTest(agent=agent):
                        form.query_one("#automation-agent", Select).value = agent
                        await pilot.pause()
                        self.assertEqual(self.options(form, "model"), ["default", *models])
                        self.assertEqual(self.options(form, "thinking"), ["default", *levels])

    async def test_an_agent_change_keeps_only_a_valid_model_and_level(self):
        app = self.app()
        with patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3"])):
            async with app.run_test(size=(160, 40)) as pilot:
                form = await self.open_form(app, pilot)
                form.query_one("#automation-model", Select).value = "opus"
                form.query_one("#automation-thinking", Select).value = "high"
                form.query_one("#automation-agent", Select).value = "pi"
                await pilot.pause()
                self.assertEqual(form.query_one("#automation-model", Select).value, "default")
                self.assertEqual(form.query_one("#automation-thinking", Select).value, "high")
                form.query_one("#automation-thinking", Select).value = "off"
                form.query_one("#automation-agent", Select).value = "claude"
                await pilot.pause()
                self.assertEqual(form.query_one("#automation-thinking", Select).value, "default")

    async def test_the_form_saves_a_claude_model_and_effort(self):
        app = self.app()
        async with app.run_test(size=(160, 40)) as pilot:
            form = await self.open_form(app, pilot)
            form.query_one("#automation-title", Input).value = "Say hello"
            form.query_one("#automation-schedule", Input).value = "every day at 10:00"
            form.query_one("#automation-agent", Select).value = "claude"
            await pilot.pause()
            form.query_one("#automation-model", Select).value = "sonnet"
            form.query_one("#automation-thinking", Select).value = "max"
            await pilot.press("ctrl+s")
            await self.settle(app, pilot)
            saved = automations.load(self.store.config)
            self.assertEqual([(a.agent, a.model, a.thinking) for a in saved], [("claude", "sonnet", "max")])

    async def test_edit_keeps_a_saved_model_that_is_not_in_the_list(self):
        added = self.add(agent="pi", model="custom/model")
        app = self.app()
        with patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3"])):
            async with app.run_test(size=(160, 40)) as pilot:
                form = await self.open_form(app, pilot, "e")
                self.assertEqual(self.options(form, "model"), ["default", "zai/glm-5.3", "custom/model"])
                self.assertEqual(form.query_one("#automation-model", Select).value, "custom/model")
                form.query_one("#automation-title", Input).value = "Check all alerts"
                await pilot.press("ctrl+s")
                await self.settle(app, pilot)
                self.assertEqual(automations.get(self.store.config, added.id).model, "custom/model")

    async def test_the_merge_checkbox_says_it_is_an_instruction_only(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            form = await self.open_form(app, pilot)
            checkbox = form.query_one("#automation-merge")
            label = str(checkbox.label)
            self.assertIn("Tell the agent", label)
            self.assertGreaterEqual(checkbox.content_size.width, len(label) + 4)
            self.assertTrue(form.query_one(".dialog").content_region.contains_region(checkbox.region))
            self.assertIn("no extra permissions", str(form.query_one("#automation-merge-help").render()))

    async def test_the_factory_checkbox_is_refused_on_the_inbox(self):
        app = self.app()
        async with app.run_test(size=(160, 40)) as pilot:
            form = await self.open_form(app, pilot)
            self.assertIn("4 reviewers per round", str(form.query_one("#automation-factory-help").render()))
            form.query_one("#automation-title", Input).value = "Fix lint"
            form.query_one("#automation-schedule", Input).value = "every day"
            form.query_one("#automation-factory").value = True
            await pilot.press("ctrl+s")
            await self.settle(app, pilot)
            self.assertIs(app.screen, form)
            self.assertIn("A factory automation needs a repository board.", form.error_text())
            self.assertEqual(automations.load(self.store.config), [])

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
