from unittest.mock import Mock, patch

from textual.containers import VerticalScroll
from textual.widgets import Input, Label, OptionList

from herdboard.board import (
    AgentScreen,
    AttachmentScreen,
    AutomationFormScreen,
    ConfirmScreen,
    DetailScreen,
    EditScreen,
    HelpScreen,
    PullRequestDetailScreen,
)
from herdboard.pr_reviews import PullRequest
from support.herdboard_board import BoardTestCase


class ModalStyleTests(BoardTestCase):
    async def test_modals_keep_shortcuts_in_the_border(self):
        card = self.store.add(self.repo_path, "A card", "Todo")
        screens = [
            (AgentScreen(), "enter", "escape"),
            (ConfirmScreen("Continue?", "Keep this explanation."), "y", "escape"),
            (HelpScreen(), "escape", "escape"),
            (AttachmentScreen("notes.txt", "A long attachment\n" * 100), "j/k", "escape"),
            (DetailScreen(card), "e", "escape"),
            (EditScreen(card), "ctrl+s", "escape"),
            (
                PullRequestDetailScreen(
                    PullRequest.from_json({"number": 1, "title": "A pull request"}, ""), "acme/api"
                ),
                "R review",
                "esc close",
            ),
        ]
        app = self.make_app()
        async with app.run_test(size=(120, 40)) as pilot:
            for screen, shortcut, close in screens:
                with self.subTest(screen=type(screen).__name__):
                    app.push_screen(screen)
                    await pilot.pause()
                    dialog = screen.query_one(".dialog")
                    self.assertIn(shortcut, dialog.border_subtitle)
                    self.assertIn(close, dialog.border_subtitle)
                    self.assertFalse(any("escape:" in str(label.render()) for label in dialog.query(Label)))
                    self.assertEqual(dialog.styles.border_bottom[0], "round")
                    if isinstance(screen, AttachmentScreen):
                        dialog.scroll_end(animate=False)
                        await pilot.pause()
                        self.assertGreater(dialog.scroll_y, 0)
                        self.assertIn("escape", dialog.border_subtitle)
                    if isinstance(screen, ConfirmScreen):
                        self.assertIn("Keep this explanation.", str(screen.query_one("Label.dim").render()))
                    if isinstance(screen, EditScreen):
                        self.assertIsInstance(app.focused, Input)
                    app.pop_screen()
                    await pilot.pause()

    async def test_choice_controls_use_the_detail_section_border(self):
        app = self.make_app()
        async with app.run_test() as pilot:
            app.theme = "textual-dark"
            app.push_screen(AgentScreen())
            await pilot.pause()
            options = app.screen.query_one(OptionList)
            self.assertEqual(options.styles.border_top[0], "round")
            self.assertTrue(app.screen.query_one("Label").styles.text_style.bold)

    async def test_automation_shortcuts_stay_outside_the_scrolling_form(self):
        with (
            patch("herdboard.board.pi_model_choices", return_value=(None, [])),
            patch("herdboard.board.codex_model_choices", return_value=(None, [])),
        ):
            form = AutomationFormScreen("Add an automation", ["inbox"], [], Mock(), self.config)
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            app.push_screen(form)
            await pilot.pause()
            dialog = form.query_one(".dialog", VerticalScroll)
            self.assertEqual(dialog.border_subtitle, "ctrl+s: save · tab: next field · escape: cancel")
            self.assertLessEqual(dialog.region.right, 80)
            self.assertLessEqual(dialog.region.bottom, 24)
            dialog.scroll_end(animate=False)
            await pilot.pause()
            self.assertGreater(dialog.scroll_y, 0)
            self.assertIn("Schedules:", str(list(form.query(Label))[-1].render()))
            self.assertIn("escape: cancel", dialog.border_subtitle)

    async def test_ansi_controls_keep_the_focus_border_visible(self):
        app = self.make_app()
        async with app.run_test() as pilot:
            app.theme = "ansi-light"
            app.push_screen(AgentScreen())
            await pilot.pause()
            options = app.screen.query_one(OptionList)
            focused = options.styles.border_top
            self.assertEqual(focused[0], "round")
            options.blur()
            await pilot.pause()
            self.assertEqual(options.styles.border_top[0], "round")
            self.assertNotEqual(options.styles.border_top[1], focused[1])

    async def test_detail_modals_use_the_pull_request_width(self):
        app = self.make_app()
        async with app.run_test(size=(140, 50)) as pilot:
            app.push_screen(AttachmentScreen("notes.txt", "Contents"))
            await pilot.pause()
            dialog = app.screen.query_one(".dialog", VerticalScroll)
            self.assertEqual(dialog.region.width, 100)
