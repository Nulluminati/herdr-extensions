import unittest
from unittest.mock import Mock


from herdboard.board import BoardChoiceScreen, ConfigScreen, IssueSearchScreen, TargetScreen
from herdboard.identity import RepoIdentity
from herdboard.linear import LinearError
from herdboard.linear_pins import IssuePin
from herdboard.start import StartError
from support.herdboard_board import SIZE, settle
from support.herdboard_linear_board import LinearBoardCase


class PinBoardCase(LinearBoardCase):
    def make_pin_app(self, search=None, **kwargs):
        app = self.make_linear_app(**kwargs)
        app.search_issues = search or self.source_in_use.search
        return app

    def pins(self, path=None):
        return self.store.board_meta(path or self.repo_path).pins

    def card_refs(self, app):
        return [card.ref for column in app.columns for card in column.cards]

    async def search(self, pilot, term):
        screen = pilot.app.screen
        self.assertIsInstance(screen, IssueSearchScreen)
        screen.query_one("#issue-search").value = term
        await pilot.press("enter")
        await settle(pilot)
        return screen


class ImportIssueTests(PinBoardCase):
    async def test_I_searches_on_enter_and_pins_the_chosen_issue(self):
        app = self.make_pin_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertNotIn("ENG-5", self.card_refs(app))
            await pilot.press("I")
            await settle(pilot)
            screen = await self.search(pilot, "issue 5")
            results = screen.query_one("#issue-results")
            self.assertEqual(results.option_count, 1)
            self.assertIn("ENG-5", str(results.get_option_at_index(0).prompt))
            self.assertIn("1 open issue", screen.query_one("#issue-status").plain)
            self.assertIs(app.focused, results)
            await pilot.press("enter")
            await settle(pilot)
            await settle(pilot)
            self.assertNotIsInstance(app.screen, IssueSearchScreen)
            self.assertEqual(self.pins(), [IssuePin("i5", "ENG-5")])
            self.assertIn("ENG-5", self.card_refs(app))
            self.assertIn((f"Pinned ENG-5 to {self.repo_path.stem}", "information"), self.notifications(app))

    async def test_typing_does_not_search(self):
        app = self.make_pin_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("I")
            await settle(pilot)
            await pilot.press("i", "s", "s")
            await settle(pilot)
            self.assertEqual(self.requests("SearchIssues"), [])
            await pilot.press("enter")
            await settle(pilot)
            self.assertEqual(len(self.requests("SearchIssues")), 1)

    async def test_no_result_and_errors_show_in_the_screen(self):
        def search(term):
            if term == "boom":
                raise LinearError("Linear rate limit exceeded")
            return []

        app = self.make_pin_app(search=search)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("I")
            await settle(pilot)
            screen = await self.search(pilot, "nothing")
            self.assertEqual(screen.query_one("#issue-status").plain, "No open issues match.")
            screen = await self.search(pilot, "boom")
            self.assertIn("rate limit", screen.query_one("#issue-status").plain)
            self.assertIs(app.screen, screen)
        self.assertEqual(self.pins(), [])

    async def test_escape_pins_nothing(self):
        app = self.make_pin_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("I")
            await settle(pilot)
            await self.search(pilot, "issue 5")
            await pilot.press("escape")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, IssueSearchScreen)
        self.assertEqual(self.pins(), [])

    async def test_without_a_repository_board_I_asks_for_the_board(self):
        other = self.store.init_board(RepoIdentity("github.com/acme/web", "github.com", ("acme", "web")))
        app = self.make_pin_app(view="inbox")
        app.repo_board = None
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("I")
            await settle(pilot)
            self.assertIsInstance(app.screen, BoardChoiceScreen)
            self.assertEqual([path for _, path in app.screen.options], sorted([self.repo_path, other]))
            app.screen.query_one("#board-choice").highlighted = app.screen.options.index(
                next(option for option in app.screen.options if option[1] == other))
            await pilot.press("enter")
            await settle(pilot)
            await self.search(pilot, "issue 5")
            await pilot.press("enter")
            await settle(pilot)
        self.assertEqual(self.pins(other), [IssuePin("i5", "ENG-5")])
        self.assertEqual(self.pins(), [])

    async def test_linear_off_says_so(self):
        app = self.make_pin_app(active=False)
        app.search_issues = None
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            await pilot.press("I")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, IssueSearchScreen)
            self.assertTrue(any("Linear" in message for message, _ in self.notifications(app)))


class UnpinTests(PinBoardCase):
    async def test_u_removes_the_pin_of_a_pinned_card(self):
        self.store.update_pins(self.repo_path, lambda _: [IssuePin("i5", "ENG-5")])
        app = self.make_pin_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-5")
            await pilot.press("u")
            await settle(pilot)
            await settle(pilot)
            self.assertEqual(self.pins(), [])
            self.assertNotIn("ENG-5", self.card_refs(app))
            self.assertIn(("Unpinned ENG-5", "information"), self.notifications(app))

    async def test_u_finds_the_pin_by_issue_id_after_a_team_move(self):
        # The pin keeps the identifier from before the move; Linear now calls the issue ENG-5.
        self.store.update_pins(self.repo_path, lambda _: [IssuePin("i5", "OPS-5")])
        app = self.make_pin_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-5")
            await pilot.press("u")
            await settle(pilot)
            await settle(pilot)
            self.assertEqual(self.pins(), [])
            self.assertIn(("Unpinned ENG-5", "information"), self.notifications(app))

    async def test_u_on_a_project_card_or_a_todo_says_why(self):
        app = self.make_pin_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("u")
            await settle(pilot)
            self.assertIn("not pinned", self.notifications(app)[-1][0])
            todo = next(card.ref for column in app.columns for card in column.cards if card.source == "todo")
            self.select(app, todo)
            await pilot.press("u")
            await settle(pilot)
            self.assertIn("Linear issue", self.notifications(app)[-1][0])


class StartRepositoryTests(PinBoardCase):
    STARTED = {"ref": "ENG-5", "workspace_id": "w1", "pane_id": "w1:p1", "worktree_path": "/tmp/work",
               "branch": "nate/eng-5", "agent_name": None, "steps_done": []}

    def start_app(self, *errors):
        app = self.make_pin_app(view="mine")
        app.start_card = Mock(side_effect=[*errors, self.STARTED])
        return app

    async def test_a_linear_start_without_a_repository_asks_for_the_board(self):
        other = self.store.init_board(RepoIdentity("github.com/acme/web", "github.com", ("acme", "web")))
        app = self.start_app(StartError("Choose a repository with --repo BOARD_OR_PATH.", "repo_required"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-5")
            await pilot.press("s")
            await settle(pilot)
            self.assertIsInstance(app.screen, TargetScreen)
            self.assertEqual([path for _, path in app.screen.options], sorted([self.repo_path, other]))
            await pilot.press("enter")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, TargetScreen)
        self.assertEqual([c.args for c in app.start_card.call_args_list], [("ENG-5", None), ("ENG-5", None)])
        self.assertEqual(app.start_card.call_args_list[1].kwargs, {"repo": self.repo_path})

    async def test_escape_starts_nothing(self):
        app = self.start_app(StartError("Choose a repository.", "repo_required"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-5")
            await pilot.press("s")
            await settle(pilot)
            await pilot.press("escape")
            await settle(pilot)
        self.assertEqual(app.start_card.call_count, 1)

    async def test_other_start_errors_only_notify(self):
        app = self.start_app(StartError("No checkout", "checkout_unknown"))
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-5")
            await pilot.press("s")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, TargetScreen)
            self.assertIn(("No checkout", "error"), self.notifications(app))

    async def test_a_repository_still_required_after_the_choice_only_notifies(self):
        app = self.start_app(*[StartError("Choose a repository.", "repo_required")] * 2)
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-5")
            await pilot.press("s")
            await settle(pilot)
            await pilot.press("enter")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, TargetScreen)
            self.assertIn(("Choose a repository.", "error"), self.notifications(app))
        self.assertEqual(app.start_card.call_count, 2)


class ConfigPinTests(PinBoardCase):
    async def test_c_lists_pins_and_enter_marks_one_for_removal(self):
        self.store.update_pins(self.repo_path, lambda _: [IssuePin("i5", "ENG-5"), IssuePin("i6", "ENG-6")])
        app = self.make_pin_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await settle(pilot)
            await pilot.press("c")
            await settle(pilot)
            screen = app.screen
            self.assertIsInstance(screen, ConfigScreen)
            pins = screen.query_one("#config-pins")
            self.assertEqual(pins.option_count, 2)
            self.assertIn("ENG-5", str(pins.get_option_at_index(0).prompt))
            pins.focus()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("remove", str(pins.get_option_at_index(0).prompt))
            await pilot.press("ctrl+s")
            await settle(pilot)
            self.assertNotIsInstance(app.screen, ConfigScreen)
        self.assertEqual(self.pins(), [IssuePin("i6", "ENG-6")])
        self.assertEqual([link.id for link in self.store.board_meta(self.repo_path).links], ["p-plat"])


if __name__ == "__main__":
    unittest.main()
