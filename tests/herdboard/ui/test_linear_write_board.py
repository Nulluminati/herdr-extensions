import unittest


from support import linear_data
from herdboard.board import StateScreen
from textual.widgets import OptionList
from support.herdboard_board import SIZE, settle
from support.herdboard_linear_source import DEAD
from support.herdboard_linear_write_board import LinearWriteBoardCase

OFFLINE = "Linear cards are read only offline"


class WriteKeyTests(LinearWriteBoardCase):
    async def test_L_moves_a_linear_card_and_refreshes_linear(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            fetches = len(self.requests("ProjectIssues"))
            await pilot.press("L")
            await settle(pilot)
            await settle(pilot)
            self.assertEqual(self.issue("ENG-1")["state"]["name"], "Doing")
            self.assertEqual(len(self.requests("IssueUpdate")), 1)
            self.assertEqual(self.requests("IssueUpdate")[0]["variables"]["input"],
                             {"stateId": "ws-ENG-Doing"})
            self.assertGreater(len(self.requests("ProjectIssues")), fetches)
            self.assertEqual(self.column_of(app, "ENG-1"), "In progress")
            self.assertEqual(app.current_card().ref, "ENG-1")
            self.assertFalse(app.writing)

    async def test_H_moves_a_linear_card_to_the_previous_column(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("H")
            await settle(pilot)
            await settle(pilot)
            self.assertEqual(self.issue("ENG-1")["state"]["name"], "Backlog")
            self.assertEqual(self.column_of(app, "ENG-1"), "Backlog")

    async def test_d_moves_a_linear_card_to_done(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-2")
            await pilot.press("d")
            await settle(pilot)
            await settle(pilot)
            self.assertEqual(self.issue("ENG-2")["state"]["name"], "Done")
            self.assertEqual(self.column_of(app, "ENG-2"), "Done")

    async def test_a_move_past_the_last_column_does_nothing(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-2")
            await pilot.press("d")
            await settle(pilot)
            await settle(pilot)
            self.select(app, "ENG-2")
            requests = len(self.requests("IssueUpdate"))
            await pilot.press("L")
            await settle(pilot)
            self.assertEqual(len(self.requests("IssueUpdate")), requests)

    async def test_todos_are_not_sent_to_linear(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, [c.ref for c in app.columns[1].cards if c.source == "todo"][0])
            await pilot.press("L")
            await settle(pilot)
            self.assertEqual(self.requests("IssueUpdate"), [])
            self.assertEqual(self.requests("IssueForWrite"), [])

    async def test_a_failed_write_is_a_notification(self):
        self.fake.mutation_failure = "http_429"
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("L")
            await settle(pilot)
            errors = [m for m, s in self.notifications(app) if s == "error"]
            self.assertTrue(any("rate limited" in m for m in errors), errors)
            self.assertEqual(self.column_of(app, "ENG-1"), "Todo")
            self.assertEqual(len(self.requests("IssueUpdate")), 1)
            self.assertFalse(app.writing)

    async def test_a_move_to_a_column_without_a_state_is_a_notification(self):
        team = self.fixtures["teams"][0]
        team["states"] = [s for s in team["states"] if s["name"] != "Doing"]
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("L")
            await settle(pilot)
            errors = [m for m, s in self.notifications(app) if s == "error"]
            self.assertTrue(any("ENG" in m and "In progress" in m for m in errors), errors)

    async def test_without_a_writer_the_keys_say_so(self):
        app = self.make_linear_app()
        app.linear_writer = None
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("L")
            await settle(pilot)
            self.assertIn(("Linear writes are not available", "warning"), self.notifications(app))
            self.assertEqual(self.requests("IssueUpdate"), [])


class OfflineTests(LinearWriteBoardCase):
    async def test_stale_linear_cards_are_read_only(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
        self.endpoint = DEAD
        before = self.repo_path.read_text()
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.assertRegex(self.title(app), r"Linear: stale since")
            for key in ("H", "L", "d"):
                self.select(app, "ENG-1")
                app._notifications.clear()
                await pilot.press(key)
                await settle(pilot)
                self.assertIn((OFFLINE, "warning"), self.notifications(app))
            self.assertEqual(self.requests("IssueUpdate"), [])
            self.assertEqual(self.requests("IssueForWrite"), [])
            self.assertEqual(self.repo_path.read_text(), before)
            self.assertEqual(self.column_of(app, "ENG-1"), "Todo")
            self.assertFalse(app.writing)


class AmbiguousStateTests(LinearWriteBoardCase):
    def setUp(self):
        super().setUp()
        linear_data.add_state(self.fixtures, "ENG", "Coding", "started")

    async def test_dialog_lists_the_candidates_and_the_choice_is_stored(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("L")
            dialog = await self.wait_for_dialog(pilot)
            self.assertIn("ENG", dialog.query_one("#state-prompt").plain)
            self.assertIn("In progress", dialog.query_one("#state-prompt").plain)
            options = dialog.query_one(OptionList)
            self.assertEqual([options.get_option_at_index(i).prompt for i in range(options.option_count)],
                             ["Doing", "Coding"])
            self.assertEqual(self.requests("IssueUpdate"), [])
            options.highlighted = 1
            await pilot.press("enter")
            await settle(pilot)
            await settle(pilot)
            self.assertEqual(self.issue("ENG-1")["state"]["name"], "Coding")
            self.assertEqual(self.column_of(app, "ENG-1"), "In progress")
            # The stored choice answers the next move without a dialog.
            self.select(app, "ENG-2")
            await pilot.press("H")
            await settle(pilot)
            await settle(pilot)
            self.assertEqual(self.issue("ENG-2")["state"]["name"], "Coding")
            self.assertNotIsInstance(app.screen, StateScreen)

    async def test_escape_cancels_the_move(self):
        app = self.make_linear_app()
        async with app.run_test(size=SIZE) as pilot:
            await settle(pilot)
            self.select(app, "ENG-1")
            await pilot.press("L")
            await self.wait_for_dialog(pilot)
            await pilot.press("escape")
            await settle(pilot)
            await settle(pilot)
            self.assertNotIsInstance(app.screen, StateScreen)
            self.assertEqual(self.requests("IssueUpdate"), [])
            self.assertEqual(self.issue("ENG-1")["state"]["name"], "Todo")
            errors = [m for m, s in self.notifications(app) if s == "error"]
            self.assertTrue(any("No state chosen" in m for m in errors), errors)
            self.assertFalse(app.writing)


if __name__ == "__main__":
    unittest.main()
