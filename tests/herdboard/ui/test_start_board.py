"""Start actions run through a thread worker, not the event loop."""

import asyncio
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from textual.widgets import Input

from herdboard.board import AgentScreen, ClaudeModelScreen, HerdboardApp, ModelScreen, ReviewKindScreen, TargetScreen
from herdboard.config import Config, load_config
from herdboard.identity import RepoIdentity
from herdboard.model import HerdboardError
from herdboard.start import StartError
from herdboard.store import Store


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
        self.root = root
        self.config_file = root / "config.toml"
        self.path = self.store.ensure_inbox()
        self.card = self.store.add(self.path, "Start test")
        self.merge = Mock(
            return_value={
                "ref": self.card.ref,
                "mode": "local",
                "base": "main",
                "commit": "abcdef12",
                "worktree_removed": True,
                "branch_deleted": True,
            }
        )
        self.finish = Mock(return_value={"ref": self.card.ref, "worktree_removed": True, "branch_deleted": False})
        self.review = Mock(
            return_value={
                "ref": self.card.ref,
                "workspace_id": "w1",
                "pane_id": "w1:p1",
                "review_pane": "w1:p2",
                "agent_restarted": False,
            }
        )
        self.agent_review = Mock(
            return_value={
                "ref": self.card.ref,
                "reviewer": "claude",
                "model": "opus",
                "pane_id": "w1:p3",
                "folder": "/f",
                "then_hunk": False,
            }
        )
        self.plan = Mock(
            return_value={
                "ref": self.card.ref,
                "agent": "claude",
                "model": "opus",
                "pane_id": "w1:p7",
                "checkout": "/repo",
                "plan": "/plan.md",
            }
        )
        self.go = Mock(return_value={"ref": self.card.ref, "workspace_id": "w1"})
        self.callback = Mock(
            return_value={
                "ref": self.card.ref,
                "workspace_id": "w1",
                "pane_id": "w1:p1",
                "worktree_path": "/tmp/work",
                "branch": "test",
                "agent_name": "hb-test",
                "steps_done": [],
            }
        )

    def app(self):
        app = HerdboardApp(
            self.store,
            "inbox",
            lambda _: [self.path],
            reload_interval=60,
            start_card=self.callback,
            review_card=self.review,
            merge_card=self.merge,
            finish_card=self.finish,
            agent_review_card=self.agent_review,
            plan_card=self.plan,
            go_card=self.go,
            config_file=self.config_file,
        )
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

    async def test_factory_key_opens_presets_and_starts_default(self):
        from herdboard import factory
        from herdboard.board import FactoryPresetScreen

        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("F")
            await pilot.pause()
            self.assertIsInstance(app.screen, FactoryPresetScreen)
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.callback.assert_called_with(
                self.card.ref, "claude", factory_run=factory.default_run(self.store.config)
            )

    async def test_factory_custom_lenses_validation_and_back(self):
        from textual.widgets import Checkbox, Static

        from herdboard.board import FactoryCustomScreen, FactoryPresetScreen

        app = self.app()
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.press("F", "c")
            await pilot.pause()
            self.assertIsInstance(app.screen, FactoryCustomScreen)
            self.assertTrue(app.screen.query_one("#factory-on-code", Checkbox).disabled)
            app.screen.query_one("#factory-plan-lenses", Checkbox).value = False
            await pilot.pause()
            for lens in ("security", "ux", "ui", "code"):
                app.screen.query_one(f"#factory-on-{lens}", Checkbox).value = False
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertIn("lens", str(app.screen.query_one("#factory-error", Static).render()))
            self.callback.assert_not_called()
            await pilot.press("escape")
            self.assertIsInstance(app.screen, FactoryPresetScreen)

    async def test_factory_plan_choice_controls_manual_reviewer_boxes(self):
        from textual.widgets import Checkbox, Select

        app = self.app()
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.press("F", "c")
            await pilot.pause()
            form = app.screen
            plan = form.query_one("#factory-plan-lenses", Checkbox)
            code = form.query_one("#factory-on-code", Checkbox)
            for lens in ("security", "ux", "ui", "code"):
                checkbox = form.query_one(f"#factory-on-{lens}", Checkbox)
                self.assertFalse(checkbox.visible)
                self.assertTrue(checkbox.disabled)
            fields = list(form.query_one("#factory-fields").children)
            self.assertLess(fields.index(plan), fields.index(form.query_one("#factory-reviewers-heading")))
            model_width = form.query_one("#factory-model-code", Select).region.width
            await pilot.click("#factory-plan-lenses")
            await pilot.pause()
            self.assertTrue(code.visible)
            self.assertFalse(code.disabled)
            self.assertEqual(form.query_one("#factory-model-code", Select).region.width, model_width)
            await pilot.click("#factory-on-code")
            self.assertFalse(code.value)
            await pilot.click("#factory-plan-lenses")
            await pilot.pause()
            self.assertFalse(code.visible)
            self.assertFalse(code.value)
            await pilot.click("#factory-plan-lenses")
            await pilot.pause()
            self.assertTrue(code.visible)
            self.assertFalse(code.value)
            await pilot.press("ctrl+s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(self.callback.call_args.kwargs["factory_run"].lenses_on, ("security", "ux", "ui"))

    async def test_factory_custom_enter_in_save_name_starts_run(self):
        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("F", "c", "shift+tab")
            await pilot.pause()
            self.assertEqual(app.focused.id, "factory-save-name")
            app.focused.value = "keyboard"
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertEqual(self.callback.call_args.kwargs["factory_run"].name, "keyboard")
            self.assertIn("keyboard", load_config(self.config_file).factory_presets)

    async def test_factory_custom_enter_in_rounds_checks_values(self):
        from textual.widgets import Static

        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("F", "c", "shift+tab", "shift+tab")
            await pilot.pause()
            self.assertEqual(app.focused.id, "factory-rounds")
            app.focused.value = "0"
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("Rounds", str(app.screen.query_one("#factory-error", Static).render()))
            self.callback.assert_not_called()

    async def test_factory_custom_long_save_error_stays_visible(self):
        from textual.widgets import Static

        message = "/a/very/long/configuration/path/" * 4 + "config.toml: cannot save settings: permission denied"
        app = self.app()
        async with app.run_test(size=(86, 24)) as pilot:
            await pilot.press("F", "c", "shift+tab")
            await pilot.pause()
            app.focused.value = "cheap"
            with patch("herdboard.board.save_factory_preset", side_effect=HerdboardError(message)):
                await pilot.press("ctrl+s")
                await pilot.pause()
            form = app.screen
            error = form.query_one("#factory-error", Static)
            self.assertIn(message, str(error.render()))
            self.assertGreaterEqual(error.region.height, 2)
            self.assertTrue(form.query_one(".dialog").content_region.contains_region(error.region))
            self.callback.assert_not_called()

    async def test_factory_custom_groups_the_four_reviewer_roles(self):
        from textual.widgets import Label, Select

        app = self.app()
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.press("F", "c")
            await pilot.pause()
            form = app.screen
            heading = form.query_one("#factory-reviewers-heading", Label)
            self.assertEqual(str(heading.render()), "Reviewers")
            rows = list(form.query(".factory-reviewer-row"))
            self.assertEqual([str(row.query_one(Label).render()) for row in rows], ["security", "ux", "ui", "code"])
            for row in rows:
                self.assertEqual(row.query_one(Label).styles.padding.left, 2)
            builder = form.query_one("#factory-agent-builder", Select).parent
            approver = form.query_one("#factory-agent-approver", Select).parent
            self.assertNotIn("factory-reviewer-row", builder.classes)
            self.assertNotIn("factory-reviewer-row", approver.classes)
            fields = list(form.query_one("#factory-fields").children)
            self.assertLess(fields.index(builder), fields.index(heading))
            self.assertLess(fields.index(heading), fields.index(rows[0]))
            self.assertLess(fields.index(rows[-1]), fields.index(approver))
            self.assertEqual(form.query_one("#factory-fields").max_scroll_y, 0)

    async def test_factory_custom_narrow_columns_and_spacing(self):
        from textual.widgets import Label

        app = self.app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.press("F", "c")
            await pilot.pause()
            form = app.screen
            fields = form.query_one("#factory-fields")
            self.assertEqual(fields.styles.scrollbar_size_vertical, 1)
            self.assertEqual((fields.styles.margin.top, fields.styles.margin.bottom), (1, 1))
            header = form.query_one(".factory-role-header > .factory-role-on", Label)
            self.assertEqual(header.styles.content_align_horizontal, "center")
            label = form.query_one("#factory-thinking-builder #label")
            self.assertGreaterEqual(label.content_size.width, len("default"))
            subtitle = form.query_one(".dialog").border_subtitle
            self.assertIn("ctrl+s/enter: start", subtitle)
            self.assertIn("tab: next field", subtitle)
            self.assertFalse(form.query("#factory-custom-hint"))

    async def test_factory_custom_layout_and_error_stay_visible_in_small_pane(self):
        from textual.widgets import Checkbox, Label, Select, Static

        app = self.app()
        async with app.run_test(size=(90, 24)) as pilot:
            await pilot.press("F", "c")
            await pilot.pause()
            form = app.screen
            self.assertEqual(form.query_one(".factory-role-header").size.height, 1)
            role_row = form.query_one("#factory-agent-builder", Select).parent
            self.assertEqual(role_row.query_one(Label).size.height, 3)
            form.query_one("#factory-plan-lenses", Checkbox).value = False
            await pilot.pause()
            checkbox = form.query_one("#factory-on-code", Checkbox)
            self.assertEqual(checkbox.region.height, 3)
            self.assertGreaterEqual(checkbox.content_size.width, 5)
            save_name = form.query_one("#factory-save-name", Input)
            self.assertTrue(save_name.parent.content_region.contains_region(save_name.region))
            self.assertEqual(
                form.query_one("#factory-rounds", Input).region.y,
                form.query_one("#factory-rounds-label", Label).region.y,
            )
            self.assertEqual(
                form.query_one("#factory-save-name", Input).region.y,
                form.query_one("#factory-save-label", Label).region.y,
            )
            form.query_one("#factory-rounds", Input).value = "0"
            await pilot.press("ctrl+s")
            await pilot.pause()
            error = form.query_one("#factory-error", Static)
            self.assertIn("Rounds", str(error.render()))
            self.assertTrue(form.query_one(".dialog").content_region.contains_region(error.region))
            self.assertIn("esc: back", form.query_one(".dialog").border_subtitle)
            self.assertFalse(form.query("#factory-custom-hint"))
            self.callback.assert_not_called()

    async def test_factory_custom_lens_checkbox_has_visible_focus(self):
        from textual.widgets import Checkbox

        app = self.app()
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.press("F", "c")
            await pilot.pause()
            form = app.screen
            self.assertEqual(form.query_one("#factory-fields").max_scroll_y, 0)
            form.query_one("#factory-plan-lenses", Checkbox).value = False
            await pilot.pause()
            checkbox = form.query_one("#factory-on-code", Checkbox)
            checkbox.focus()
            await pilot.pause()
            self.assertEqual(checkbox.styles.border_top[0], "round")
            self.assertGreater(checkbox.styles.border_top[1].a, 0)

    async def test_factory_preset_summary_custom_reset_and_save(self):
        from dataclasses import replace

        from textual.widgets import Checkbox, OptionList, Select, Static

        from herdboard import factory
        from herdboard.board import ConfirmScreen, FactoryCustomScreen
        from herdboard.config import Role

        run = replace(
            factory.default_run(self.store.config),
            name="cheap",
            builder=Role("claude", "sonnet", "high"),
            rounds=2,
            lenses_on=("code",),
        )
        self.store.config.factory_presets["cheap"] = run
        app = self.app()
        async with app.run_test(size=(110, 50)) as pilot:
            await pilot.press("F", "down")
            await pilot.pause()
            self.assertEqual(app.screen.query_one("#factory-presets", OptionList).highlighted, 1)
            self.assertIn("sonnet", str(app.screen.query_one("#factory-summary", Static).render()))
            await pilot.press("c")
            await pilot.pause()
            form = app.screen
            self.assertIsInstance(form, FactoryCustomScreen)
            self.assertEqual(app.focused.id, "factory-agent-builder")
            await pilot.press("shift+tab")
            self.assertEqual(app.focused.id, "factory-save-name")
            await pilot.press("tab")
            self.assertEqual(form.query_one("#factory-model-builder", Select).value, "sonnet")
            self.assertFalse(form.query_one("#factory-plan-lenses", Checkbox).value)
            self.assertFalse(form.query_one("#factory-on-security", Checkbox).value)
            self.assertTrue(form.query_one("#factory-on-code", Checkbox).value)
            self.assertGreaterEqual(form.query_one(".factory-role-row Label").size.width, 10)
            form.query_one("#factory-agent-builder", Select).value = "codex"
            await pilot.pause()
            self.assertEqual(form.query_one("#factory-model-builder", Select).value, "")
            self.assertEqual(form.query_one("#factory-thinking-builder", Select).value, "")
            form.query_one("#factory-save-name", Input).value = "cheap"
            await pilot.press("ctrl+s")
            await pilot.pause()
            self.assertIsInstance(app.screen, ConfirmScreen)
            self.callback.assert_not_called()
            await pilot.press("n")
            await pilot.pause()
            self.assertIs(app.screen, form)
            await pilot.press("ctrl+s", "y")
            await app.workers.wait_for_complete()
            await pilot.pause()
            saved = load_config(self.config_file).factory_presets["cheap"]
            self.assertEqual(saved.builder.agent, "codex")
            self.assertEqual(self.callback.call_args.kwargs["factory_run"], saved)

    async def test_factory_preset_enter_starts_named_run(self):
        from dataclasses import replace

        from herdboard import factory

        run = replace(factory.default_run(self.store.config), name="cheap", rounds=2)
        self.store.config.factory_presets["cheap"] = run
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("F", "down", "enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.callback.assert_called_with(self.card.ref, run.builder.agent, factory_run=run)

    async def test_pi_model_dialog_ignores_codex_only_saved_levels(self):
        self.store.config.thinking = {"pi/default": "ultra"}
        app = self.app()
        with (
            patch("herdboard.board.pi_model_choices", return_value=(None, ["pi/default"])),
            patch("herdboard.start.pi_default_model", return_value="pi/default"),
        ):
            async with app.run_test() as pilot:
                app._chosen_agent(self.card.ref, "pi")
                await pilot.pause()
                self.assertIsInstance(app.screen, ModelScreen)
                self.assertEqual(app.screen.query_one("#thinking").value, "default")

    async def test_codex_start_plan_and_review_use_codex_models(self):
        from textual.widgets import OptionList, Select

        from herdboard.board import AGENT_LOGOS
        from herdboard.config import CODEX_THINKING_LEVELS

        self.assertEqual(AGENT_LOGOS.get("codex"), "\ue1a1")
        for action in ("start", "plan", "review"):
            with (
                self.subTest(action=action),
                patch("herdboard.board.codex_model_choices", return_value=("gpt-6.1, high", ["gpt-6.1"])),
            ):
                app = self.app()
                async with app.run_test() as pilot:
                    if action == "start":
                        await pilot.press("S")
                    elif action == "plan":
                        app.action_plan()
                    else:
                        app._chosen_reviewer(self.card.ref, "codex", False)
                    await pilot.pause()
                    if action != "review":
                        choices = app.screen.query_one(OptionList)
                        self.assertEqual(choices.option_count, 3)
                        await pilot.press("down", "down", "enter")
                        await pilot.pause()
                    self.assertIsInstance(app.screen, ModelScreen)
                    self.assertEqual(app.screen.labels, ["codex default (gpt-6.1, high)", "gpt-6.1"])
                    thinking = app.screen.query_one("#thinking", Select)
                    self.assertEqual([value for _, value in thinking._options], ["default", *CODEX_THINKING_LEVELS])
                    thinking.value = "ultra"
                    await pilot.press("down", "enter")
                    await app.workers.wait_for_complete()
                    await pilot.pause()
                if action == "start":
                    self.callback.assert_called_with(self.card.ref, "codex", model="gpt-6.1", thinking="ultra")
                elif action == "plan":
                    self.plan.assert_called_with(self.card.ref, agent="codex", model="gpt-6.1", thinking="ultra")
                else:
                    self.agent_review.assert_called_with(
                        self.card.ref, reviewer="codex", model="gpt-6.1", thinking="ultra", then_hunk=False
                    )

    async def test_choose_a_pi_model_and_thinking_level(self):
        app = self.app()
        with patch(
            "herdboard.board.pi_model_choices",
            return_value=("openai-codex/gpt-6.1-sol, high", ["zai/glm-5.3", "openai-codex/gpt-6-sol"]),
        ):
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, ModelScreen)
                self.assertEqual(
                    screen.labels,
                    ["pi default (openai-codex/gpt-6.1-sol, high)", "zai/glm-5.3", "openai-codex/gpt-6-sol"],
                )
                # The level belongs to the highlighted model, so it is chosen after the cursor moves.
                await pilot.press("down")
                await pilot.pause()
                screen.query_one("#thinking").value = "medium"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.callback.assert_called_with(self.card.ref, "pi", model="zai/glm-5.3", thinking="medium")

    async def test_the_chosen_level_is_saved_for_its_model(self):
        app = self.app()
        with patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3", "openai/gpt-5"])):
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                await pilot.press("down")
                await pilot.pause()
                app.screen.query_one("#thinking").value = "xhigh"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.assertEqual(load_config(app.config_file).thinking, {"zai/glm-5.3": "xhigh"})
        self.assertEqual(self.store.config.thinking, {"zai/glm-5.3": "xhigh"})
        # The next dialog offers the saved level for the same model.
        app2 = self.app()
        with patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3", "openai/gpt-5"])):
            async with app2.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                await pilot.press("down")
                await pilot.pause()
                self.assertEqual(app2.screen.query_one("#thinking").value, "xhigh")

    async def test_a_thinking_level_that_does_not_save_does_not_stop_the_start(self):
        app = self.app()
        notices = []
        app.notify = lambda message, **kwargs: notices.append((message, kwargs.get("severity")))
        app.config_file = self.root / "missing-folder" / "config.toml"
        with (
            patch("herdboard.board.save_thinking_levels", side_effect=HerdboardError("read-only directory")),
            patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3"])),
        ):
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                await pilot.press("down")
                await pilot.pause()
                app.screen.query_one("#thinking").value = "max"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.callback.assert_called_with(self.card.ref, "pi", model="zai/glm-5.3", thinking="max")
        self.assertIn(("The thinking level did not save: read-only directory", "warning"), notices)
        self.assertEqual(self.store.config.thinking, {})

    async def test_choosing_the_agent_default_forgets_the_level_of_that_model(self):
        app = self.app()
        app.config_file.write_text('[thinking]\n"zai/glm-5.3" = "high"\n')
        self.store.config.thinking = {"zai/glm-5.3": "high"}
        with patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3"])):
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                await pilot.press("down")
                await pilot.pause()
                self.assertEqual(app.screen.query_one("#thinking").value, "high")
                app.screen.query_one("#thinking").value = "default"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.assertEqual(load_config(app.config_file).thinking, {})
        self.assertEqual(self.store.config.thinking, {})

    async def test_the_thinking_level_follows_the_model_under_the_cursor(self):
        self.store.config.thinking = {"zai/glm-5.3": "xhigh", "openai-codex/gpt-6-sol": "minimal"}
        app = self.app()
        with patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3", "openai-codex/gpt-6-sol"])):
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                thinking = app.screen.query_one("#thinking")
                # pi's own default model has no remembered level.
                self.assertEqual(thinking.value, "default")
                for key, level in (("down", "xhigh"), ("down", "minimal"), ("up", "xhigh")):
                    await pilot.press(key)
                    await pilot.pause()
                    self.assertEqual(thinking.value, level, key)
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.callback.assert_called_with(self.card.ref, "pi", model="zai/glm-5.3", thinking="xhigh")

    async def test_the_default_row_shows_the_level_of_the_model_that_runs(self):
        # start_model runs for the unnamed row, so its level is the one that the row shows.
        self.store.config.start_agent = "pi"
        self.store.config.start_model = "start/one"
        self.store.config.thinking = {"start/one": "xhigh", "openai-codex/gpt-6.1-sol": "max"}
        app = self.app()
        with patch(
            "herdboard.board.pi_model_choices", return_value=("openai-codex/gpt-6.1-sol, high", ["zai/glm-5.3"])
        ):
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, ModelScreen)
                self.assertEqual(screen.query_one("#thinking").value, "xhigh")
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        # The row starts start_model, so it uses that model's level.
        self.assertEqual(self.callback.call_args.kwargs["thinking"], "xhigh")
        self.assertEqual(load_config(app.config_file).thinking, {"start/one": "xhigh"})

    async def test_the_default_row_saves_the_level_of_the_model_that_runs(self):
        self.store.config.start_agent = "pi"
        self.store.config.start_model = "start/one"
        app = self.app()
        with patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3"])):
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                app.screen.query_one("#thinking").value = "max"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.assertEqual(load_config(app.config_file).thinking, {"start/one": "max"})
        self.assertNotIn("openai-codex/gpt-6.1-sol", load_config(app.config_file).thinking)

    async def test_the_cursor_stops_steering_the_level_once_the_user_picks_one(self):
        self.store.config.thinking = {"zai/glm-5.3": "xhigh", "openai/gpt-5": "minimal"}
        app = self.app()
        with patch("herdboard.board.pi_model_choices", return_value=(None, ["zai/glm-5.3", "openai/gpt-5"])):
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                thinking = app.screen.query_one("#thinking")
                await pilot.press("down")
                await pilot.pause()
                self.assertEqual(thinking.value, "xhigh")
                thinking.value = "max"
                await pilot.pause()
                # A later cursor move keeps the level that the user chose.
                await pilot.press("down")
                await pilot.pause()
                self.assertEqual(thinking.value, "max")
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.callback.assert_called_with(self.card.ref, "pi", model="openai/gpt-5", thinking="max")

    async def test_a_save_applies_on_the_ui_thread(self):
        app = self.app()
        seen = []
        app.call_from_thread = lambda function, *args, **kwargs: seen.append((function, args, kwargs))
        app.remember_thinking("pi", "zai/glm-5.3", "high")
        # The file save runs here; the change in memory waits for the UI thread.
        self.assertEqual(load_config(app.config_file).thinking, {"zai/glm-5.3": "high"})
        self.assertEqual(app.store.config.thinking, {})
        self.assertEqual(len(seen), 1)
        function, args, _ = seen[0]
        function(*args)
        self.assertEqual(app.store.config.thinking, {"zai/glm-5.3": "high"})

    async def test_the_remembered_level_of_pis_own_default_model(self):
        # No start_model, so the unnamed row is pi's own default model, which pi names in its settings.
        settings = self.root / "home" / ".pi" / "agent"
        settings.mkdir(parents=True)
        (settings / "settings.json").write_text(
            json.dumps(
                {
                    "defaultProvider": "openai-codex",
                    "defaultModel": "gpt-6.1-sol",
                    "defaultThinkingLevel": "high",
                    "enabledModels": ["zai/glm-5.3"],
                }
            )
        )
        self.store.config.thinking = {"openai-codex/gpt-6.1-sol": "max"}
        app = self.app()
        with patch.dict(os.environ, {"HOME": str(self.root / "home")}):
            async with app.run_test() as pilot:
                await pilot.press("S")
                await pilot.pause()
                await pilot.press("down", "enter")
                await pilot.pause()
                screen = app.screen
                self.assertIsInstance(screen, ModelScreen)
                self.assertEqual(screen.query_one("#thinking").value, "max")
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.callback.assert_called_with(self.card.ref, "pi", model=None, thinking="max")

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
        self.agent_review.assert_called_once_with(
            self.card.ref, reviewer="claude", model="opus", thinking=None, then_hunk=False
        )
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
                await pilot.press("down")
                await pilot.pause()
                app.screen.query_one("#thinking").value = "high"
                await pilot.press("enter")
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.agent_review.assert_called_once_with(
            self.card.ref, reviewer="pi", model="zai/glm-5.3", thinking="high", then_hunk=True
        )

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
        self.assertIn((f"claude (opus) plans {self.card.ref} in a tab in its worktree", None), notices)

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
        start._write(
            start.state_dir() / "starts.json",
            {self.card.ref: {"worktree_path": str(path), "agent_review": {"kind": "pi", "pane_id": "w1:p3"}}},
        )
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
        start._write(
            start.state_dir() / "starts.json",
            {
                self.card.ref: {
                    "worktree_path": str(path),
                    "pr_url": "https://github.com/acme/api/pull/7",
                    "pr_state": "open",
                    "pr_checked": time.time(),
                    **extra,
                }
            },
        )

    async def test_m_on_an_open_pr_asks_before_it_rewrites_the_description(self):
        self.open_pr()
        self.merge.return_value = {
            "ref": self.card.ref,
            "mode": "pr",
            "base": "main",
            "pr_url": "https://github.com/acme/api/pull/7",
            "pr_action": "writing",
            "writer": "the card's agent",
            "folder": "/f",
        }
        notices = await self.run_keys("m", "y", "y")
        self.merge.assert_called_once_with(self.card.ref, rewrite=True)
        self.assertIn((f"The card's agent writes the pull request description of {self.card.ref}", None), notices)

    async def test_m_on_an_open_pr_can_only_push(self):
        self.open_pr()
        self.merge.return_value = {
            "ref": self.card.ref,
            "mode": "pr",
            "base": "main",
            "pr_url": "https://github.com/acme/api/pull/7",
            "pr_action": "pushed",
        }
        notices = await self.run_keys("m", "y", "n")
        self.merge.assert_called_once_with(self.card.ref, rewrite=False)
        self.assertIn((f"Pushed {self.card.ref} to its pull request https://github.com/acme/api/pull/7", None), notices)

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
        start._write(
            start.state_dir() / "starts.json",
            {
                self.card.ref: {
                    "worktree_path": str(path),
                    "pr_url": "https://github.com/acme/api/pull/7",
                    "pr_state": "open",
                }
            },
        )
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
        from support.herdboard_start import FakeHerdr

        repo = self.store.config.todos_dir.parent / "repo"
        repo.mkdir()
        for args in (
            ["init", "-q"],
            ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "init"],
        ):
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
        self.assertEqual(
            start._read(start.state_dir() / "starts.json")[card.ref]["repository"], repo_identity(repo).identity
        )

    async def test_real_starts_of_two_cards_run_at_the_same_time(self):
        from herdboard import start
        from herdboard.identity import repo_identity
        from support.herdboard_start import FakeHerdr

        repo = self.store.config.todos_dir.parent / "repo"
        repo.mkdir()
        for args in (
            ["init", "-q"],
            ["-c", "user.name=Test", "-c", "user.email=test@example.com", "commit", "--allow-empty", "-qm", "init"],
        ):
            subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
        board = self.store.init_board(repo_identity(repo))
        first = self.store.add(board, "First start")
        second = self.store.add(board, "Second start")
        start.register_checkout(repo)
        fake = FakeHerdr(repo.parent)
        # Each start waits at its worktree until the other start reaches its own: they must overlap.
        both = threading.Barrier(2, timeout=10)
        lock = threading.Lock()

        def runner(argv, timeout=120):
            if argv[1:3] == ["worktree", "create"]:
                both.wait()
            with lock:
                return fake(argv, timeout)

        app = HerdboardApp(self.store, "repo", lambda _: [board], directory=repo, repo_board=board, reload_interval=60)
        app.animation_level = "none"
        notices = []
        app.notify = lambda message, **kwargs: notices.append((message, kwargs.get("severity")))
        with patch.object(start, "command", runner):
            async with app.run_test() as pilot:
                await pilot.pause()
                app._start_card(first.ref, None)
                app._start_card(second.ref, None)
                await app.workers.wait_for_complete()
                await pilot.pause()
        self.assertEqual([n for n in notices if n[1] in ("error", "warning")], [])
        self.assertEqual(self.store.show(first.ref).column, "In progress")
        self.assertEqual(self.store.show(second.ref).column, "In progress")
        records = start._read(start.state_dir() / "starts.json")
        self.assertIn(first.ref, records)
        self.assertIn(second.ref, records)

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


class TargetBoardTests(unittest.IsolatedAsyncioTestCase):
    """A card without a repository asks where it runs: a repository, a new one, or none."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Config(todos_dir=root / "todos", projects_dir=root / "Projects"))
        self.path = self.store.ensure_inbox()
        self.card = self.store.add(self.path, "Research corvid memory")
        self.other = self.store.init_board(RepoIdentity("github.com/acme/web", "github.com", ("acme", "web")))
        self.started = {
            "ref": self.card.ref,
            "workspace_id": "w1",
            "pane_id": "w1:p1",
            "worktree_path": "/tmp/work",
            "branch": None,
            "base": None,
            "agent_name": None,
            "steps_done": [],
            "mode": "repoless",
        }
        self.callback = Mock(side_effect=[StartError("Choose a repository.", "repo_required"), self.started])

    def app(self, **kwargs):
        app = HerdboardApp(
            self.store, "inbox", lambda _: [self.path], reload_interval=60, start_card=self.callback, **kwargs
        )
        app.animation_level = "none"
        return app

    async def test_s_on_an_inbox_card_asks_where_it_runs(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIsInstance(app.screen, TargetScreen)
            self.assertEqual([path for _, path in app.screen.options], [self.other])
            self.assertIn("New repository", app.screen.labels)
            self.assertIn("No repository: a plain folder, output as attachments", app.screen.labels)
            await pilot.press("escape")
            await pilot.pause()
        self.assertEqual(self.callback.call_count, 1)

    async def test_a_repository_board_choice_starts_there(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
        self.assertEqual(self.callback.call_args_list[1].kwargs, {"repo": self.other})

    async def test_no_repository_choice_starts_repoless(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            # The repository board, then the new repository, then no repository.
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
        self.assertEqual(self.callback.call_args_list[1].kwargs, {"no_repo": True})

    async def test_new_repository_choice_asks_for_the_path(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.press("s")
            await app.workers.wait_for_complete()
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()
            field = app.screen.query_one("#target-path", Input)
            self.assertEqual(field.value, str(Path(self.temp.name) / "Projects" / "research-corvid-memory"))
            field.value = str(Path(self.temp.name) / "scratch" / "corvid")
            await pilot.press("enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
        self.assertEqual(
            self.callback.call_args_list[1].kwargs, {"new_repo": Path(self.temp.name) / "scratch" / "corvid"}
        )

    async def test_the_new_repository_choice_is_hidden_for_a_linear_card(self):
        screen = TargetScreen("ENG-5", [("inbox", self.other)], None, linear=True)
        self.assertNotIn("New repository", screen.labels)
        self.assertEqual(screen.labels[-1], "No repository: a plain folder, output as attachments")
        self.assertEqual([kind for kind, _ in screen.choices], ["repo", "repoless"])

    async def test_a_plan_without_a_repository_asks_where_it_runs(self):
        planned = {
            "ref": self.card.ref,
            "agent": "claude",
            "model": None,
            "pane_id": "w1:p7",
            "checkout": None,
            "plan": "/plan.md",
            "workspace_id": "x1",
            "worktree_path": "/todos/.herdboard/work/t000",
            "branch": None,
            "base": None,
            "mode": "repoless",
        }
        plan = Mock(side_effect=[StartError("Choose a repository.", "repo_required"), planned])
        app = self.app(plan_card=plan)
        notices = []
        app.notify = lambda message, **kwargs: notices.append((message, kwargs.get("severity")))
        async with app.run_test() as pilot:
            await pilot.press("P")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
            self.assertIsInstance(app.screen, TargetScreen)
            await pilot.press("down", "down", "enter")
            await pilot.pause()
            await app.workers.wait_for_complete()
            await pilot.pause()
        self.assertEqual(
            plan.call_args_list[1].kwargs, {"agent": "claude", "model": None, "thinking": None, "no_repo": True}
        )
        self.assertIn((f"claude plans {self.card.ref} in its work folder", None), notices)

    async def test_a_new_repo_plan_says_it_plans_in_the_card_folder(self):
        planned = {
            "ref": self.card.ref,
            "agent": "claude",
            "model": None,
            "pane_id": "w1:p7",
            "checkout": None,
            "plan": "/plan.md",
            "workspace_id": "x1",
            "worktree_path": "/todos/.herdboard/work/t000",
            "branch": None,
            "base": None,
            "mode": "new_repo",
        }
        app = self.app(plan_card=Mock(return_value=planned))
        notices = []
        app.notify = lambda message, **kwargs: notices.append((message, kwargs.get("severity")))
        async with app.run_test() as pilot:
            await pilot.press("P")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")
            await app.workers.wait_for_complete()
            await pilot.pause()
        self.assertIn((f"claude plans {self.card.ref} in its work folder", None), notices)

    async def test_r_on_a_repoless_card_goes_to_the_agent_choice(self):
        from herdboard import start

        folder = start.work_folder(self.store.config, self.card.ref)
        folder.mkdir(parents=True)
        start._write(
            start.state_dir() / "starts.json",
            {
                self.card.ref: {
                    "ref": self.card.ref,
                    "mode": "repoless",
                    "workspace_id": "x1",
                    "pane_id": "x1:p1",
                    "worktree_path": str(folder),
                }
            },
        )
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            await pilot.press("R")
            await pilot.pause()
            self.assertIsInstance(app.screen, AgentScreen)
            self.assertEqual(str(app.screen.title_text), "Choose the reviewer")


class ParallelBoardTests(unittest.IsolatedAsyncioTestCase):
    """An action on one card blocks only that card and its parent. Other cards stay free."""

    async def asyncSetUp(self):
        asyncio.get_running_loop().set_debug(False)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        env = patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Config(todos_dir=root / "todos", projects_dir=root / "Projects"))
        self.path = self.store.ensure_inbox()
        self.a = self.store.add(self.path, "Card A")
        self.b = self.store.add(self.path, "Card B")
        self.child = self.store.add(self.path, "Child of A", parent=self.a.ref)
        # Each start blocks until the test releases its card.
        self.release = {ref: threading.Event() for ref in (self.a.ref, self.b.ref, self.child.ref)}
        self.failures = {}
        self.notices = []

    def start(self, ref, agent, **options):
        if not self.release[ref].wait(10):
            raise AssertionError(f"{ref} was not released")
        if ref in self.failures:
            raise self.failures[ref]
        return {
            "ref": ref,
            "workspace_id": f"w-{ref}",
            "pane_id": "p",
            "worktree_path": f"/tmp/{ref}",
            "branch": ref,
            "agent_name": None,
            "steps_done": [],
        }

    def app(self, **kwargs):
        app = HerdboardApp(
            self.store, "inbox", lambda _: [self.path], reload_interval=60, start_card=self.start, **kwargs
        )
        app.animation_level = "none"
        app.notify = lambda message, **kw: self.notices.append(message)
        return app

    def select(self, app, ref):
        card = next(card for column in app.columns for card in column.cards if card.ref == ref)
        self.assertTrue(app.select_card(card))

    async def until(self, pilot, check):
        for _ in range(200):
            await pilot.pause()
            if check():
                return
            await asyncio.sleep(0.01)
        self.fail("condition not met")

    def column(self, ref):
        return self.store.show(ref).column

    def card_text(self, app, ref):
        return next(widget.plain for widget in app.query(".card") if widget.card.ref == ref)

    async def finish(self, pilot):
        for event in self.release.values():
            event.set()
        await pilot.app.workers.wait_for_complete()
        await pilot.pause()

    async def test_a_start_does_not_block_a_move_of_another_card(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._start_card(self.a.ref, None)
            self.select(app, self.b.ref)
            app.action_move(1)
            await self.until(pilot, lambda: self.column(self.b.ref) != "Backlog")
            self.assertFalse([n for n in self.notices if n.startswith("Busy")])
            await self.finish(pilot)

    async def test_a_start_blocks_a_move_of_the_same_card(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._start_card(self.a.ref, None)
            self.select(app, self.a.ref)
            app.action_move(1)
            self.assertIn(f"Busy: {self.a.ref} is starting", self.notices)
            await self.finish(pilot)
        self.assertEqual(self.column(self.a.ref), "Backlog")

    async def test_a_start_of_a_sub_card_blocks_its_parent(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._start_card(self.child.ref, None)
            self.select(app, self.a.ref)
            app.action_move(1)
            self.assertIn(f"Busy: {self.a.ref} is starting", self.notices)
            await self.finish(pilot)
        self.assertEqual(self.column(self.a.ref), "Backlog")

    async def test_two_starts_of_different_cards_run_together(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._start_card(self.a.ref, None)
            app._start_card(self.b.ref, None)
            self.release[self.b.ref].set()
            await self.until(pilot, lambda: any(f"w-{self.b.ref}" in n for n in self.notices))
            self.release[self.a.ref].set()
            await self.until(pilot, lambda: any(f"w-{self.a.ref}" in n for n in self.notices))
            await self.finish(pilot)
            self.assertFalse(app.starting)
        self.assertFalse([n for n in self.notices if n.startswith("Busy")])

    async def test_a_start_does_not_block_an_add(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._start_card(self.a.ref, None)
            app._start_write(lambda: self.store.add(self.path, "Added meanwhile"))
            await self.until(pilot, lambda: "Added meanwhile" in [c.title for c in self.store.list_cards([self.path])])
            self.assertFalse([n for n in self.notices if n.startswith("Busy")])
            await self.finish(pilot)

    async def test_the_target_question_uses_the_request_of_the_failed_plan(self):
        def plan(ref, **options):
            raise StartError("Choose a repository.", "repo_required")

        app = self.app(plan_card=plan)
        async with app.run_test() as pilot:
            await pilot.pause()
            app._start_card(self.b.ref, None)
            app._plan(self.a.ref, "claude", None, None)
            await self.until(pilot, lambda: isinstance(app.screen, TargetScreen))
            self.assertEqual(app.screen.ref, self.a.ref)
            await pilot.press("escape")
            await self.finish(pilot)

    async def test_a_busy_card_shows_its_action(self):
        app = self.app()
        async with app.run_test() as pilot:
            await pilot.pause()
            app._start_card(self.a.ref, None)
            await self.until(pilot, lambda: "busy: starting" in self.card_text(app, self.a.ref))
            self.assertNotIn("busy", self.card_text(app, self.b.ref))
            await self.finish(pilot)
            self.assertNotIn("busy", self.card_text(app, self.a.ref))
