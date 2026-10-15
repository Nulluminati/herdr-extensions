"""The board shows a planner without moving its card to another column."""
import sys
import threading
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import cli, plan, start  # noqa: E402
from herdboard.board import card_text  # noqa: E402
from test_herdboard_board import BoardTestCase, SIZE  # noqa: E402


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


class PlanningStatusTests(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.card = self.store.add(self.repo_path, "Plan login", "Todo")
        self.pane_exists = self.enterContext(patch.object(plan, "_pane_exists", return_value=True))

    def test_no_record_means_no_marker_or_herdr_call(self):
        plan.mark_planning([self.card])
        self.assertIsNone(self.card.planning)
        self.assertNotIn("planning:", card_text(self.card, False).plain)
        self.pane_exists.assert_not_called()

    def test_stale_and_malformed_records_do_not_show_a_marker(self):
        for record in (None, [], {}, {"kind": "pi"}, {"kind": "pi", "pane_id": 7},
                       {"kind": "unknown", "pane_id": "w1:p7"}):
            with self.subTest(record=record):
                start._write(start.state_dir() / "plans.json", {self.card.ref: record})
                plan.mark_planning([self.card])
                self.assertIsNone(self.card.planning)
        self.pane_exists.assert_not_called()
        start._write(start.state_dir() / "plans.json", {self.card.ref: {"kind": "pi", "pane_id": "w1:p7"}})
        self.pane_exists.return_value = False
        plan.mark_planning([self.card])
        self.assertIsNone(self.card.planning)

    def test_only_visible_cards_need_pane_checks(self):
        start._write(start.state_dir() / "plans.json", {"other": {"kind": "pi", "pane_id": "w1:p7"}})
        plan.mark_planning([self.card])
        self.pane_exists.assert_not_called()

    def test_a_herdr_error_keeps_the_recorded_marker(self):
        start._write(start.state_dir() / "plans.json", {self.card.ref: {"kind": "pi", "pane_id": "w1:p7"}})
        self.pane_exists.side_effect = start.StartError("Herdr is not reachable", "herdr_unavailable")
        plan.mark_planning([self.card])
        self.assertEqual(self.card.planning, "pi")

    def test_a_linear_card_shows_the_marker_too(self):
        self.card.ref = "ENG-12"
        self.card.source = "linear"
        start._write(start.state_dir() / "plans.json", {"ENG-12": {"kind": "claude", "pane_id": "w1:p7"}})
        plan.mark_planning([self.card])
        self.assertIn("planning: claude", card_text(self.card, False).plain)
        self.assertIn("ENG-12", card_text(self.card, False).plain)

    def test_planning_is_board_only_and_leaves_the_cli_schema_unchanged(self):
        self.assertIsNone(self.card.planning)
        self.card.planning = "pi"
        self.assertNotIn("planning", cli._card_dict(self.card))
