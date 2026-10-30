import json
import unittest
import uuid
from pathlib import Path
from unittest import mock

from herdboard.config import Config
from herdboard.linear import LinearNetworkError, LinearWriteRejectedError, LinearWriteUnknownError
from herdboard.linear_states import (
    AmbiguousStateError,
    NoStateError,
    StateChoices,
    candidates,
    refuse,
    resolve_state,
)
from herdboard.linear_writes import (
    LinearOfflineError,
    LinearWriter,
    TeamRequiredError,
)
from herdboard.model import NotFoundError
from support import linear_data
from support.herdboard_linear_source import DEAD
from support.herdboard_linear_writes import WriterCase

ENG = {"id": "t-eng", "key": "ENG"}


def state(name, state_type, state_id=None):
    return {"id": state_id or f"id-{name}", "name": name, "type": state_type, "position": 1.0}


def names(found):
    return [s["name"] for s in found]


class CandidateTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()

    def test_states_match_the_column_of_their_type(self):
        states = [state("Backlog", "backlog"), state("Todo", "unstarted"), state("Doing", "started"),
                  state("Done", "completed")]
        self.assertEqual(names(candidates(self.config, states, "In progress")), ["Doing"])
        self.assertEqual(names(candidates(self.config, states, "done")), ["Done"])

    def test_several_states_of_one_type_are_all_candidates(self):
        states = [state("Doing", "started"), state("Coding", "started"), state("Todo", "unstarted")]
        self.assertEqual(names(candidates(self.config, states, "In Progress")), ["Doing", "Coding"])

    def test_state_name_override_wins_over_type_and_ignores_case(self):
        states = [state("Doing", "started"), state("in review", "started")]
        self.assertEqual(names(candidates(self.config, states, "Review")), ["in review"])
        self.assertEqual(names(candidates(self.config, states, "In progress")), ["Doing"])

    def test_canceled_is_a_candidate_only_when_nothing_else_matches(self):
        both = [state("Done", "completed"), state("Cancelled", "canceled")]
        self.assertEqual(names(candidates(self.config, both, "Done")), ["Done"])
        only = [state("Cancelled", "canceled")]
        self.assertEqual(names(candidates(self.config, only, "Done")), ["Cancelled"])

    def test_triage_is_a_candidate_only_when_no_backlog_state_matches(self):
        both = [state("Triage", "triage"), state("Backlog", "backlog")]
        self.assertEqual(names(candidates(self.config, both, "Backlog")), ["Backlog"])
        only = [state("Triage", "triage")]
        self.assertEqual(names(candidates(self.config, only, "Backlog")), ["Triage"])

    def test_two_backlog_states_stay_candidates(self):
        found = candidates(self.config, [state("Icebox", "backlog"), state("Backlog", "backlog"),
                                         state("Triage", "triage")], "Backlog")
        self.assertEqual(names(found), ["Icebox", "Backlog"])

    def test_config_columns_change_the_mapping(self):
        config = Config()
        config.linear.columns["started"] = "Doing now"
        self.assertEqual(names(candidates(config, [state("Doing", "started")], "Doing now")), ["Doing"])
        self.assertEqual(candidates(config, [state("Doing", "started")], "In progress"), [])

    def test_no_match(self):
        self.assertEqual(candidates(self.config, [state("Todo", "unstarted")], "Review"), [])


class ResolveTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name) / "o1" / "u1"
        self.choices = StateChoices(self.folder)
        self.config = Config()
        self.states = [state("Todo", "unstarted"), state("Doing", "started"), state("Coding", "started")]
        self.asked = []

    def ask(self, pick):
        def ask(team_key, column, found):
            self.asked.append((team_key, column, names(found)))
            return next(s for s in found if s["name"] == pick)
        return ask

    def resolve(self, column="In progress", ask=refuse, state_name=None, states=None):
        return resolve_state(self.config, self.choices, ENG, states or self.states, column, ask,
                             state_name)

    def test_one_candidate_needs_no_question(self):
        self.assertEqual(self.resolve("Todo")["name"], "Todo")
        self.assertEqual(self.asked, [])
        self.assertFalse(self.folder.exists())

    def test_no_candidate_names_team_and_column(self):
        with self.assertRaises(NoStateError) as caught:
            self.resolve("Review")
        self.assertEqual(caught.exception.code, "no_state")
        self.assertIn("ENG", str(caught.exception))
        self.assertIn("Review", str(caught.exception))

    def test_ambiguous_without_ask_lists_the_candidates(self):
        with self.assertRaises(AmbiguousStateError) as caught:
            self.resolve()
        error = caught.exception
        self.assertEqual((error.code, error.candidates), ("ambiguous_state", ["Doing", "Coding"]))
        self.assertIn("Doing, Coding", str(error))
        self.assertIn("--state", str(error))

    def test_answer_is_stored_by_team_and_column(self):
        self.assertEqual(self.resolve(ask=self.ask("Coding"))["id"], "id-Coding")
        stored = json.loads((self.folder / "state-choices.json").read_text())
        self.assertEqual(stored, {"t-eng": {"in progress": "id-Coding"}})
        # The stored choice answers the next question.
        self.assertEqual(self.resolve(ask=self.ask("Doing"))["id"], "id-Coding")
        self.assertEqual(self.asked, [("ENG", "In progress", ["Doing", "Coding"])])

    def test_stale_choice_is_dropped_and_asked_again(self):
        self.resolve(ask=self.ask("Coding"))
        left = [s for s in self.states if s["name"] != "Coding"] + [state("Building", "started")]
        self.assertEqual(self.resolve(ask=self.ask("Building"), states=left)["name"], "Building")
        self.assertEqual(len(self.asked), 2)
        self.assertEqual(json.loads((self.folder / "state-choices.json").read_text()),
                         {"t-eng": {"in progress": "id-Building"}})

    def test_stale_choice_without_a_new_answer_is_removed(self):
        self.resolve(ask=self.ask("Coding"))
        left = [s for s in self.states if s["name"] != "Coding"] + [state("Building", "started")]
        with self.assertRaises(AmbiguousStateError):
            self.resolve(states=left)
        self.assertEqual(json.loads((self.folder / "state-choices.json").read_text()), {})

    def test_state_name_answers_ignoring_case_and_is_stored(self):
        self.assertEqual(self.resolve(state_name="coding")["id"], "id-Coding")
        self.assertEqual(self.resolve()["id"], "id-Coding")

    def test_state_name_replaces_the_stored_choice(self):
        self.resolve(ask=self.ask("Coding"))
        self.assertEqual(self.resolve(state_name="Doing")["id"], "id-Doing")
        self.assertEqual(self.resolve()["id"], "id-Doing")

    def test_unknown_state_name_lists_candidates(self):
        with self.assertRaises(AmbiguousStateError) as caught:
            self.resolve(state_name="Nope")
        self.assertIn("Nope", str(caught.exception))
        self.assertEqual(caught.exception.candidates, ["Doing", "Coding"])
        self.assertFalse(self.folder.exists())

    def test_state_name_must_be_a_candidate_even_when_there_is_one(self):
        with self.assertRaises(AmbiguousStateError) as caught:
            self.resolve("Todo", state_name="Doing")
        self.assertEqual(caught.exception.candidates, ["Todo"])

    def test_a_cancelled_question_is_an_error(self):
        with self.assertRaises(Exception) as caught:
            self.resolve(ask=lambda *_: None)
        self.assertEqual(caught.exception.code, "cancelled")

    def test_choices_are_per_team(self):
        self.resolve(ask=self.ask("Coding"))
        other = {"id": "t-ops", "key": "OPS"}
        found = resolve_state(self.config, self.choices, other, self.states, "In progress",
                              self.ask("Doing"))
        self.assertEqual(found["name"], "Doing")

    def test_unreadable_choices_file_is_ignored(self):
        self.folder.mkdir(parents=True)
        (self.folder / "state-choices.json").write_text("not json")
        self.assertEqual(self.resolve(ask=self.ask("Doing"))["name"], "Doing")


class MoveTests(WriterCase):
    def test_ops_move_uses_ops_state_not_eng_state(self):
        issue = self.fixtures["issues"][0]
        ops = self.fixtures["teams"][1]
        issue["identifier"], issue["team"] = "OPS-1", {"id": ops["id"], "key": "OPS"}
        issue["state"] = next(s for s in ops["states"] if s["name"] == "Todo")
        moved = self.writer().move("OPS-1", "In progress")
        self.assertEqual(self.inputs("IssueUpdate"), [{"stateId": "ws-OPS-Doing"}])
        self.assertEqual(moved["state"]["id"], "ws-OPS-Doing")
        eng = next(s for s in self.fixtures["teams"][0]["states"] if s["name"] == "Doing")
        self.assertNotEqual(moved["state"]["id"], eng["id"])
        from herdboard.linear import LinearWriteRejectedError
        with self.assertRaises(LinearWriteRejectedError):
            self.source_in_use.client.update_issue(issue["id"], state_id=eng["id"])
        self.assertEqual(issue["state"]["id"], "ws-OPS-Doing")

    def test_move_changes_the_state(self):
        issue = self.writer().move("ENG-1", "In progress")
        self.assertEqual(self.inputs("IssueUpdate"), [{"stateId": "ws-ENG-Doing"}])
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Doing")
        self.assertEqual((issue["identifier"], issue["state"]["name"]), ("ENG-1", "Doing"))
        self.assertEqual(issue["title"], "Issue 1")

    def test_move_finds_the_issue_by_lowercase_identifier(self):
        self.writer().move("eng-1", "In progress")
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Doing")

    def test_move_to_the_current_state_changes_nothing(self):
        issue = self.writer().move("ENG-1", "Todo")
        self.assertEqual(self.requests("IssueUpdate"), [])
        self.assertEqual(issue["identifier"], "ENG-1")

    def test_move_to_the_current_state_with_a_position_still_writes(self):
        self.writer().move("ENG-1", "Todo", "top")
        self.assertEqual(len(self.requests("IssueUpdate")), 1)

    def test_unknown_issue(self):
        with self.assertRaises(NotFoundError) as caught:
            self.writer().move("ENG-99", "Todo")
        self.assertIn("ENG-99", str(caught.exception))
        self.assertEqual(self.requests("IssueUpdate"), [])

    def test_no_state_for_the_column(self):
        team = self.fixtures["teams"][0]
        team["states"] = [s for s in team["states"] if s["name"] != "In Review"]
        with self.assertRaises(NoStateError) as caught:
            self.writer().move("ENG-1", "Review")
        self.assertIn("ENG", str(caught.exception))
        self.assertIn("Review", str(caught.exception))
        self.assertEqual(self.requests("IssueUpdate"), [])

    def test_position_top_goes_below_the_lowest_sort_order_of_the_target_state(self):
        # ENG-5 (sort order 5.0) is the only issue in Doing.
        self.writer().move("ENG-1", "In progress", "top")
        self.assertEqual(self.inputs("IssueUpdate"), [{"stateId": "ws-ENG-Doing", "sortOrder": 4.0}])

    def test_position_bottom_goes_above_the_highest_sort_order(self):
        # ENG-2 (sort order 2.0) is the only issue in In Review.
        self.writer().move("ENG-1", "Review", "bottom")
        self.assertEqual(self.inputs("IssueUpdate"), [{"stateId": "ws-ENG-In Review", "sortOrder": 3.0}])

    def test_position_sort_orders_are_read_for_the_issues_team_and_state(self):
        self.writer().move("ENG-1", "In progress", "top")
        filt = self.requests("StateSortOrders")[0]["variables"]["filter"]
        self.assertEqual(filt, {"team": {"id": {"eq": "t-eng"}}, "state": {"id": {"eq": "ws-ENG-Doing"}}})

    def test_position_in_an_empty_state_sends_no_sort_order(self):
        self.writer().move("ENG-1", "Backlog", "top")
        self.assertEqual(self.inputs("IssueUpdate"), [{"stateId": "ws-ENG-Backlog"}])

    def test_position_in_a_completed_state_counts_only_the_done_days_window(self):
        self.writer().move("ENG-1", "Done", "top")
        filt = self.requests("StateSortOrders")[0]["variables"]["filter"]
        self.assertIn("or", filt)
        self.assertEqual(self.inputs("IssueUpdate"), [{"stateId": "ws-ENG-Done", "sortOrder": 5.0}])

    def test_done_picks_the_completed_state_not_the_canceled_one(self):
        issue = self.writer().done("ENG-1")
        self.assertEqual(self.inputs("IssueUpdate"), [{"stateId": "ws-ENG-Done"}])
        self.assertEqual(issue["state"]["type"], "completed")

    def test_done_uses_the_column_that_config_maps_completed_to(self):
        config = Config(todos_dir=self.root / "todos")
        config.linear.columns["completed"] = "Shipped"
        config.linear.state_names["Done"] = "Shipped"
        writer = LinearWriter(config, self.source())
        writer.done("ENG-1")
        self.assertEqual(self.inputs("IssueUpdate"), [{"stateId": "ws-ENG-Done"}])


class AmbiguityTests(WriterCase):
    def setUp(self):
        super().setUp()
        linear_data.add_state(self.fixtures, "ENG", "Coding", "started")

    def test_ambiguous_state_without_a_question_fails_before_any_write(self):
        with self.assertRaises(AmbiguousStateError) as caught:
            self.writer().move("ENG-1", "In progress")
        self.assertEqual(caught.exception.candidates, ["Doing", "Coding"])
        self.assertEqual(self.requests("IssueUpdate"), [])

    def test_answer_is_used_and_stored(self):
        asked = []

        def ask(team_key, column, found):
            asked.append(names(found))
            return found[1]

        writer = self.writer()
        writer.move("ENG-1", "In progress", ask=ask)
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Coding")
        self.assertEqual(json.loads(self.choices_file().read_text()),
                         {"t-eng": {"in progress": "ws-ENG-Coding"}})
        writer.move("ENG-3", "In progress", ask=ask)
        self.assertEqual(asked, [["Doing", "Coding"]])
        self.assertEqual(self.issue("ENG-3")["state"]["name"], "Coding")

    def test_state_name_is_stored(self):
        self.writer().move("ENG-1", "In progress", state_name="coding")
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Coding")
        self.writer().move("ENG-3", "In progress")
        self.assertEqual(self.issue("ENG-3")["state"]["name"], "Coding")

    def test_stale_stored_choice_asks_again(self):
        self.writer().move("ENG-1", "In progress", state_name="Coding")
        team = self.fixtures["teams"][0]
        team["states"] = [s for s in team["states"] if s["name"] != "Coding"]
        linear_data.add_state(self.fixtures, "ENG", "Building", "started")
        with self.assertRaises(AmbiguousStateError):
            self.writer().move("ENG-3", "In progress")
        self.writer().move("ENG-3", "In progress", ask=lambda _t, _c, found: found[-1])
        self.assertEqual(self.issue("ENG-3")["state"]["name"], "Building")


class UnknownResultTests(WriterCase):
    def test_delayed_commit_after_first_verification_read_succeeds(self):
        import time
        self.slow_mutation("delayed_commit")
        writer = self.writer()
        real = writer.source.client.issue_for_write
        reads = []

        def verify(identifier):
            found = real(identifier)
            reads.append(found)
            return found

        writer.source.client.issue_for_write = verify
        # A real wait lets the fake commit after the first verification read.
        writer.wait = time.sleep
        issue = writer.create("Delayed thing", team="ENG")
        self.assertIsNone(reads[0])
        self.assertEqual(issue["title"], "Delayed thing")
        self.assertEqual(len(self.requests("IssueCreate")), 1)

    def test_unknown_applied_update_invalidates_cache(self):
        self.slow_mutation()
        writer = self.writer()
        writer.source.my_issues()
        real = writer.source.client.issue_for_write
        calls = []

        def verify(identifier):
            if calls:
                raise LinearNetworkError("down")
            calls.append(identifier)
            return real(identifier)

        writer.source.client.issue_for_write = verify
        with self.assertRaises(LinearWriteUnknownError):
            writer.move("ENG-1", "In progress")
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Doing")
        self.assertFalse((writer.source.account_dir() / "mine.json").exists())

    def test_unknown_applied_create_invalidates_cache(self):
        self.slow_mutation()
        writer = self.writer()
        writer.source.my_issues()
        writer.source.client.issue_for_write = mock.Mock(side_effect=LinearNetworkError("down"))
        with self.assertRaises(LinearWriteUnknownError):
            writer.create("Created", team="ENG")
        self.assertEqual(len(self.requests("IssueCreate")), 1)
        self.assertFalse((writer.source.account_dir() / "mine.json").exists())

    def test_update_that_timed_out_but_applied_is_a_success(self):
        self.slow_mutation()
        issue = self.writer().move("ENG-1", "In progress", "top")
        self.assertEqual(len(self.requests("IssueUpdate")), 1)
        self.assertEqual(issue["state"]["name"], "Doing")
        self.assertEqual(issue["sortOrder"], 4.0)

    def test_update_without_observed_effect_is_unknown(self):
        self.fake.mutation_failure = "http_500"
        with self.assertRaises(LinearWriteUnknownError) as caught:
            self.writer().move("ENG-1", "In progress")
        self.assertEqual(caught.exception.code, "linear_unknown")
        self.assertIn("ENG-1", str(caught.exception))
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertEqual(len(self.requests("IssueUpdate")), 1)
        self.assertEqual(self.issue("ENG-1")["state"]["name"], "Todo")

    def test_a_state_change_without_the_requested_sort_order_is_unknown(self):
        self.fake.mutation_failure = "http_500"
        # The issue already has the state, so only the sort order was asked for.
        with self.assertRaises(LinearWriteUnknownError):
            self.writer().move("ENG-1", "Todo", "top")

    def test_update_with_a_failing_follow_up_fetch_is_unknown(self):
        self.fake.mutation_failure = "http_500"
        writer = self.writer()
        client, real, calls = writer.source.client, writer.source.client.issue_for_write, []

        def flaky(identifier):
            if calls:
                raise LinearNetworkError("down")
            calls.append(identifier)
            return real(identifier)

        client.issue_for_write = flaky
        with self.assertRaises(LinearWriteUnknownError) as caught:
            writer.move("ENG-1", "In progress")
        self.assertEqual(caught.exception.code, "linear_unknown")
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertIn("ENG-1", str(caught.exception))
        self.assertIn("check", str(caught.exception))
        self.assertEqual(len(self.requests("IssueUpdate")), 1)

    def test_rate_limited_write_is_sent_once_and_says_so(self):
        self.fake.mutation_failure = "http_429"
        with self.assertRaises(Exception) as caught:
            self.writer().move("ENG-1", "In progress")
        self.assertIn("rate limited", str(caught.exception))
        self.assertEqual(caught.exception.exit_code, 3)
        self.assertEqual(len(self.requests("IssueUpdate")), 1)

    def test_rejected_write_is_a_linear_error(self):
        self.fake.mutation_failure = "graphql_error"
        with self.assertRaises(Exception) as caught:
            self.writer().move("ENG-1", "In progress")
        self.assertEqual(caught.exception.code, "linear_rejected")
        self.assertEqual(len(self.requests("IssueUpdate")), 1)

    def test_create_that_timed_out_but_applied_is_a_success(self):
        self.slow_mutation()
        issue = self.writer().create("New thing", team="ENG")
        self.assertEqual(issue["title"], "New thing")
        creates = self.requests("IssueCreate")
        self.assertEqual(len(creates), 1)
        self.assertEqual(issue["id"], creates[0]["variables"]["input"]["id"])
        self.assertEqual(len(self.fixtures["issues"]), 7)

    def test_create_without_observed_effect_is_unknown(self):
        self.fake.mutation_failure = "http_500"
        with self.assertRaises(LinearWriteUnknownError) as caught:
            self.writer().create("New thing", team="ENG")
        self.assertEqual(caught.exception.code, "linear_unknown")
        self.assertEqual(caught.exception.issue_id, self.inputs("IssueCreate")[0]["id"])
        self.assertEqual(len(self.requests("IssueCreate")), 1)
        self.assertEqual(len(self.fixtures["issues"]), 6)

    def test_create_with_a_failing_follow_up_fetch_is_unknown(self):
        self.fake.mutation_failure = "http_500"
        writer = self.writer()
        writer.source.client.issue_for_write = mock.Mock(side_effect=LinearNetworkError("down"))
        with self.assertRaises(LinearWriteUnknownError) as caught:
            writer.create("New thing", team="ENG")
        self.assertIn("check", str(caught.exception))
        self.assertEqual(len(self.requests("IssueCreate")), 1)
        issue_id = self.requests("IssueCreate")[0]["variables"]["input"]["id"]
        self.assertEqual(caught.exception.issue_id, issue_id)
        self.assertIn(issue_id, str(caught.exception))
        self.assertEqual(writer.source.client.issue_for_write.call_args_list,
                         [mock.call(issue_id)] * 3)


class CreateTests(WriterCase):
    PLATFORM = {"id": "p-plat", "name": "Platform", "teams": [ENG]}

    def create_input(self):
        (inputs,) = self.inputs("IssueCreate")
        return inputs

    def test_create_sends_a_client_uuid_and_assigns_the_viewer(self):
        issue = self.writer().create("New thing", team="eng")
        inputs = self.create_input()
        self.assertEqual(str(uuid.UUID(inputs["id"])), inputs["id"])
        self.assertEqual(inputs, {"id": inputs["id"], "teamId": "t-eng", "title": "New thing",
                                  "stateId": "ws-ENG-Backlog", "assigneeId": "u1"})
        self.assertEqual((issue["id"], issue["identifier"]), (inputs["id"], "ENG-7"))

    def test_two_creates_use_different_ids(self):
        writer = self.writer()
        writer.create("One", team="ENG")
        writer.create("Two", team="ENG")
        first, second = self.inputs("IssueCreate")
        self.assertNotEqual(first["id"], second["id"])

    def test_team_key_picks_the_team_even_with_a_project(self):
        self.writer().create("x", team="OPS", project=self.PLATFORM)
        self.assertEqual(self.create_input()["teamId"], "t-ops")

    def test_unknown_team_key(self):
        with self.assertRaises(NotFoundError) as caught:
            self.writer().create("x", team="NOPE")
        self.assertIn("NOPE", str(caught.exception))
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_the_single_team_of_the_project(self):
        self.writer().create("x", project=self.PLATFORM)
        inputs = self.create_input()
        self.assertEqual((inputs["teamId"], inputs["projectId"]), ("t-eng", "p-plat"))

    def test_the_single_team_of_the_viewer(self):
        self.fixtures["my_team_ids"] = ["t-ops"]
        self.writer().create("x")
        self.assertEqual(self.create_input()["teamId"], "t-ops")

    def test_a_project_with_several_teams_uses_the_viewers_team_among_them(self):
        self.fixtures["my_team_ids"] = ["t-ops"]
        mobile = {"id": "p-mob", "name": "Mobile", "teams": [ENG, {"id": "t-ops", "key": "OPS"}]}
        self.writer().create("x", project=mobile)
        self.assertEqual(self.create_input()["teamId"], "t-ops")

    def test_several_candidate_teams_need_a_team_key(self):
        mobile = {"id": "p-mob", "name": "Mobile", "teams": [ENG, {"id": "t-ops", "key": "OPS"}]}
        for kwargs in ({}, {"project": mobile}):
            with self.assertRaises(TeamRequiredError) as caught:
                self.writer().create("x", **kwargs)
            self.assertEqual(caught.exception.code, "team_required")
            self.assertIn("ENG", str(caught.exception))
            self.assertIn("OPS", str(caught.exception))
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_project_and_labels_are_sent(self):
        self.writer().create("x", project=self.PLATFORM, label_ids=["l-api", "l-bug"])
        inputs = self.create_input()
        self.assertEqual(inputs["labelIds"], ["l-api", "l-bug"])
        self.assertEqual(self.fixtures["issues"][-1]["project"]["name"], "Platform")

    def test_column_and_state_choose_the_workflow_state(self):
        linear_data.add_state(self.fixtures, "ENG", "Coding", "started")
        self.writer().create("x", team="ENG", column="In progress", state_name="Coding")
        self.assertEqual(self.create_input()["stateId"], "ws-ENG-Coding")

    def test_default_column_is_the_first_configured_column(self):
        config = Config(todos_dir=self.root / "todos", columns=["Todo", "Done"])
        LinearWriter(config, self.source()).create("x", team="ENG")
        self.assertEqual(self.create_input()["stateId"], "ws-ENG-Todo")

    def test_ambiguous_column_fails_before_the_write(self):
        linear_data.add_state(self.fixtures, "ENG", "Icebox", "backlog")
        with self.assertRaises(AmbiguousStateError):
            self.writer().create("x", team="ENG")
        self.assertEqual(self.requests("IssueCreate"), [])


class ConnectionTests(WriterCase):
    def test_offline_writes_are_refused_without_a_request(self):
        writer = self.writer(offline=True)
        for call in (lambda: writer.move("ENG-1", "Todo"), lambda: writer.done("ENG-1"),
                     lambda: writer.create("x", team="ENG")):
            with self.assertRaises(LinearOfflineError) as caught:
                call()
            self.assertEqual(caught.exception.code, "linear_offline")
            self.assertIn("connection", str(caught.exception))
            self.assertEqual(caught.exception.exit_code, 3)
        self.assertEqual(self.fake.requests, [])

    def test_unreachable_linear_refuses_the_write(self):
        self.endpoint = DEAD
        with self.assertRaises(LinearOfflineError):
            self.writer().move("ENG-1", "In progress")
        self.assertEqual(self.fake.requests, [])

    def test_linear_off_is_reported(self):
        import os
        del os.environ["LINEAR_API_KEY"]
        with self.assertRaises(Exception) as caught:
            self.writer().move("ENG-1", "Todo")
        self.assertEqual(caught.exception.code, "linear_disabled")


class CacheTests(WriterCase):
    def cached(self, source):
        return sorted(p.name for p in source.account_dir().glob("*.json"))

    def test_a_write_drops_the_cached_queries_of_the_account(self):
        writer = self.writer()
        writer.source.my_issues()
        writer.source.project_issues("p-plat")
        writer.source.issue("ENG-1")
        self.assertEqual(len(self.cached(writer.source)), 3)
        writer.move("ENG-1", "In progress")
        self.assertEqual(self.cached(writer.source), [])

    def test_stored_state_choices_survive_the_cache_drop(self):
        linear_data.add_state(self.fixtures, "ENG", "Coding", "started")
        writer = self.writer()
        writer.source.my_issues()
        writer.move("ENG-1", "In progress", state_name="Coding")
        self.assertEqual(self.cached(writer.source), ["state-choices.json"])

    def test_a_no_op_move_keeps_the_cache(self):
        writer = self.writer()
        writer.source.my_issues()
        writer.move("ENG-1", "Todo")
        self.assertEqual(self.cached(writer.source), ["mine.json"])

    def test_a_failed_write_keeps_the_cache(self):
        writer = self.writer()
        writer.source.my_issues()
        self.fake.mutation_failure = "graphql_error"
        with self.assertRaises(LinearWriteRejectedError):
            writer.move("ENG-1", "In progress")
        self.assertEqual(self.cached(writer.source), ["mine.json"])

    def test_create_drops_the_cache(self):
        writer = self.writer()
        writer.source.my_issues()
        writer.create("x", team="ENG")
        self.assertEqual(self.cached(writer.source), [])

    def test_next_list_shows_the_change(self):
        writer = self.writer()
        before = {i["identifier"]: i["state"]["name"] for i in writer.source.my_issues().data}
        writer.move("ENG-1", "In progress")
        after = {i["identifier"]: i["state"]["name"] for i in writer.source.my_issues().data}
        self.assertEqual((before["ENG-1"], after["ENG-1"]), ("Todo", "Doing"))


if __name__ == "__main__":
    unittest.main()
