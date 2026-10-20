import json
import shutil
import sys
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))
sys.path.insert(0, str(ROOT / "tests"))

import linear_data  # noqa: E402
from herdboard.config import Config, LinearConfig  # noqa: E402
from herdboard.linear import LinearClient, LinearWriteRejectedError, LinearWriteUnknownError  # noqa: E402
from herdboard.linear_source import LinearSource  # noqa: E402
from herdboard.linear_states import AmbiguousStateError, StateChoices  # noqa: E402
from herdboard.linear_writes import LinearOfflineError, LinearWriter  # noqa: E402
from herdboard.model import ConflictError, HerdboardError, NotFoundError  # noqa: E402
from herdboard.promote import AlreadyPromotedError, NotATodoError, Promoter  # noqa: E402
from herdboard.store import Store  # noqa: E402
from test_herdboard_linear_writes import WriterCase  # noqa: E402

FIXTURE = ROOT / "tests/fixtures/herdboard/kanban-promote.md"
HEADER = "---\n\nkanban-plugin: board\n\n---\n\n"
FOOTER = '\n%% kanban:settings\n```\n{"kanban-plugin":"board"}\n```\n%%'
ENG = {"id": "t-eng", "key": "ENG"}
OPS = {"id": "t-ops", "key": "OPS"}


class PromoteCase(WriterCase):
    def setUp(self):
        super().setUp()
        self.todos = self.root / "todos"
        self.todos.mkdir()
        self.inbox = self.todos / "inbox.md"
        shutil.copy(FIXTURE, self.inbox)
        self.config = Config(todos_dir=self.todos)
        self.fixtures["labels"] += [
            {"id": "l-home-ws", "name": "home", "team": None},
            {"id": "l-home-eng", "name": "home", "team": ENG},
            {"id": "l-ops-only", "name": "Unknown-tag", "team": OPS},
        ]

    def promoter(self, config=None, **source_kwargs):
        config = config or self.config
        writer = LinearWriter(config, LinearSource(config, **source_kwargs))
        writer.wait = lambda _: None
        self.writer_in_use = writer
        return Promoter(config, Store(config), writer)

    def promote(self, ref="t5r1", **kwargs):
        kwargs.setdefault("team", "ENG")
        return self.promoter().promote(ref, **kwargs)

    def write_board(self, body, done=True):
        if done and "## Done" not in body and "**Complete**" not in body:
            body += "\n## Done\n\n**Complete**\n"
        self.inbox.write_text(HEADER + body + FOOTER, encoding="utf-8")

    def text(self):
        return self.inbox.read_text(encoding="utf-8")

    def record_file(self):
        return self.state / "herdboard" / "o1" / "u1" / "promotions.json"

    def all_records(self):
        try:
            return json.loads(self.record_file().read_text())
        except FileNotFoundError:
            return {}

    def records(self, todos=None):
        """The records of one todo store, by card ID."""
        prefix = f"{(todos or self.todos).resolve()}::"
        return {key[len(prefix):]: record for key, record in self.all_records().items()
                if key.startswith(prefix)}

    def unfinished(self):
        """Leave a record with the parent created and the first sub-issue unknown."""
        self.fake.mutation_failures = [None, "http_500"]
        with self.assertRaises(LinearWriteUnknownError):
            self.promote()

    def mark(self, identifier="ENG-7"):
        self.inbox.write_text(self.text().replace("^t5r1", f"→ {identifier} ^t5r1"))

    def creates(self):
        return self.inputs("IssueCreate")

    def issues_titled(self, title):
        return [i for i in self.fixtures["issues"] if i["title"] == title]

    def slow(self, failures):
        """Make the listed mutations fail in order. A `timeout_after` write is applied, but the
        client gives up before it hears about it. A first request builds the schema before the
        client timeout gets short."""
        self.writer().client().viewer()
        self.timeout = 0.3
        self.fake.mutation_delay = 0.8
        self.fake.mutation_failures = list(failures)


class ConversionTests(PromoteCase):
    def test_parent_has_title_due_date_label_assignee_and_state(self):
        result = self.promote()
        parent = self.creates()[0]
        self.assertEqual((result.issue["identifier"], result.issue["title"]),
                         ("ENG-7", "Renovate the bathroom"))
        self.assertEqual(parent["title"], "Renovate the bathroom")
        self.assertEqual(parent["dueDate"], "2026-11-02")
        self.assertEqual(parent["labelIds"], ["l-home-eng"])
        self.assertEqual((parent["teamId"], parent["assigneeId"]), ("t-eng", "u1"))
        self.assertEqual(parent["stateId"], "ws-ENG-Backlog")
        self.assertNotIn("parentId", parent)
        self.assertEqual(str(uuid.UUID(parent["id"])), parent["id"])

    def test_leftover_text_and_tags_without_a_label_go_into_the_description(self):
        self.promote()
        self.assertEqual(self.creates()[0]["description"], "\n".join([
            "Budget is tight, ask for two quotes",
            "\t- [ ] Measure the ceiling",
            "\tCeiling is uneven",
            "- Not a checklist item",
            "",
            "#Unknown-tag",
        ]))

    def test_sub_issues_follow_the_checklist_in_order(self):
        result = self.promote()
        parent, *children = self.creates()
        self.assertEqual([c["title"] for c in children],
                         ["Choose tiles", "Measure the room", "Book the plumber"])
        self.assertEqual({c["parentId"] for c in children}, {parent["id"]})
        self.assertEqual([c["assigneeId"] for c in children], ["u1"] * 3)
        self.assertEqual([i["identifier"] for i in result.sub_issues], ["ENG-8", "ENG-9", "ENG-10"])
        found = self.issues_titled("Measure the room")[0]
        self.assertEqual(found["parent"]["id"], parent["id"])
        self.assertEqual(self.issue("ENG-7")["children"]["nodes"],
                         [{"identifier": f"ENG-{n}"} for n in (8, 9, 10)])

    def test_sub_issues_share_the_project_but_not_the_labels(self):
        project = {"id": "p-plat", "name": "Platform", "teams": [ENG]}
        self.promote(target=lambda client, view: (project, ["l-api"]))
        parent, *children = self.creates()
        self.assertEqual(parent["projectId"], "p-plat")
        self.assertEqual(parent["labelIds"], ["l-api", "l-home-eng"])
        for child in children:
            self.assertEqual(child["projectId"], "p-plat")
            self.assertNotIn("labelIds", child)

    def test_a_card_without_extras_sends_only_the_title_and_state(self):
        self.write_board("## Todo\n\n- [ ] Just this ^t0aa\n")
        self.promote("t0aa")
        (inputs,) = self.creates()
        self.assertEqual(inputs, {"id": inputs["id"], "teamId": "t-eng", "title": "Just this",
                                  "stateId": "ws-ENG-Todo", "assigneeId": "u1"})


class LabelTests(PromoteCase):
    def labels_of(self, tags):
        self.write_board(f"## Todo\n\n- [ ] Card {tags} ^t0aa\n")
        self.promote("t0aa")
        return self.creates()[0].get("labelIds")

    def test_team_label_wins_over_workspace_label(self):
        self.assertEqual(self.labels_of("#home"), ["l-home-eng"])

    def test_workspace_label_is_used_when_the_team_has_none(self):
        self.assertEqual(self.labels_of("#repo:api"), ["l-api"])

    def test_label_names_ignore_case(self):
        self.assertEqual(self.labels_of("#BUG"), ["l-bug"])

    def test_a_label_of_another_team_does_not_count(self):
        self.assertIsNone(self.labels_of("#Unknown-tag"))
        self.assertEqual(self.creates()[0]["description"], "#Unknown-tag")

    def test_labels_keep_tag_order_without_duplicates(self):
        self.assertEqual(self.labels_of("#repo:web #bug #repo:web"), ["l-web", "l-bug"])

    def test_the_type_becomes_the_label_of_that_name(self):
        self.assertEqual(self.labels_of("#type/bug #repo:web"), ["l-bug", "l-web"])
        self.assertIsNone(self.creates()[0].get("description"))

    def test_a_type_without_a_label_goes_into_the_description(self):
        self.assertIsNone(self.labels_of("#type/spike"))
        self.assertEqual(self.creates()[0]["description"], "#type/spike")

    def test_the_team_decides_which_label_wins(self):
        self.write_board("## Todo\n\n- [ ] Card #bug ^t0aa\n")
        self.promote("t0aa", team="OPS")
        self.assertEqual(self.creates()[0]["labelIds"], ["l-bug-ops"])


class DescriptionTests(PromoteCase):
    def setUp(self):
        super().setUp()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / "Tile notes.md").write_text("Tile notes body\nSecond line\n", encoding="utf-8")

    def test_linked_note_becomes_the_description(self):
        config = Config(todos_dir=self.todos, vault_root=self.vault)
        self.promoter(config).promote("t6n2", team="ENG")
        (inputs,) = self.creates()
        self.assertEqual(inputs["title"], "Read [[Tile notes]] and [[Missing note]]")
        self.assertEqual(inputs["description"], "\n\n".join([
            "Tile notes body\nSecond line", "See also [[Grout guide]]", "[[Missing note]]"]))

    def test_several_notes_get_a_heading_each(self):
        (self.vault / "Missing note.md").write_text("Found now\n", encoding="utf-8")
        config = Config(todos_dir=self.todos, vault_root=self.vault)
        self.promoter(config).promote("t6n2", team="ENG")
        self.assertEqual(self.creates()[0]["description"], "\n\n".join([
            "## Tile notes\nTile notes body\nSecond line", "## Missing note\nFound now",
            "See also [[Grout guide]]"]))

    def test_without_a_vault_the_links_stay_text(self):
        self.promote("t6n2")
        self.assertEqual(self.creates()[0]["description"],
                         "See also [[Grout guide]]\n\n[[Tile notes]]\n[[Missing note]]")

    def test_note_outside_the_vault_is_not_read(self):
        outside = self.root / "outside.md"
        outside.write_text("secret", encoding="utf-8")
        (self.vault / "Tile notes.md").unlink()
        (self.vault / "Tile notes.md").symlink_to(outside)
        config = Config(todos_dir=self.todos, vault_root=self.vault)
        self.promoter(config).promote("t6n2", team="ENG")
        self.assertNotIn("secret", self.creates()[0]["description"])


class StateTests(PromoteCase):
    def state_ids(self):
        return [c["stateId"] for c in self.creates()]

    def test_state_follows_the_column_of_the_todo(self):
        self.write_board("## Backlog\n\n- [ ] A ^t0aa\n\n## Todo\n\n- [ ] B ^t0bb\n\n"
                         "## Review\n\n- [ ] C ^t0cc\n")
        for ref in ("t0aa", "t0bb", "t0cc"):
            self.promote(ref)
        self.assertEqual(self.state_ids(), ["ws-ENG-Backlog", "ws-ENG-Todo", "ws-ENG-In Review"])

    def test_a_todo_in_the_done_column_becomes_a_completed_issue(self):
        result = self.promote("t9d4")
        self.assertEqual(self.state_ids(), ["ws-ENG-Done", "ws-ENG-Done"])
        self.assertEqual(result.issue["state"]["type"], "completed")

    def test_the_done_column_can_have_another_name(self):
        self.write_board("## Todo\n\n- [ ] A ^t0aa\n\n## Shipped\n\n**Complete**\n- [x] B ^t0bb\n")
        self.promote("t0bb")
        self.assertEqual(self.state_ids(), ["ws-ENG-Done"])

    def test_checked_sub_issue_is_completed_and_the_others_share_the_parent_state(self):
        self.promote("t5r1")
        self.assertEqual(self.state_ids(),
                         ["ws-ENG-Backlog", "ws-ENG-Backlog", "ws-ENG-Done", "ws-ENG-Backlog"])

    def test_open_sub_issue_of_a_completed_parent_starts_in_the_first_column(self):
        self.write_board("## Backlog\n\n## Done\n\n**Complete**\n- [x] A ^t0aa\n"
                         "\t- [ ] Open item\n\t- [x] Closed item\n")
        self.promote("t0aa")
        self.assertEqual(self.state_ids(), ["ws-ENG-Done", "ws-ENG-Backlog", "ws-ENG-Done"])

    def test_ambiguous_column_fails_before_any_write_and_state_answers(self):
        linear_data.add_state(self.fixtures, "ENG", "Coding", "started")
        self.write_board("## In progress\n\n- [ ] A ^t0aa\n\t- [ ] Sub\n")
        with self.assertRaises(AmbiguousStateError) as caught:
            self.promote("t0aa")
        self.assertEqual(caught.exception.candidates, ["Doing", "Coding"])
        self.assertEqual(self.requests("IssueCreate"), [])
        self.assertEqual(self.records(), {})
        self.promote("t0aa", state_name="coding")
        self.assertEqual(self.state_ids(), ["ws-ENG-Coding", "ws-ENG-Coding"])

    def test_a_column_without_a_state_fails_before_any_write(self):
        self.write_board("## Someday\n\n- [ ] A ^t0aa\n\n## Done\n\n**Complete**\n")
        with self.assertRaises(HerdboardError) as caught:
            self.promote("t0aa")
        self.assertEqual(caught.exception.code, "no_state")
        self.assertEqual(self.requests("IssueCreate"), [])
        self.assertNotIn("→ ENG", self.text())


class CompletedStateTests(PromoteCase):
    def setUp(self):
        super().setUp()
        # "Doing" (started) also maps to the Done column.
        linear = LinearConfig(state_names={"In Review": "Review", "Doing": "Done"})
        self.config = Config(todos_dir=self.todos, linear=linear)

    def promote(self, ref="t5r1", **kwargs):
        kwargs.setdefault("team", "ENG")
        return self.promoter(self.config).promote(ref, **kwargs)

    def state_ids(self):
        return [c["stateId"] for c in self.creates()]

    def test_a_done_todo_uses_only_a_completed_state(self):
        self.promote("t9d4")
        self.assertEqual(self.state_ids(), ["ws-ENG-Done", "ws-ENG-Done"])

    def test_a_stored_choice_of_a_started_state_is_not_used_for_a_done_todo(self):
        folder = self.promoter(self.config).writer.source.account_dir()
        StateChoices(folder).set("t-eng", "Done", "ws-ENG-Doing")
        self.promote("t9d4")
        self.assertEqual(self.state_ids(), ["ws-ENG-Done", "ws-ENG-Done"])

    def test_an_incompatible_state_option_fails_before_the_record_is_written(self):
        with self.assertRaises(AmbiguousStateError):
            self.promote("t9d4", state_name="Doing")
        self.assertEqual(self.requests("IssueCreate"), [])
        self.assertEqual(self.all_records(), {})

    def test_a_checked_sub_issue_uses_only_a_completed_state(self):
        folder = self.promoter(self.config).writer.source.account_dir()
        StateChoices(folder).set("t-eng", "Done", "ws-ENG-Doing")
        self.promote("t5r1")
        self.assertEqual(self.state_ids(),
                         ["ws-ENG-Backlog", "ws-ENG-Backlog", "ws-ENG-Done", "ws-ENG-Backlog"])


class PlanOptionTests(PromoteCase):
    def test_different_options_warn_and_the_recorded_plan_stays(self):
        project = {"id": "p-plat", "name": "Platform", "teams": [ENG]}
        self.fake.mutation_failures = [None, "http_500"]
        with self.assertRaises(LinearWriteUnknownError):
            self.promote(target=lambda client, view: (project, []), project="Platform")
        result = self.promote(team="OPS", project="Mobile", state_name="Doing")
        self.assertEqual(len(result.warnings), 3)
        self.assertIn("OPS", result.warnings[0])
        self.assertIn("ENG", result.warnings[0])
        self.assertIn("Mobile", result.warnings[1])
        self.assertIn("Platform", result.warnings[1])
        self.assertIn("Doing", result.warnings[2])
        self.assertIn("Backlog", result.warnings[2])
        for sent in self.creates()[2:]:
            self.assertEqual((sent["teamId"], sent["projectId"]), ("t-eng", "p-plat"))

    def test_an_empty_project_means_no_project_and_warns_against_a_recorded_one(self):
        project = {"id": "p-plat", "name": "Platform", "teams": [ENG]}
        self.fake.mutation_failures = [None, "http_500"]
        with self.assertRaises(LinearWriteUnknownError):
            self.promote(target=lambda client, view: (project, []), project="Platform")
        (warning,) = self.promote(project="").warnings
        self.assertEqual(warning, "--project none is ignored: the recorded plan uses project Platform")

    def test_an_empty_project_matches_a_plan_without_a_project(self):
        self.fake.mutation_failures = [None, "http_500"]
        with self.assertRaises(LinearWriteUnknownError):
            self.promote()
        self.assertEqual(self.promote(project="").warnings, [])

    def test_the_same_options_do_not_warn(self):
        self.fake.mutation_failures = [None, "http_500"]
        with self.assertRaises(LinearWriteUnknownError):
            self.promote()
        self.assertEqual(self.promote(team="eng").warnings, [])

    def test_a_first_run_has_no_warnings(self):
        self.assertEqual(self.promote(project="Platform").warnings, [])


class RecordTests(PromoteCase):
    def test_the_record_holds_every_uuid_before_the_first_create(self):
        seen = []
        real = LinearClient.create_issue

        def spy(client, **kwargs):
            record = self.records()["t5r1"]
            seen.append(([i["id"] for i in record["issues"]],
                         [i["identifier"] for i in record["issues"]]))
            return real(client, **kwargs)

        with mock.patch.object(LinearClient, "create_issue", autospec=True, side_effect=spy):
            self.promote()
        ids = [i["id"] for i in self.fixtures["issues"][6:]]
        self.assertEqual(len(seen), 4)
        self.assertEqual([s[0] for s in seen], [ids] * 4)
        self.assertEqual(seen[0][1], [None] * 4)
        # Each confirmed create is recorded before the next one is sent.
        self.assertEqual(seen[3][1], ["ENG-7", "ENG-8", "ENG-9", None])

    def test_equal_card_ids_in_two_stores_keep_separate_records(self):
        self.unfinished()
        other = self.root / "other"
        other.mkdir()
        (other / "inbox.md").write_text(self.text().replace(
            "Renovate the bathroom", "Completely different").replace(" → ENG-7", ""))
        config = Config(todos_dir=other)
        result = self.promoter(config).promote("t5r1", team="ENG")
        self.assertEqual(len(self.issues_titled("Completely different")), 1)
        self.assertEqual(len(self.issues_titled("Renovate the bathroom")), 1)
        self.assertIn("→ " + result.issue["identifier"] + " ^t5r1",
                      (other / "inbox.md").read_text())
        self.assertNotIn("Completely different", self.text())
        self.assertIn("t5r1", self.records())
        self.assertEqual(self.records(other), {})

    def test_the_record_is_removed_after_the_todo_update(self):
        self.promote()
        self.assertEqual(self.records(), {})

    def test_the_record_is_account_scoped_and_survives_a_cache_drop(self):
        self.fake.mutation_failure = "http_500"
        with self.assertRaises(LinearWriteUnknownError):
            self.promote()
        self.assertTrue(self.record_file().exists())
        promoter = self.promoter()
        promoter.writer.source.drop_cache()
        self.assertTrue(self.record_file().exists())

    def test_a_record_that_cannot_be_written_stops_before_any_create(self):
        with mock.patch("os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(HerdboardError) as caught:
                self.promote()
        self.assertEqual(caught.exception.code, "io")
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_a_second_promote_at_the_same_time_is_a_conflict(self):
        import fcntl
        promoter = self.promoter()
        folder = promoter.writer.source.account_dir()
        folder.mkdir(parents=True, exist_ok=True)
        with open(folder / "promotions.lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            with self.assertRaises(ConflictError):
                self.promote()
        self.assertEqual(self.requests("IssueCreate"), [])


class UnknownResultTests(PromoteCase):
    def test_unknown_parent_that_is_not_found_stops_and_keeps_the_record(self):
        self.fake.mutation_failure = "http_500"
        with self.assertRaises(LinearWriteUnknownError) as caught:
            self.promote()
        record = self.records()["t5r1"]
        self.assertEqual(caught.exception.issue_id, record["issues"][0]["id"])
        self.assertIn("promote t5r1", str(caught.exception))
        self.assertEqual(len(self.requests("IssueCreate")), 1)
        self.assertEqual([i["identifier"] for i in record["issues"]], [None] * 4)
        self.assertEqual(len(self.fixtures["issues"]), 6)
        self.assertNotIn("→ ENG", self.text())

    def test_unknown_parent_that_was_created_is_found_and_the_run_continues(self):
        self.slow(["timeout_after"])
        result = self.promote()
        self.assertEqual(result.issue["identifier"], "ENG-7")
        self.assertEqual(len(self.requests("IssueCreate")), 4)
        self.assertEqual([i["identifier"] for i in result.sub_issues], ["ENG-8", "ENG-9", "ENG-10"])
        self.assertEqual(self.records(), {})
        self.assertIn("→ ENG-7 ^t5r1", self.text())

    def test_unknown_sub_issue_that_was_created_is_found_and_the_run_continues(self):
        self.slow([None, None, "timeout_after"])
        result = self.promote()
        self.assertEqual(len(self.requests("IssueCreate")), 4)
        self.assertEqual(len(result.sub_issues), 3)
        self.assertEqual(len(self.issues_titled("Measure the room")), 1)

    def test_unknown_sub_issue_that_is_not_found_stops_and_a_rerun_creates_the_rest(self):
        self.fake.mutation_failures = [None, "http_500"]
        with self.assertRaises(LinearWriteUnknownError) as caught:
            self.promote()
        first = self.creates()
        self.assertEqual(len(first), 2)
        record = self.records()["t5r1"]
        self.assertEqual(caught.exception.issue_id, record["issues"][1]["id"])
        self.assertEqual([i["identifier"] for i in record["issues"]], ["ENG-7", None, None, None])
        self.assertNotIn("→ ENG", self.text())

        result = self.promote()
        later = self.creates()[2:]
        # The parent exists, so only the three sub-issues are sent, each one once.
        self.assertEqual([c["title"] for c in later],
                         ["Choose tiles", "Measure the room", "Book the plumber"])
        self.assertEqual(later[0]["id"], first[1]["id"])
        self.assertNotIn(first[0]["id"], [c["id"] for c in later])
        self.assertEqual([i["identifier"] for i in result.sub_issues], ["ENG-8", "ENG-9", "ENG-10"])
        self.assertEqual(self.records(), {})
        self.assertIn("→ ENG-7 ^t5r1", self.text())

    def test_rerun_after_a_rejected_sub_issue_creates_only_the_missing_ones(self):
        self.fake.mutation_failures = [None, None, "graphql_error"]
        with self.assertRaises(LinearWriteRejectedError) as caught:
            self.promote()
        self.assertIn("promote t5r1", str(caught.exception))
        first = self.creates()
        self.assertEqual(len(first), 3)
        self.assertEqual([i["identifier"] for i in self.records()["t5r1"]["issues"]],
                         ["ENG-7", "ENG-8", None, None])

        self.promote()
        later = self.creates()[3:]
        self.assertEqual([c["title"] for c in later], ["Measure the room", "Book the plumber"])
        sent = [c["id"] for c in self.creates()]
        # Only the rejected create is sent twice. Linear never had that one.
        self.assertEqual(sorted(i for i in set(sent) if sent.count(i) > 1), [first[2]["id"]])
        self.assertEqual([len(self.issues_titled(t)) for t in
                          ("Renovate the bathroom", "Choose tiles", "Measure the room")], [1, 1, 1])

    def test_rerun_looks_up_every_recorded_uuid(self):
        self.fake.mutation_failures = [None, None, "graphql_error"]
        with self.assertRaises(LinearWriteRejectedError):
            self.promote()
        recorded = [i["id"] for i in self.records()["t5r1"]["issues"]]
        before = len(self.requests("IssueForWrite"))
        self.promote()
        looked_up = [r["variables"]["id"] for r in self.requests("IssueForWrite")[before:]]
        self.assertEqual(looked_up, recorded)

    def test_rerun_follows_the_record_when_the_todo_changed(self):
        self.fake.mutation_failures = [None, "http_500"]
        with self.assertRaises(LinearWriteUnknownError):
            self.promote()
        self.inbox.write_text(self.text().replace("\t- [ ] Book the plumber",
                                                  "\t- [ ] Book the plumber\n\t- [ ] Extra"))
        self.promote()
        titles = [i["title"] for i in self.fixtures["issues"][6:]]
        self.assertEqual(titles, ["Renovate the bathroom", "Choose tiles", "Measure the room",
                                  "Book the plumber"])
        self.assertIn("→ ENG-7 ^t5r1", self.text())


class TodoUpdateTests(PromoteCase):
    def test_the_todo_moves_to_done_with_the_identifier_before_the_block_id(self):
        result = self.promote()
        lines = self.text().split("\n")
        done = lines.index("## Done")
        card = lines.index("- [x] Renovate the bathroom #home #Unknown-tag 📅 2026-11-02 → ENG-7 ^t5r1")
        self.assertGreater(card, done)
        # Its indented lines move with it, and other cards stay where they were.
        self.assertEqual(lines[card + 1:card + 3],
                         ["\tBudget is tight, ask for two quotes", "\t- [ ] Choose tiles"])
        self.assertIn("- [x] Paint the hallway #home ^t9d4", lines)
        self.assertEqual((result.todo.column, result.todo.checked), ("Done", True))
        self.assertNotIn("→ ENG-7", "\n".join(lines[:done]))

    def test_a_todo_already_in_done_keeps_its_place(self):
        before = self.text().split("\n")
        self.promote("t9d4")
        lines = self.text().split("\n")
        index = before.index("- [x] Paint the hallway #home ^t9d4")
        self.assertEqual(lines[index], "- [x] Paint the hallway #home → ENG-7 ^t9d4")
        self.assertEqual(len(lines), len(before))

    def test_a_todo_without_an_id_gets_one_first(self):
        self.write_board("## Todo\n\n- [ ] No id yet\n\n## Done\n\n**Complete**\n")
        result = self.promoter().promote("inbox:1", team="ENG")
        self.assertRegex(result.todo.ref, r"^t[0-9a-z]{3}$")
        self.assertIn(f"- [x] No id yet → ENG-7 ^{result.todo.ref}", self.text())
        self.assertEqual(len(self.records()), 0)

    def test_crlf_line_endings_are_kept(self):
        self.inbox.write_bytes(self.text().replace("\n", "\r\n").encode())
        self.promote()
        data = self.inbox.read_bytes()
        self.assertEqual(data.count(b"\n"), data.count(b"\r\n"))
        self.assertIn("→ ENG-7 ^t5r1\r\n".encode(), data)

    def test_a_board_without_a_done_column_fails_before_any_write(self):
        self.write_board("## Todo\n\n- [ ] A ^t0aa\n", done=False)
        with self.assertRaises(HerdboardError) as caught:
            self.promote("t0aa")
        self.assertIn("inbox", str(caught.exception))
        self.assertIn("done column", str(caught.exception))
        self.assertEqual(self.requests("IssueCreate"), [])
        self.assertEqual(self.records(), {})

    def test_unknown_todo(self):
        with self.assertRaises(NotFoundError):
            self.promote("t0zz")
        self.assertEqual(self.requests("IssueCreate"), [])


class ConflictTests(PromoteCase):
    def edit_before_the_update(self):
        real = Store.finish_promotion

        def edited(store, snapshot, identifier):
            path = snapshot.located.path
            path.write_text(path.read_text().replace("Choose tiles", "Choose floor tiles"))
            return real(store, snapshot, identifier)

        return mock.patch.object(Store, "finish_promotion", edited)

    def test_a_todo_that_changed_keeps_the_record_and_reports_a_conflict(self):
        with self.edit_before_the_update():
            with self.assertRaises(ConflictError) as caught:
                self.promote()
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertIn("ENG-7", str(caught.exception))
        self.assertIn("promote t5r1", str(caught.exception))
        self.assertEqual(len(self.requests("IssueCreate")), 4)
        record = self.records()["t5r1"]
        self.assertEqual([i["identifier"] for i in record["issues"]],
                         ["ENG-7", "ENG-8", "ENG-9", "ENG-10"])
        self.assertNotIn("→ ENG-7", self.text())

    def test_a_rerun_finishes_the_todo_without_creating_anything(self):
        with self.edit_before_the_update():
            with self.assertRaises(ConflictError):
                self.promote()
        result = self.promote()
        self.assertEqual(len(self.requests("IssueCreate")), 4)
        self.assertEqual(result.issue["identifier"], "ENG-7")
        self.assertEqual([i["identifier"] for i in result.sub_issues], ["ENG-8", "ENG-9", "ENG-10"])
        self.assertIn("→ ENG-7 ^t5r1", self.text())
        self.assertIn("Choose floor tiles", self.text())
        self.assertEqual(self.records(), {})

    def test_a_todo_that_moved_is_a_conflict_too(self):
        self.write_board("## Backlog\n\n## Todo\n\n- [ ] A ^t0aa\n")
        real = Store.finish_promotion

        def moved(store, snapshot, identifier):
            self.write_board("## Backlog\n\n- [ ] A ^t0aa\n\n## Todo\n")
            return real(store, snapshot, identifier)

        with mock.patch.object(Store, "finish_promotion", moved):
            with self.assertRaises(ConflictError):
                self.promote("t0aa")
        self.assertIn("t0aa", self.records())


class RefusalTests(PromoteCase):
    def test_a_promoted_todo_is_refused(self):
        self.promote()
        self.fake.requests.clear()
        with self.assertRaises(AlreadyPromotedError) as caught:
            self.promote()
        self.assertEqual(caught.exception.code, "already_promoted")
        self.assertIn("ENG-7", str(caught.exception))
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_a_promoted_todo_by_hand_is_refused_without_a_request(self):
        self.write_board("## Done\n\n**Complete**\n- [x] Old → ENG-3 ^t0aa\n")
        with self.assertRaises(AlreadyPromotedError):
            self.promote("t0aa")
        self.assertEqual(self.requests("IssueCreate"), [])

    def test_a_marker_keeps_an_unfinished_record_and_names_what_is_left(self):
        self.unfinished()
        record = self.records()["t5r1"]
        self.mark()
        with self.assertRaises(AlreadyPromotedError) as caught:
            self.promote()
        self.assertEqual(caught.exception.code, "already_promoted")
        message = str(caught.exception)
        self.assertIn("recorded parent ENG-7", message)
        self.assertIn("3 of 4 issues are not confirmed", message)
        self.assertIn("remove the marker", message)
        self.assertEqual(self.records()["t5r1"], record)

    def test_a_rerun_without_the_marker_finishes_without_a_second_parent(self):
        self.unfinished()
        self.mark()
        with self.assertRaises(AlreadyPromotedError):
            self.promote()
        self.inbox.write_text(self.text().replace(" → ENG-7", ""))
        result = self.promote()
        self.assertEqual(len(self.issues_titled("Renovate the bathroom")), 1)
        self.assertEqual(result.issue["identifier"], "ENG-7")
        self.assertEqual(self.records(), {})

    def test_a_marker_removes_a_record_whose_issues_are_all_confirmed(self):
        def failing(store, snapshot, identifier):
            raise ConflictError("changed")

        with mock.patch.object(Store, "finish_promotion", failing):
            with self.assertRaises(ConflictError):
                self.promote()
        self.mark()
        with self.assertRaises(AlreadyPromotedError):
            self.promote()
        self.assertEqual(self.records(), {})

    def test_a_marker_added_after_the_first_read_stops_before_any_create(self):
        real = Store.todo_snapshot

        def marked(store, ref):
            self.mark("ENG-1")
            return real(store, ref)

        with mock.patch.object(Store, "todo_snapshot", marked):
            with self.assertRaises(AlreadyPromotedError):
                self.promote()
        self.assertEqual(self.requests("IssueCreate"), [])
        self.assertEqual(self.records(), {})
        self.assertEqual(self.text().count("→ ENG-"), 1)

    def test_a_linear_card_is_not_a_todo(self):
        for ref in ("ENG-1", "eng-1"):
            with self.assertRaises(NotATodoError) as caught:
                self.promote(ref)
            self.assertEqual(caught.exception.code, "not_a_todo")
        self.assertEqual(self.fake.requests, [])

    def test_offline_is_refused_without_a_request_or_a_change(self):
        before = self.inbox.read_bytes()
        with self.assertRaises(LinearOfflineError) as caught:
            self.promoter(offline=True).promote("t5r1", team="ENG")
        self.assertEqual(caught.exception.code, "linear_offline")
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.inbox.read_bytes(), before)

    def test_an_unreachable_linear_is_refused(self):
        self.endpoint = "http://127.0.0.1:1/graphql"
        with self.assertRaises(LinearOfflineError):
            self.promote()
        self.assertEqual(self.fake.requests, [])

    def test_the_team_must_be_known(self):
        with self.assertRaises(HerdboardError) as caught:
            self.promote(team=None)
        self.assertEqual(caught.exception.code, "team_required")
        self.assertEqual(self.requests("IssueCreate"), [])


class CacheTests(PromoteCase):
    def test_promote_drops_the_cached_queries(self):
        promoter = self.promoter()
        source = promoter.writer.source
        source.my_issues()
        self.assertTrue((source.account_dir() / "mine.json").exists())
        promoter.promote("t5r1", team="ENG")
        self.assertFalse((source.account_dir() / "mine.json").exists())


class StoreTests(PromoteCase):
    def test_snapshot_reports_the_column_and_done_column(self):
        store = Store(self.config)
        self.assertFalse(store.todo_snapshot("t5r1").done)
        self.assertTrue(store.todo_snapshot("t9d4").done)

    def test_finish_promotion_refuses_a_changed_card(self):
        store = Store(self.config)
        snapshot = store.todo_snapshot("t5r1")
        self.inbox.write_text(self.text().replace("Choose tiles", "Choose rugs"))
        with self.assertRaises(ConflictError):
            store.finish_promotion(snapshot, "ENG-1")

    def test_finish_promotion_refuses_a_card_changed_in_the_lock_window(self):
        store = Store(self.config)
        snapshot = store.todo_snapshot("t5r1")
        view = store.finish_promotion(snapshot, "ENG-1")
        self.assertEqual((view.column, view.checked), ("Done", True))
        with self.assertRaises(ConflictError):
            store.finish_promotion(snapshot, "ENG-1")


if __name__ == "__main__":
    unittest.main()


class SubCardTests(PromoteCase):
    def setUp(self):
        super().setUp()
        self.write_board(
            "## Todo\n\n"
            "- [ ] Billing v2 #home ^tp00\n\t- [ ] Checklist item\n"
            "- [ ] Add invoice table [[#^tp00]] 📅 2026-11-05 ^tc01\n\tUse the new API\n\t- [ ] Write the migration\n"
            "- [ ] Unrelated ^tu02\n\n"
            "## Done\n\n**Complete**\n"
            "- [x] Add tax field [[#^tp00]] ^tc02\n"
        )

    def test_sub_cards_become_sub_issues_of_the_parent(self):
        result = self.promote("tp00")
        parent, checklist, table, tax = self.creates()
        self.assertEqual(parent["title"], "Billing v2")
        self.assertEqual(checklist["title"], "Checklist item")
        self.assertEqual(table["title"], "Add invoice table")
        self.assertEqual(table["dueDate"], "2026-11-05")
        self.assertEqual(table["description"], "Use the new API\n\n- [ ] Write the migration")
        self.assertEqual(tax["title"], "Add tax field")
        self.assertEqual({c["parentId"] for c in (checklist, table, tax)}, {parent["id"]})
        self.assertEqual(tax["stateId"], "ws-ENG-Done")
        self.assertEqual(len(result.sub_issues), 3)
        text = self.text()
        self.assertIn("- [x] Add invoice table [[#^tp00]] 📅 2026-11-05 → ENG-9 ^tc01", text)
        self.assertIn("- [x] Add tax field [[#^tp00]] → ENG-10 ^tc02", text)
        self.assertIn("- [ ] Unrelated ^tu02", text)
        done = text.index("## Done")
        self.assertGreater(text.index("Add invoice table"), done)

    def test_a_sub_card_alone_is_refused(self):
        with self.assertRaises(HerdboardError) as raised:
            self.promote("tc01")
        self.assertEqual(raised.exception.code, "parent_not_promoted")
        self.assertEqual(self.creates(), [])

    def test_a_rerun_finishes_the_sub_cards_without_new_issues(self):
        self.fake.mutation_failures = [None, None, "http_500"]
        with self.assertRaises(LinearWriteUnknownError):
            self.promote("tp00")
        self.assertNotIn("→", self.text())
        self.promote("tp00")
        titles = [c["title"] for c in self.creates()]
        self.assertEqual(sorted(set(titles)), sorted(["Billing v2", "Checklist item", "Add invoice table",
                                                      "Add tax field"]))
        self.assertEqual(len(self.issues_titled("Add invoice table")), 1)
        self.assertEqual(self.text().count("→ ENG-"), 3)
