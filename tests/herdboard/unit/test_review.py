"""Review a started card in Hunk, next to the agent that wrote it."""
import unittest
from pathlib import Path
from unittest.mock import patch

from herdboard import activity, review, start
from support.herdboard_cli import CliTestCase
from support.herdboard_review import ReviewTestCase

HUNK = "jhochenbaum.hunkdiff"


class ReviewTests(ReviewTestCase):
    def test_restarted_agent_models_are_scoped_to_their_agent(self):
        self.starter.start(self.card.ref, agent="codex")
        for configured, model, expected in (("claude", "opus", None),
                                             ("codex", "gpt-6.1-sol", "gpt-6.1-sol")):
            with self.subTest(configured=configured):
                self.config.start_agent = configured
                self.config.start_model = model
                self.fake.agent = False
                self.fake.calls.clear()
                self.reviewer.review(self.card.ref)
                args = self.calls("agent", "start")[-1]
                if expected is None:
                    self.assertNotIn("--model", args)
                else:
                    self.assertEqual(args[args.index("--model") + 1], expected)

    def test_a_gone_codex_agent_keeps_valid_config_thinking(self):
        self.starter.start(self.card.ref, agent="codex")
        self.config.start_thinking = "ultra"
        self.fake.agent = False
        self.reviewer.review(self.card.ref)
        args = self.calls("agent", "start")[-1]
        self.assertIn('model_reasoning_effort="ultra"', args)

    def test_a_gone_pi_agent_drops_codex_config_thinking(self):
        self.starter.start(self.card.ref, agent="pi")
        self.config.start_thinking = "ultra"
        self.fake.agent = False
        self.reviewer.review(self.card.ref)
        self.assertNotIn("--thinking", self.calls("agent", "start")[-1])

    def test_a_card_that_was_never_started_cannot_be_reviewed(self):
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.review(self.card.ref)
        self.assertEqual(raised.exception.code, "not_started")

    def test_hunk_opens_next_to_the_live_agent(self):
        self.starter.start(self.card.ref, agent="pi")
        result = self.reviewer.review(self.card.ref)
        self.assertEqual(self.calls("agent", "focus"), [["agent", "focus", "w9:p1"]])
        self.assertEqual(self.calls("plugin", "action", "invoke"),
                         [["plugin", "action", "invoke", "review:branch", "--plugin", HUNK]])
        self.assertEqual(result["review_pane"], "w9:p2:hunk")
        self.assertFalse(result["agent_restarted"])
        self.assertEqual(self.record()["review_pane"], "w9:p2:hunk")
        self.assertEqual(len(self.calls("agent", "start")), 1)
        entries = activity.read(self.config, self.card.ref)
        self.assertEqual([(e["author"], e["message"]) for e in entries], [("herdboard", "Review opened.")])

    def test_a_second_review_reuses_the_open_hunk_pane(self):
        self.starter.start(self.card.ref, agent="pi")
        self.reviewer.review(self.card.ref)
        again = self.reviewer.review(self.card.ref)
        self.assertEqual(again["review_pane"], "w9:p2:hunk")
        self.assertEqual(self.record()["review_pane"], "w9:p2:hunk")

    def test_a_gone_agent_is_started_again_with_the_log(self):
        self.starter.start(self.card.ref, agent="pi")
        activity.append(self.store, self.card.ref, "Two commits ready.", "pi")
        self.fake.agent = False
        result = self.reviewer.review(self.card.ref)
        self.assertTrue(result["agent_restarted"])
        starts = self.calls("agent", "start")
        self.assertEqual(len(starts), 2)
        self.assertEqual(starts[1][starts[1].index("--kind") + 1], "pi")
        brief = (start.state_dir() / "briefs" / f"{self.card.ref}.md").read_text()
        self.assertIn("The card is in review. Review feedback follows. Wait for it.", brief)
        self.assertIn("Two commits ready.", brief)
        messages = [e["message"] for e in activity.read(self.config, self.card.ref)]
        self.assertEqual(messages[-2:], ["Agent restarted for review.", "Review opened."])
        self.assertEqual(self.store.show(self.card.ref).column, "In progress")

    def test_without_hunkdiff_the_review_stops(self):
        self.starter.start(self.card.ref, agent="pi")
        self.fake.hunk = "missing"
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.review(self.card.ref)
        self.assertEqual(raised.exception.code, "hunk_unavailable")

    def test_a_removed_worktree_cannot_be_reviewed(self):
        result = self.starter.start(self.card.ref, agent="pi")
        Path(result["worktree_path"]).rmdir()
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.review(self.card.ref)
        self.assertEqual(raised.exception.code, "not_started")
        self.assertIn("start", str(raised.exception))


class ReviewPaneTests(ReviewTestCase):
    def setUp(self):
        super().setUp()
        self.starter.start(self.card.ref, agent="pi")
        self.store.move(self.card.ref, "Review")
        self.reviewer.review(self.card.ref)

    def test_leaving_review_closes_the_hunk_pane(self):
        start.card_changed(self.store.move(self.card.ref, "In progress"))
        self.assertEqual(self.calls("plugin", "pane", "close"), [["plugin", "pane", "close", "w9:p2:hunk"]])
        self.assertNotIn("review_pane", self.record())

    def test_staying_in_review_keeps_the_hunk_pane(self):
        start.card_changed(self.store.show(self.card.ref))
        self.assertEqual(self.calls("plugin", "pane", "close"), [])
        self.assertEqual(self.record()["review_pane"], "w9:p2:hunk")

    def test_a_deleted_card_closes_the_hunk_pane(self):
        start.card_changed(self.store.delete(self.card.ref), removed=True)
        self.assertEqual(len(self.calls("plugin", "pane", "close")), 1)

    def test_a_pane_that_is_already_gone_is_forgotten(self):
        self.fake.panes["w9"].remove("w9:p2:hunk")
        start.card_changed(self.store.move(self.card.ref, "Done"))
        self.assertNotIn("review_pane", self.record())


class ReviewCliTests(CliTestCase):
    def test_review_prints_where_hunk_opened(self):
        card = self.run_json("add", "Fix it", directory=self.plain)["card"]
        result = {"ref": card["ref"], "workspace_id": "w9", "pane_id": "w9:p1", "review_pane": "w9:p2",
                  "agent_restarted": True}
        with patch("herdboard.cli.Reviewer") as reviewer:
            reviewer.return_value.review.return_value = result
            out, _ = self.run_cli("review", card["ref"], directory=self.plain)
            data = self.run_json("review", card["ref"], directory=self.plain)
        self.assertEqual(out, f"{card['ref']}: Hunk review in pane w9:p2 (workspace w9)\n"
                              "The agent was gone; a new agent started with the card and its log.\n")
        self.assertEqual(data, result)

    def test_review_errors_are_json(self):
        card = self.run_json("add", "Fix it", directory=self.plain)["card"]
        data = self.run_json("review", card["ref"], directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "not_started")

    def test_moving_out_of_review_closes_the_hunk_pane(self):
        card = self.run_json("add", "Fix it", directory=self.plain)["card"]
        with patch.object(start, "close_review_pane") as close:
            self.run_json("move", card["ref"], "Review", directory=self.plain)
            close.assert_not_called()
            self.run_json("move", card["ref"], "In progress", directory=self.plain)
            close.assert_called_once_with(card["ref"])


if __name__ == "__main__":
    unittest.main()


class NoRepositoryReviewTests(ReviewTestCase):
    """A repoless card has no branch for Hunk; only an agent review of its attachments applies."""

    def setUp(self):
        super().setUp()
        self.card = self.store.add(self.store.ensure_inbox(), "Research corvid memory")
        self.starter.start(self.card.ref, no_repo=True, agent="pi", no_agent=True)

    def test_human_review_refuses_a_repoless_card(self):
        with self.assertRaises(review.ReviewError) as raised:
            self.reviewer.review(self.card.ref)
        self.assertEqual(raised.exception.code, "no_repository")
        self.assertIn("--agent", str(raised.exception))
        self.assertIn("finish", str(raised.exception))
        self.assertFalse(self.calls("plugin", "action", "invoke"))
