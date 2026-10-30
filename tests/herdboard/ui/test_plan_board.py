"""The board shows a planner without moving its card to another column."""
import fcntl
import threading
from unittest.mock import patch

from herdboard import plan, start
from herdboard.board import RELOAD_SECONDS
from herdboard.model import IoError
from support.herdboard_board import SIZE, BoardTestCase


class PlanningBoardTests(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.card = self.store.add(self.repo_path, "Plan login", "Todo")
        self.pane_exists = self.enterContext(patch.object(plan, "_pane_exists", return_value=True))

    def record(self, kind="pi"):
        start._write(start.state_dir() / "plans.json", {
            self.card.ref: {"pane_id": "w1:p7", "kind": kind},
        })

    async def test_a_live_planner_shows_its_kind_without_moving_the_card(self):
        for kind in ("pi", "claude"):
            with self.subTest(kind=kind):
                self.record(kind)
                app = self.make_app()
                async with app.run_test(size=SIZE) as pilot:
                    await pilot.pause()
                    self.assertIn(f"planning: {kind}", self.cards(app)[0])
                    self.assertEqual(self.store.show(self.card.ref).column, "Todo")
                    self.assertEqual(self.headers(app), ["Backlog 0", "Todo 1", "In progress 0", "Review 0", "Done 0"])

    async def test_a_planner_started_elsewhere_appears_on_the_next_check(self):
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertNotIn("planning:", self.cards(app)[0])
            self.record()
            await app.check_files()
            await pilot.pause()
            self.assertIn("planning: pi", self.cards(app)[0])

    async def test_a_closed_pane_clears_the_marker_even_if_the_record_stays(self):
        self.record()
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertIn("planning: pi", self.cards(app)[0])
            self.pane_exists.return_value = False
            await app.check_files()
            await pilot.pause()
            self.assertNotIn("planning:", self.cards(app)[0])

    async def test_a_removed_record_clears_the_marker(self):
        self.record()
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertIn("planning: pi", self.cards(app)[0])
            start._write(start.state_dir() / "plans.json", {})
            await app.check_files()
            await pilot.pause()
            self.assertNotIn("planning:", self.cards(app)[0])

    def test_the_refresh_interval_stays_at_two_seconds(self):
        self.assertEqual(RELOAD_SECONDS, 2.0)

    async def test_bad_planning_json_does_not_hide_the_cards(self):
        self.record()
        plan._plans_path().write_text("{", encoding="utf-8")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertEqual(self.cards(app), [f"Plan login  {self.card.ref}"])
            self.assertFalse(app._notifications)

    async def test_a_planning_read_error_does_not_hide_the_cards(self):
        self.record()
        app = self.make_app()
        with patch.object(plan, "_read", side_effect=IoError("Cannot read planning state")):
            async with app.run_test(size=SIZE) as pilot:
                await pilot.pause()
                self.assertEqual(self.cards(app), [f"Plan login  {self.card.ref}"])
                self.assertFalse(app._notifications)

    async def test_a_busy_start_lock_does_not_hide_the_cards(self):
        self.record()
        with (start.state_dir() / ".starts.lock").open("a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            app = self.make_app()
            async with app.run_test(size=SIZE) as pilot:
                await pilot.pause()
                self.assertIn("planning: pi", self.cards(app)[0])
                self.assertFalse(app._notifications)

    async def test_a_herdr_failure_during_load_keeps_the_cards_and_recorded_marker(self):
        self.record()
        self.pane_exists.side_effect = start.StartError("Herdr is not reachable", "herdr_unavailable")
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertIn("Plan login", self.cards(app)[0])
            self.assertIn("planning: pi", self.cards(app)[0])
            self.assertFalse(app._notifications)
            self.pane_exists.side_effect = None
            self.pane_exists.return_value = False
            await app.check_files()
            await pilot.pause()
            self.assertEqual(self.cards(app), [f"Plan login  {self.card.ref}"])

    async def test_a_broken_state_file_clears_the_marker_without_blocking_other_changes(self):
        self.record()
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertIn("planning: pi", self.cards(app)[0])
            plan._plans_path().write_text("{", encoding="utf-8")
            another = self.store.add(self.repo_path, "Another card", "Todo")
            await app.check_files()
            await pilot.pause()
            self.assertEqual(self.cards(app), [f"Plan login  {self.card.ref}", f"Another card  {another.ref}"])
            self.assertFalse(app._notifications)

    async def test_archive_markers_show_on_load_and_survive_reload(self):
        # An external board edit can archive a card without the CLI's active-planner guard.
        self.store.archive(self.card.ref)
        self.record()
        app = self.make_app()
        app.show_archive = True
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertIn("planning: pi", self.cards(app)[0])
            await app.reload()
            await pilot.pause()
            self.assertIn("planning: pi", self.cards(app)[0])
            self.pane_exists.return_value = False
            await app.check_files()
            await pilot.pause()
            self.assertNotIn("planning:", self.cards(app)[0])

    async def test_a_new_archive_marker_appears_on_the_next_check(self):
        self.store.archive(self.card.ref)
        app = self.make_app()
        app.show_archive = True
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            self.assertNotIn("planning:", self.cards(app)[0])
            self.record()
            await app.check_files()
            await pilot.pause()
            self.assertIn("planning: pi", self.cards(app)[0])

    async def test_pane_checks_run_outside_the_event_loop(self):
        self.record()
        threads = []
        self.pane_exists.side_effect = lambda pane: threads.append(threading.get_ident()) or True
        app = self.make_app()
        async with app.run_test(size=SIZE) as pilot:
            await pilot.pause()
            await app.check_files()
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)


