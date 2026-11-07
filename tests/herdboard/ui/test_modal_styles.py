from dataclasses import asdict, replace
from unittest.mock import Mock, patch

from rich.cells import cell_len
from textual.containers import VerticalScroll
from textual.widgets import Input, Label, OptionList, Select

from herdboard import factory
from herdboard.automations import Automation
from herdboard.board import (
    AddScreen,
    AgentScreen,
    AttachmentChoiceScreen,
    AttachmentScreen,
    AutomationDetailScreen,
    AutomationFormScreen,
    BoardChoiceScreen,
    CardFormScreen,
    CardMenuScreen,
    ClaudeModelScreen,
    ConfigScreen,
    ConfirmScreen,
    DeleteScreen,
    DetailScreen,
    EditScreen,
    FactoryCustomScreen,
    FactoryPresetScreen,
    HelpScreen,
    IssueSearchScreen,
    ModalBase,
    ModelScreen,
    NoteChangedScreen,
    ParentScreen,
    PromoteScreen,
    PullRequestDetailScreen,
    ReviewKindScreen,
    StateScreen,
    TargetScreen,
)
from herdboard.pr_reviews import PullRequest
from support.herdboard_board import BoardTestCase


class ModalStyleTests(BoardTestCase):
    async def test_modals_keep_shortcuts_in_the_border(self):
        card = self.store.add(self.repo_path, "A card", "Todo")
        screens = [
            (AgentScreen(), "enter", "esc"),
            (ConfirmScreen("Continue?", "Keep this explanation."), "y", "esc"),
            (HelpScreen(), "esc", "esc"),
            (AttachmentScreen("notes.txt", "A long attachment\n" * 100), "j/k", "esc"),
            (DetailScreen(card), "e", "esc"),
            (EditScreen(card), "ctrl+s", "esc"),
            (
                PullRequestDetailScreen(
                    PullRequest.from_json({"number": 1, "title": "A pull request"}, ""), "acme/api"
                ),
                "R: review",
                "esc: close",
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
                    self.assertFalse(any("esc:" in str(label.render()) for label in dialog.query(Label)))
                    self.assertEqual(dialog.styles.border_bottom[0], "round")
                    if isinstance(screen, AttachmentScreen):
                        dialog.scroll_end(animate=False)
                        await pilot.pause()
                        self.assertGreater(dialog.scroll_y, 0)
                        self.assertIn("esc:", dialog.border_subtitle)
                    if isinstance(screen, ConfirmScreen):
                        self.assertIn("Keep this explanation.", str(screen.query_one("Label.dim").render()))
                    if isinstance(screen, EditScreen):
                        self.assertIsInstance(app.focused, Input)
                    app.pop_screen()
                    await pilot.pause()

    def modal_examples(self, app):
        card = self.store.add(self.repo_path, "A card", "Todo")
        options = [("Repository", self.repo_path)]
        file = {"name": "notes.txt", "path": str(self.root / "notes.txt"), "size": 12}
        entry = {
            **asdict(Automation("a123", "An automation", "every day")),
            "attachments": [],
            "next_due": None,
            "schedule_error": None,
            "last_run": None,
            "last_card": None,
            "last_result": None,
        }
        config = ConfigScreen(
            self.repo_path,
            self.store.board_meta(self.repo_path),
            False,
            {key: getattr(self.config, key) for key in ConfigScreen.GLOBAL_KEYS},
            sorted(app.available_themes),
            (None, []),
        )
        return [
            AddScreen(options, self.repo_path, "Todo"),
            EditScreen(card),
            DetailScreen(card),
            DetailScreen(replace(card, body=["- [ ] Check"], pr_url="https://example.com/pr"), files=[file]),
            AttachmentChoiceScreen([file]),
            AttachmentScreen("notes.txt", "Contents"),
            HelpScreen(),
            StateScreen("ENG", "Todo", [{"id": "todo", "name": "Todo"}]),
            AgentScreen(),
            FactoryPresetScreen(card.ref, self.config),
            FactoryCustomScreen(card.ref, self.config, factory.default_run(self.config)),
            ReviewKindScreen(),
            ClaudeModelScreen(),
            TargetScreen(card.ref, options),
            TargetScreen(card.ref, options, default_path=self.root / "repo"),
            TargetScreen(card.ref, options, linear=True),
            BoardChoiceScreen(card, options),
            BoardChoiceScreen(None, options, prompt="Choose a board"),
            IssueSearchScreen(Mock(return_value=[]), "Repository"),
            PromoteScreen(card.ref, card.title, Mock(return_value=[]), Mock(return_value=[])),
            ParentScreen(card, []),
            CardMenuScreen(card.title, [("Edit", "edit")]),
            DeleteScreen(card),
            ConfirmScreen("Continue?", "Keep this explanation."),
            ModelScreen(None, ["a-model"]),
            ModelScreen(None, ["a-model"], agent="codex"),
            config,
            PullRequestDetailScreen(PullRequest.from_json({"number": 1, "title": "A pull request"}, ""), "acme/api"),
            NoteChangedScreen(self.root / "notes.md"),
            AutomationFormScreen("Add an automation", ["inbox"], [], Mock(), self.config),
            AutomationDetailScreen(entry),
            AutomationDetailScreen({**entry, "attachments": [file]}),
        ]

    async def test_every_modal_has_consistent_border_shortcuts(self):
        app = self.make_app()
        with (
            patch("herdboard.board.pi_model_choices", return_value=(None, [])),
            patch("herdboard.board.codex_model_choices", return_value=(None, [])),
        ):
            screens = self.modal_examples(app)
            expected = (set(ModalBase.__subclasses__()) - {CardFormScreen}) | set(CardFormScreen.__subclasses__())
            self.assertEqual({type(screen) for screen in screens}, expected)
            async with app.run_test(size=(120, 40)) as pilot:
                for screen in screens:
                    with self.subTest(screen=type(screen).__name__):
                        app.push_screen(screen)
                        await pilot.pause()
                        await app.workers.wait_for_complete()
                        await pilot.pause()
                        subtitle = screen.query_one(".dialog").border_subtitle
                        self.assertTrue(subtitle)
                        for shortcut in subtitle.split(" · "):
                            self.assertRegex(shortcut, r"^[^:]+: [^:]+$")
                        self.assertRegex(subtitle, r"(?:^| · )(?:n or )?esc: .+$")
                        self.assertNotIn("escape:", subtitle)
                        for label in screen.query(".dialog Label"):
                            text = str(label.render()).lower()
                            self.assertNotRegex(text, r"(?:esc|escape|ctrl\+s|tab|enter):")
                            self.assertNotIn("enter in text fields:", text)
                        if isinstance(screen, BoardChoiceScreen):
                            action = "choose" if screen.prompt else "move"
                            self.assertEqual(subtitle, f"enter: {action} · esc: cancel")
                        if isinstance(screen, AutomationDetailScreen):
                            self.assertEqual("o: open" in subtitle, bool(screen.files))
                        app.pop_screen()
                        await pilot.pause()

    async def test_card_and_pull_request_shortcuts_fit_at_80_columns(self):
        card = self.store.add(self.repo_path, "A card", "Todo", body=["- [ ] Check"])
        card = replace(card, pr_url="https://example.com/pr")
        screens = [
            DetailScreen(card),
            PullRequestDetailScreen(PullRequest.from_json({"number": 1, "title": "A pull request"}, ""), "acme/api"),
        ]
        app = self.make_app()
        async with app.run_test(size=(80, 24)) as pilot:
            for screen in screens:
                with self.subTest(screen=type(screen).__name__):
                    app.push_screen(screen)
                    await pilot.pause()
                    dialog = screen.query_one(".dialog")
                    self.assertLessEqual(cell_len(dialog.border_subtitle), dialog.size.width - 4)
                    rendered = "\n".join(strip.text for strip in screen._compositor.render_strips())
                    self.assertIn("esc: close", rendered)
                    app.pop_screen()
                    await pilot.pause()

    async def test_selection_menus_keep_their_native_border(self):
        app = self.make_app()
        card = self.store.add(self.repo_path, "A card", "Todo")
        async with app.run_test(size=(100, 40)) as pilot:
            for theme in ("textual-dark", "ansi-dark", "ansi-light"):
                app.theme = theme
                for selector in ("#card-type", "#config-theme"):
                    with self.subTest(theme=theme, selector=selector):
                        if selector == "#card-type":
                            screen = EditScreen(card, types=["bug", "chore"])
                        else:
                            screen = ConfigScreen(
                                self.repo_path,
                                self.store.board_meta(self.repo_path),
                                False,
                                {key: getattr(self.config, key) for key in ConfigScreen.GLOBAL_KEYS},
                                sorted(app.available_themes),
                                (None, []),
                            )
                        app.push_screen(screen)
                        await pilot.pause()
                        select = screen.query_one(selector, Select)
                        select.focus()
                        await pilot.press("enter")
                        await pilot.pause()
                        overlay = select.query_one("SelectOverlay")
                        self.assertTrue(overlay.display)
                        self.assertTrue(overlay.has_focus)
                        self.assertEqual(overlay.styles.border_top[0], "tall")
                        self.assertEqual(select.styles.border_top[0], "round")
                        self.assertTrue(screen.region.contains_region(overlay.region))
                        await pilot.press("escape")
                        self.assertIs(app.screen, screen)
                        app.pop_screen()
                        await pilot.pause()

    async def test_repository_without_default_path_accepts_a_typed_path(self):
        app = self.make_app()
        chosen = Mock()
        screen = TargetScreen("t123", [])
        async with app.run_test() as pilot:
            app.push_screen(screen, chosen)
            await pilot.pause()
            path = screen.query_one("#target-path", Input)
            self.assertEqual(path.value, "")
            await pilot.press("enter")
            self.assertIs(app.focused, path)
            path.value = str(self.root / "project")
            await pilot.press("enter")
            await pilot.pause()
            chosen.assert_called_once_with(("new_repo", self.root / "project"))

    async def test_repository_without_default_path_requires_input(self):
        app = self.make_app()
        chosen = Mock()
        screen = TargetScreen("t123", [])
        async with app.run_test() as pilot:
            app.push_screen(screen, chosen)
            await pilot.pause()
            path = screen.query_one("#target-path", Input)
            path.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIs(app.screen, screen)
            chosen.assert_not_called()
            self.assertIn("Enter a repository path", [notice.message for notice in app._notifications])

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
            self.assertEqual(dialog.border_subtitle, "ctrl+s: save · tab: next field · esc: cancel")
            self.assertLessEqual(dialog.region.right, 80)
            self.assertLessEqual(dialog.region.bottom, 24)
            dialog.scroll_end(animate=False)
            await pilot.pause()
            self.assertGreater(dialog.scroll_y, 0)
            self.assertIn("Schedules:", str(list(form.query(Label))[-1].render()))
            self.assertIn("esc: cancel", dialog.border_subtitle)

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
