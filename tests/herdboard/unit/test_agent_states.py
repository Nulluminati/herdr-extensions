"""Agent states: the Herdr status of each agent pane that works on a card, for the board's glyphs."""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from herdboard import agent_states, start
from herdboard.store import CardView


def card(ref, worktree=True):
    return CardView(ref=ref, board="api", path=None, column="Todo", title="Ship it", checked=False, due=None,
                    tags=[], body=[], worktree=worktree)


class AgentListHerdr:
    """Answers `herdr agent list` with the given pane statuses, or with an error."""

    def __init__(self, statuses):
        self.statuses = statuses
        self.error = None
        self.calls = []

    def __call__(self, argv, timeout=120):
        self.calls.append(argv[1:])
        if self.error:
            return subprocess.CompletedProcess(argv, 1, "", json.dumps({"error": {"code": self.error,
                                                                                  "message": "broken"}}))
        agents = [{"pane_id": pane, "agent": "claude", "agent_status": status}
                  for pane, status in self.statuses.items()]
        return subprocess.CompletedProcess(argv, 0, json.dumps({"result": {"agents": agents}}), "")


class AgentStatesTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name) / "state" / "herdboard"
        self.enterContext(patch.dict(os.environ, {"XDG_STATE_HOME": str(Path(temp.name) / "state")}))
        self.fake = AgentListHerdr({"w1:p1": "working", "w1:p2": "blocked", "w1:p3": "done", "w2:p1": "idle"})
        self.enterContext(patch.object(start, "command", self.fake))

    def write(self, name, records):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / name).write_text(json.dumps(records))

    def test_pane_statuses_map_each_pane_to_its_status(self):
        self.assertEqual(agent_states.pane_statuses(),
                         {"w1:p1": "working", "w1:p2": "blocked", "w1:p3": "done", "w2:p1": "idle"})

    def test_a_herdr_error_gives_no_statuses(self):
        self.fake.error = "server_unavailable"
        self.assertEqual(agent_states.pane_statuses(), {})

    def test_mark_sets_each_agent_status_from_its_pane(self):
        self.write("starts.json", {"t123": {"ref": "t123", "pane_id": "w1:p1",
                                            "agent_review": {"pane_id": "w1:p2", "kind": "pi"},
                                            "pr_write": {"pane_id": "w1:p3"}}})
        self.write("plans.json", {"t123": {"pane_id": "w2:p1", "kind": "claude"}})
        item = card("t123")
        agent_states.mark([item])
        self.assertEqual((item.agent_status, item.review_status, item.pr_write_status, item.planning_status),
                         ("working", "blocked", "done", "idle"))
        self.assertEqual(len(self.fake.calls), 1)

    def test_a_record_found_by_its_ref_field_counts(self):
        self.write("starts.json", {"api/t123": {"ref": "t123", "pane_id": "w1:p1"}})
        item = card("t123")
        agent_states.mark([item])
        self.assertEqual(item.agent_status, "working")

    def test_a_pane_that_herdr_does_not_list_gives_none(self):
        self.write("starts.json", {"t123": {"ref": "t123", "pane_id": "w9:p1", "agent_review": {"pane_id": "w9:p2"}}})
        self.write("plans.json", {"t123": {"pane_id": "w9:p3"}})
        item = card("t123")
        item.agent_status = "working"
        agent_states.mark([item])
        self.assertEqual((item.agent_status, item.review_status, item.pr_write_status, item.planning_status),
                         (None, None, None, None))

    def test_the_work_agent_status_needs_the_worktree(self):
        self.write("starts.json", {"t123": {"ref": "t123", "pane_id": "w1:p1"}})
        item = card("t123", worktree=False)
        agent_states.mark([item])
        self.assertIsNone(item.agent_status)

    def test_a_herdr_error_marks_no_status_and_raises_nothing(self):
        self.write("starts.json", {"t123": {"ref": "t123", "pane_id": "w1:p1"}})
        self.fake.error = "herdr_error"
        item = card("t123")
        agent_states.mark([item])
        self.assertIsNone(item.agent_status)

    def test_unreadable_state_files_mark_no_status(self):
        self.write("starts.json", {"t123": {"ref": "t123", "pane_id": "w1:p1"}})
        (self.state / "plans.json").write_text("not json")
        item = card("t123")
        agent_states.mark([item])
        self.assertEqual(item.agent_status, "working")
        self.assertIsNone(item.planning_status)


if __name__ == "__main__":
    unittest.main()
