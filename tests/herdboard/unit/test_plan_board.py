"""The board shows a planner without moving its card to another column."""
from unittest.mock import patch

from herdboard import cli, plan, start
from herdboard.board import card_text
from herdboard.model import IoError
from support.herdboard_board import BoardTestCase


class PlanningStatusTests(BoardTestCase):
    def setUp(self):
        super().setUp()
        self.card = self.store.add(self.repo_path, "Plan login", "Todo")
        self.pane_exists = self.enterContext(patch.object(plan, "_pane_exists", return_value=True))

    def test_no_record_means_no_marker_or_herdr_call(self):
        plan.mark_planning([self.card])
        self.assertIsNone(self.card.planning)
        self.assertNotIn("✎", card_text(self.card, False).plain)
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

    def test_an_unreadable_state_clears_a_previous_marker(self):
        self.card.planning = "pi"
        with patch.object(plan, "_read", side_effect=IoError("Cannot read planning state")):
            plan.mark_planning([self.card])
        self.assertIsNone(self.card.planning)
        self.pane_exists.assert_not_called()

    def test_the_running_guard_ignores_malformed_pane_records(self):
        for record in (None, [], {}, {"pane_id": 7}, {"pane_id": ""}):
            with self.subTest(record=record):
                start._write(start.state_dir() / "plans.json", {self.card.ref: record})
                self.assertIsNone(plan.running_planner(self.card.ref))
        self.pane_exists.assert_not_called()

    def test_the_running_guard_still_blocks_if_the_planner_kind_is_missing(self):
        start._write(start.state_dir() / "plans.json", {self.card.ref: {"pane_id": "w1:p7"}})
        self.assertEqual(plan.running_planner(self.card.ref), "w1:p7")

    def test_the_running_guard_keeps_errors_visible(self):
        start._write(start.state_dir() / "plans.json", {self.card.ref: {"pane_id": "w1:p7"}})
        self.pane_exists.side_effect = start.StartError("Herdr is not reachable", "herdr_unavailable")
        with self.assertRaises(start.StartError):
            plan.running_planner(self.card.ref)

    def test_a_linear_card_shows_the_marker_too(self):
        self.card.ref = "ENG-12"
        self.card.source = "linear"
        start._write(start.state_dir() / "plans.json", {"ENG-12": {"kind": "claude", "pane_id": "w1:p7"}})
        plan.mark_planning([self.card])
        self.assertIn("✎ claude", card_text(self.card, False).plain)
        self.assertIn("ENG-12", card_text(self.card, False).plain)

    def test_planning_is_board_only_and_leaves_the_cli_schema_unchanged(self):
        self.assertIsNone(self.card.planning)
        self.card.planning = "pi"
        self.assertNotIn("planning", cli._card_dict(self.card))
