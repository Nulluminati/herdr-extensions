import fcntl
import os
import re
import threading
import time
import unittest
from unittest import mock

from herdboard import kanban_md
from herdboard.config import Config
from herdboard.identity import RepoIdentity
from herdboard.model import (
    ConflictError,
    DuplicateIdError,
    FormatError,
    HerdboardError,
    NotFoundError,
)
from herdboard.store import BoardSnapshot, Store
from support.herdboard_store import BASIC, StoreTestCase, board_text, col

ID = re.compile(r" \^(t[0-9a-z]{3})$")


class BoardsTest(StoreTestCase):
    def test_paths_and_names(self):
        self.put("b", BASIC)
        self.put("a", BASIC)
        self.assertEqual([p.stem for p in self.store.board_paths()], ["a", "b"])
        inbox = self.put("inbox", BASIC)
        self.assertEqual(self.store.board_paths()[0], inbox)
        self.assertEqual(self.store.board_name(inbox), "inbox")
        self.assertEqual(self.store.board_name(self.todos / "projects/a.md"), "a")

    def test_resolve_board(self):
        inbox = self.put("inbox", BASIC)
        proj = self.put("acme-api", BASIC)
        self.assertEqual(self.store.resolve_board("inbox"), inbox)
        self.assertEqual(self.store.resolve_board("acme-api"), proj)
        with self.assertRaises(NotFoundError):
            self.store.resolve_board("nope")

    def test_ensure_inbox(self):
        path = self.store.ensure_inbox()
        expected = kanban_md.new_board(self.config.columns, "Done")
        self.assertEqual(path.read_text(), expected)
        path.write_text("changed")
        self.store.ensure_inbox()
        self.assertEqual(path.read_text(), "changed")

    def test_init_board(self):
        identity = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))
        path = self.store.init_board(identity)
        self.assertEqual(path, self.todos / "projects/acme-api.md")
        expected = kanban_md.new_board(self.config.columns, "Done", [("repo", "github.com/acme/api")])
        self.assertEqual(path.read_text(), expected)
        self.assertEqual(self.store.board_for_repo(identity), path)
        with self.assertRaises(HerdboardError):
            self.store.init_board(identity)

    def test_init_board_creates_projects_dir(self):
        self.config.todos_dir = self.todos / "fresh"
        store = Store(self.config)
        identity = RepoIdentity("/x/api", None, ("api",))
        self.assertTrue(store.init_board(identity).is_file())

    def test_init_board_without_done_column(self):
        self.config.columns = ["A", "B"]
        identity = RepoIdentity("/x/api", None, ("api",))
        path = Store(self.config).init_board(identity)
        self.assertEqual(path.read_text(), kanban_md.new_board(["A", "B"], None, [("repo", "/x/api")]))


class ReadTest(StoreTestCase):
    def test_list_cards(self):
        self.put("p", BASIC)
        cards = self.store.list_cards()
        self.assertEqual(
            [(c.ref, c.column, c.title, c.checked) for c in cards],
            [
                ("p:1", "Todo", "one", False),
                ("p:2", "Todo", "two #tag 📅 2026-01-02", False),
                ("tabc", "Doing", "three", False),
                ("p:4", "Done", "four", True),
            ],
        )
        two = cards[1]
        self.assertEqual((two.tags, two.due, two.body, two.board), (["tag"], "2026-01-02", ["\tbody line"], "p"))
        self.assertEqual(two.path, self.todos / "projects/p.md")

    def test_list_given_paths(self):
        a = self.put("a", BASIC)
        self.put("b", BASIC)
        self.assertEqual({c.board for c in self.store.list_cards([a])}, {"a"})

    def test_show_by_id_and_temp_ref(self):
        self.put("p", BASIC)
        self.assertEqual(self.store.show("tabc").title, "three")
        self.assertEqual(self.store.show("p:2").title, "two #tag 📅 2026-01-02")
        self.assertEqual(self.store.show("tabc").ref, "tabc")

    def test_unknown_refs(self):
        self.put("p", BASIC)
        for ref in ("tzzz", "p:99", "p:0", "nope:1"):
            with self.subTest(ref=ref), self.assertRaises(NotFoundError):
                self.store.show(ref)

    def test_archive_not_listed_or_addressable(self):
        self.put("p", BASIC)
        self.assertNotIn("old", [c.title for c in self.store.list_cards()])
        with self.assertRaises(NotFoundError):
            self.store.show("p:5")

    def test_duplicate_id_across_boards(self):
        self.put("a", BASIC)
        self.put("b", BASIC)
        with self.assertRaises(DuplicateIdError) as caught:
            self.store.show("tabc")
        message = str(caught.exception)
        self.assertIn("a.md", message)
        self.assertIn("b.md", message)

    def test_duplicate_id_in_one_board(self):
        self.put("a", board_text(col("Todo", "- [ ] x ^tdup", "- [ ] y ^tdup")))
        with self.assertRaises(DuplicateIdError):
            self.store.show("tdup")


class IdsTest(StoreTestCase):
    def test_first_write_assigns_ids_to_that_board_only(self):
        a = self.put("a", BASIC.replace(" ^tabc", ""))
        b = self.put("b", BASIC.replace(" ^tabc", ""))
        before_b = b.read_text()
        self.store.done("a:1")
        cards = self.store.list_cards([a])
        self.assertTrue(all(ID.search(f"x ^{c.ref}") for c in cards))
        self.assertEqual(b.read_text(), before_b)
        text = a.read_text()
        self.assertEqual(len(re.findall(r" \^t[0-9a-z]{3}$", text, re.M)), 4)
        self.assertNotIn("old ^", text)

    def test_ids_unique_across_boards(self):
        for name in "abcdef":
            self.put(name, board_text(col("Todo", *[f"- [ ] c{i}" for i in range(20)])))
            self.store.add(self.todos / f"projects/{name}.md", "new")
        refs = [c.ref for c in self.store.list_cards()]
        self.assertEqual(len(refs), 126)
        self.assertEqual(len(set(refs)), 126)

    def test_collision_retries(self):
        draws = iter("aaabbb")
        with mock.patch("herdboard.store.secrets.choice", lambda alphabet: next(draws)):
            self.assertEqual(self.store._new_id({"taaa"}), "tbbb")

    def test_an_id_with_a_log_or_attachments_is_not_reused(self):
        # A deleted card keeps its log and attachments; a new card must not get them.
        (self.todos / ".herdboard/log").mkdir(parents=True)
        (self.todos / ".herdboard/log/taaa.md").write_text("- old\n")
        (self.todos / ".herdboard/attachments/tbbb").mkdir(parents=True)
        draws = iter("aaabbbccc")
        with mock.patch("herdboard.store.secrets.choice", lambda alphabet: next(draws)):
            self.assertEqual(self.store._new_id(set()), "tccc")


SHIPPED = board_text(
    col("Todo", "- [ ] a ^taaa", "- [ ] b ^tbbb"),
    col("Shipped", "**Complete**"),
    col("Done", "**Complete**"),
)


class AddMoveDoneTest(StoreTestCase):
    def test_add_default_column_bottom(self):
        path = self.put("p", BASIC)
        view = self.store.add(path, "fresh")
        self.assertEqual((view.column, view.title, view.checked), ("Todo", "fresh", False))
        self.assertRegex(view.ref, r"^t[0-9a-z]{3}$")
        titles = [c.title for c in self.store.list_cards() if c.column == "Todo"]
        self.assertEqual(titles[-1], "fresh")

    def test_add_top_and_named_column(self):
        path = self.put("p", BASIC)
        self.store.add(path, "top", column="todo", position="top")
        self.store.add(path, "doing", column="Doing")
        todo = [c.title for c in self.store.list_cards() if c.column == "Todo"]
        self.assertEqual(todo[0], "top")
        doing = [c.title for c in self.store.list_cards() if c.column == "Doing"]
        self.assertEqual(doing, ["three", "doing"])

    def test_add_to_empty_column(self):
        path = self.store.ensure_inbox()
        self.store.add(path, "first", column="Review")
        self.assertEqual([(c.column, c.title) for c in self.store.list_cards()], [("Review", "first")])

    def test_add_rejects_bad_text_and_column(self):
        path = self.put("p", BASIC)
        for text in ("", "   ", "a\nb"):
            with self.subTest(text=text), self.assertRaises(HerdboardError):
                self.store.add(path, text)
        with self.assertRaises(NotFoundError):
            self.store.add(path, "x", column="Nope")

    def test_add_with_details(self):
        path = self.put("p", BASIC)
        card = self.store.add(path, "new card #tag", "Doing", body=["first", "", "  second", ""])
        self.assertEqual(card.body, ["\tfirst", "\t", "\t  second"])
        self.assertEqual(self.store.show(card.ref).body, card.body)

    def test_edit_title_keeps_the_box_and_the_id(self):
        path = self.put("p", BASIC)
        card = self.store.edit("tabc", title="three, renamed #tag")
        self.assertEqual(card.title, "three, renamed #tag")
        self.assertEqual(card.tags, ["tag"])
        self.assertIn("- [ ] three, renamed #tag ^tabc\n", path.read_text())

    def test_edit_body_replaces_the_indented_lines(self):
        path = self.put("p", BASIC)
        card = self.store.edit("p:2", body=["first", "  nested", "", "last"])
        self.assertEqual(card.body, ["\tfirst", "\t  nested", "\t", "\tlast"])
        text = path.read_text()
        self.assertNotIn("body line", text)
        self.assertIn("two #tag 📅 2026-01-02", text)
        self.assertEqual(self.store.show(card.ref).body, card.body)

    def test_edit_body_drops_trailing_blank_lines_and_can_clear(self):
        self.put("p", BASIC)
        card = self.store.edit("p:2", body=["only", "", ""])
        self.assertEqual(card.body, ["\tonly"])
        card = self.store.edit(card.ref, body=[])
        self.assertEqual(card.body, [])

    def test_edit_rejects_a_bad_title(self):
        self.put("p", BASIC)
        for title in ("", "   ", "two\nlines", "sneaky ^tzzz"):
            with self.subTest(title=title), self.assertRaises(HerdboardError):
                self.store.edit("tabc", title=title)

    def test_edit_fails_when_the_card_changed_since_it_was_read(self):
        path = self.put("p", BASIC)
        before = self.store.show("tabc")
        path.write_text(path.read_text().replace("three ^tabc", "three changed ^tabc"))
        with self.assertRaises(ConflictError):
            self.store.edit("tabc", title="mine", expected=before)
        self.assertIn("three changed", path.read_text())

    def test_move_to_another_board_keeps_the_card(self):
        source = self.put("p", BASIC)
        target = self.put("q", board_text(col("Todo"), col("Doing"), col("Done", "**Complete**")))
        moved = self.store.move_to_board("p:2", target)
        self.assertEqual((moved.board, moved.column), ("q", "Todo"))
        self.assertEqual(moved.body, ["\tbody line"])
        self.assertNotIn("two #tag", source.read_text())
        self.assertIn("two #tag", target.read_text())
        self.assertEqual(self.store.show(moved.ref).board, "q")

    def test_move_to_another_board_keeps_the_id(self):
        self.put("p", BASIC)
        target = self.put("q", board_text(col("Todo"), col("Doing")))
        moved = self.store.move_to_board("tabc", target)
        self.assertEqual(moved.ref, "tabc")
        self.assertEqual(moved.column, "Doing")

    def test_move_to_another_board_without_that_column_uses_the_first(self):
        self.put("p", BASIC)
        target = self.put("q", board_text(col("Later"), col("Done", "**Complete**")))
        self.assertEqual(self.store.move_to_board("tabc", target).column, "Later")

    def test_move_to_another_board_and_column(self):
        self.put("p", BASIC)
        target = self.put("q", board_text(col("Todo"), col("Done", "**Complete**")))
        moved = self.store.move_to_board("tabc", target, "Done")
        self.assertEqual(moved.column, "Done")
        self.assertTrue(moved.checked)

    def test_move_to_another_board_never_reuses_a_new_id(self):
        self.put("p", board_text(col("Todo", "- [ ] a", "- [ ] b")))
        target = self.put("q", board_text(col("Todo", "- [ ] c", "- [ ] d")))
        # The source takes t001 and t002. The target is then offered t001 again.
        chars = iter("001001002001003004")
        with mock.patch("herdboard.store.secrets.choice", side_effect=lambda alphabet: next(chars)):
            self.store.move_to_board("p:1", target)
        ids = [c.ref for c in self.store.list_cards()]
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_move_to_the_same_board_is_refused(self):
        source = self.put("p", BASIC)
        with self.assertRaises(HerdboardError):
            self.store.move_to_board("tabc", source)

    def test_move_to_an_unknown_column_changes_nothing(self):
        source = self.put("p", BASIC)
        target = self.put("q", board_text(col("Todo")))
        before = (source.read_text(), target.read_text())
        with self.assertRaises(NotFoundError):
            self.store.move_to_board("tabc", target, "Nope")
        self.assertEqual((source.read_text(), target.read_text()), before)

    def test_shift_moves_a_card_up_and_down_in_its_column(self):
        path = self.put("p", board_text(col("Todo", "- [ ] one", "- [ ] two", "\tbody of two", "- [ ] three")))
        self.store.shift("p:2", -1)
        self.assertEqual([c.title for c in self.store.list_cards()], ["two", "one", "three"])
        self.assertIn("- [ ] two ^", path.read_text())
        two = [c for c in self.store.list_cards() if c.title == "two"][0]
        self.assertEqual(two.body, ["\tbody of two"])
        self.store.shift(two.ref, 1)
        self.store.shift(two.ref, 1)
        self.assertEqual([c.title for c in self.store.list_cards()], ["one", "three", "two"])

    def test_shift_at_the_edge_changes_nothing(self):
        path = self.put("p", board_text(col("Todo", "- [ ] one ^t001", "- [ ] two ^t002")))
        before = path.read_text()
        self.assertEqual(self.store.shift("t001", -1).title, "one")
        self.assertEqual(self.store.shift("t002", 1).title, "two")
        self.assertEqual(path.read_text(), before)

    def test_shift_keeps_other_lines_in_place(self):
        path = self.put("p", board_text(col("Done", "**Complete**", "- [x] one ^t001", "- [x] two ^t002")))
        self.store.shift("t002", -1)
        self.assertIn("**Complete**\n- [x] two ^t002\n- [x] one ^t001", path.read_text())

    def test_toggle_item_checks_and_clears_a_checklist_line(self):
        path = self.put(
            "p", board_text(col("Todo", "- [ ] ship ^t001", "\t- [ ] write notes", "\tplain", "\t\t- [x] nested"))
        )
        card = self.store.toggle_item("t001", 0)
        self.assertEqual(card.body[0], "\t- [x] write notes")
        self.assertIn("\t- [x] write notes\n", path.read_text())
        card = self.store.toggle_item("t001", 2)
        self.assertEqual(card.body[2], "\t\t- [ ] nested")
        card = self.store.toggle_item("t001", 0)
        self.assertEqual(card.body[0], "\t- [ ] write notes")

    def test_toggle_item_refuses_a_line_that_is_not_a_checklist_item(self):
        self.put("p", board_text(col("Todo", "- [ ] ship ^t001", "\tplain")))
        for index in (0, 5):
            with self.subTest(index=index), self.assertRaises(HerdboardError):
                self.store.toggle_item("t001", index)

    def test_toggle_item_fails_when_the_card_changed(self):
        path = self.put("p", board_text(col("Todo", "- [ ] ship ^t001", "\t- [ ] write notes")))
        before = self.store.show("t001")
        path.write_text(path.read_text().replace("write notes", "write the notes"))
        with self.assertRaises(ConflictError):
            self.store.toggle_item("t001", 0, expected=before)

    def test_delete_removes_the_card_and_its_body(self):
        path = self.put("p", BASIC)
        deleted = self.store.delete("p:2")
        self.assertEqual(deleted.title, "two #tag 📅 2026-01-02")
        self.assertEqual(deleted.column, "Todo")
        text = path.read_text()
        self.assertNotIn("two #tag", text)
        self.assertNotIn("body line", text)
        self.assertIn("- [ ] three ^tabc", text)
        self.assertIn("- [x] old", text)
        self.assertEqual([c.title for c in self.store.list_cards()], ["one", "three", "four"])

    def test_delete_by_id_keeps_every_other_line(self):
        path = self.put("p", BASIC)
        self.store.delete("tabc")
        self.assertNotIn("three", path.read_text())
        with self.assertRaises(NotFoundError):
            self.store.show("tabc")

    def test_delete_unknown_card(self):
        self.put("p", BASIC)
        with self.assertRaises(NotFoundError):
            self.store.delete("tzzz")

    def test_move_top_and_bottom(self):
        self.put("p", BASIC)
        self.store.move("tabc", "Todo", position="top")
        self.store.move("p:2", "Doing")
        cards = self.store.list_cards()
        self.assertEqual([c.title for c in cards if c.column == "Todo"][0], "three")
        self.assertEqual([c.title for c in cards if c.column == "Doing"], ["one"])

    def test_move_moves_body_lines(self):
        path = self.put("p", BASIC)
        self.store.move("p:2", "Doing")
        self.assertIn("## Doing\n\n- [ ] three ^tabc\n- [ ] two", path.read_text())
        view = self.store.show(self.store.list_cards()[2].ref)
        self.assertEqual(view.body, ["\tbody line"])

    def test_move_into_done_checks_and_out_unchecks(self):
        self.put("p", BASIC)
        view = self.store.move("tabc", "Done")
        self.assertEqual((view.column, view.checked), ("Done", True))
        view = self.store.move("tabc", "Doing")
        self.assertEqual((view.column, view.checked), ("Doing", False))

    def test_move_between_other_columns_keeps_check(self):
        self.put("p", board_text(col("Todo", "- [x] a ^taaa"), col("Doing"), col("Done", "**Complete**")))
        self.assertTrue(self.store.move("taaa", "Doing").checked)

    def test_move_into_any_completed_column_checks(self):
        self.put("p", SHIPPED)
        self.assertTrue(self.store.move("taaa", "Shipped").checked)
        self.assertTrue(self.store.move("tbbb", "Done").checked)

    def test_move_out_of_any_completed_column_unchecks(self):
        self.put("p", SHIPPED)
        self.store.move("taaa", "Shipped")
        self.assertFalse(self.store.move("taaa", "Todo").checked)

    def test_move_between_completed_columns_keeps_check(self):
        self.put("p", SHIPPED)
        self.store.move("taaa", "Shipped")
        view = self.store.move("taaa", "Done")
        self.assertEqual((view.column, view.checked), ("Done", True))

    def test_column_titled_done_without_marker_is_not_completed(self):
        self.put("p", board_text(col("Todo", "- [ ] a ^taaa"), col("Done")))
        self.assertFalse(self.store.move("taaa", "Done").checked)

    def test_configured_marker_adds_to_english(self):
        self.put(
            "p", board_text(col("Todo", "- [ ] a ^taaa"), col("Fini", "**Terminé**"), col("Shipped", "**Complete**"))
        )
        store = Store(Config(todos_dir=self.todos, complete_markers=["Terminé"]))
        self.assertTrue(store.move("taaa", "Fini").checked)
        self.assertTrue(store.move("taaa", "Shipped").checked)
        self.assertFalse(store.move("taaa", "Todo").checked)
        self.assertEqual(store.done("taaa").column, "Fini")

    def test_marker_must_be_the_marker_line(self):
        self.put("p", board_text(col("Todo", "- [ ] a ^taaa"), col("Other", "text **Complete** here")))
        self.assertFalse(self.store.move("taaa", "Other").checked)

    def test_add_into_completed_column_is_checked(self):
        path = self.put("p", SHIPPED)
        view = self.store.add(path, "finished", column="Shipped")
        self.assertTrue(view.checked)
        self.assertIn("- [x] finished ^", path.read_text())
        self.assertTrue(self.store.add(path, "also", column="Done").checked)
        self.assertFalse(self.store.add(path, "open", column="Todo").checked)

    def test_add_into_column_with_configured_marker_is_checked(self):
        path = self.put("p", board_text(col("Fini", "**Terminé**")))
        store = Store(Config(todos_dir=self.todos, complete_markers=["Terminé"]))
        self.assertTrue(store.add(path, "finished").checked)

    def test_move_unknown_column(self):
        self.put("p", BASIC)
        with self.assertRaises(NotFoundError):
            self.store.move("tabc", "Nope")

    def test_done_by_marker(self):
        self.put("p", board_text(col("Todo", "- [ ] a ^taaa"), col("Shipped", "**Complete**"), col("Done")))
        view = self.store.done("taaa")
        self.assertEqual((view.column, view.checked), ("Shipped", True))

    def test_done_by_name(self):
        self.put("p", board_text(col("Todo", "- [ ] a ^taaa"), col("done")))
        view = self.store.done("taaa")
        self.assertEqual((view.column, view.checked), ("done", True))

    def test_done_without_done_column(self):
        self.put("p", board_text(col("Todo", "- [ ] a ^taaa")))
        with self.assertRaises(HerdboardError) as caught:
            self.store.done("taaa")
        self.assertIn("p", str(caught.exception))


class DuplicateRefTest(StoreTestCase):
    def test_temp_ref_to_duplicate_card_raises(self):
        path = self.put("a", board_text(col("Todo", "- [ ] x ^tdup", "- [ ] y ^tdup"), col("Done")))
        before = path.read_text()
        for call in (
            lambda: self.store.done("a:1"),
            lambda: self.store.move("a:2", "Done"),
            lambda: self.store.show("a:1"),
        ):
            with self.assertRaises(DuplicateIdError):
                call()
        self.assertEqual(path.read_text(), before)

    def test_temp_ref_duplicate_across_boards_names_both(self):
        self.put("a", BASIC)
        self.put("b", BASIC)
        with self.assertRaises(DuplicateIdError) as caught:
            self.store.done("a:3")
        message = str(caught.exception)
        self.assertIn("a.md", message)
        self.assertIn("b.md", message)

    def test_active_card_duplicated_in_archive_blocks_commands(self):
        path = self.put(
            "p",
            board_text(
                col("Todo", "- [ ] x ^tdup"),
                col("Done"),
                archive="***\n\n## Archive\n\n- [x] old ^tdup\n\n",
            ),
        )
        before = path.read_text()
        for call in (
            lambda: self.store.done("tdup"),
            lambda: self.store.done("p:1"),
            lambda: self.store.show("tdup"),
        ):
            with self.assertRaises(DuplicateIdError) as caught:
                call()
            self.assertEqual(str(caught.exception).count("p.md"), 2)
        self.assertEqual(path.read_text(), before)

    def test_duplicate_created_before_lock_is_caught_under_lock(self):
        path = self.put("p", board_text(col("Todo", "- [ ] x ^tdup"), col("Done")))

        class Racy(Store):
            def _resolve(self, ref):
                located = super()._resolve(ref)
                other.write_text(path.read_text())
                return located

        other = self.todos / "projects/q.md"
        with self.assertRaises(DuplicateIdError):
            Racy(self.config).done("tdup")


class ValidBoardTest(StoreTestCase):
    BODY = "## Todo\n\n- [ ] a ^taaa\n\n## Done\n\n"

    def invalid_boards(self):
        return {
            "unclosed": "---\nkanban-plugin: board\n\n" + self.BODY,
            "missing key": "---\nrepo: x\n---\n\n" + self.BODY,
            "bad value": "---\n\nkanban-plugin: [\n\n---\n\n" + self.BODY,
            "no frontmatter": self.BODY,
        }

    def test_writes_raise_format_error_and_leave_file(self):
        for name, text in self.invalid_boards().items():
            path = self.put("p", text)
            for call in (
                lambda: self.store.move("taaa", "Done"),
                lambda: self.store.done("taaa"),
                lambda: self.store.add(path, "x"),  # noqa: B023  called only in this iteration
            ):
                with self.subTest(board=name), self.assertRaises(FormatError) as caught:
                    call()
                self.assertIn(str(path), str(caught.exception))
            self.assertEqual(path.read_text(), text)

    def test_reads_still_work(self):
        for name, text in self.invalid_boards().items():
            self.put("p", text)
            with self.subTest(board=name):
                self.assertEqual([c.ref for c in self.store.list_cards()], ["taaa"])
                self.assertEqual(self.store.show("taaa").title, "a")


class LockTest(StoreTestCase):
    def run_threads(self, targets):
        errors = []

        def wrap(target):
            def run():
                try:
                    target()
                except BaseException as error:  # noqa: BLE001
                    errors.append(error)

            return run

        threads = [threading.Thread(target=wrap(target)) for target in targets]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        return errors

    def test_concurrent_adds_to_two_boards_never_share_an_id(self):
        paths = [self.put(name, board_text(col("Todo"))) for name in ("a", "b")]
        draws = {}

        def same_sequence_per_thread(alphabet):
            it = draws.setdefault(threading.get_ident(), iter("aaabbbcccdddeeefff"))
            return next(it)

        real_serialize = kanban_md.serialize

        def slow_serialize(board):
            time.sleep(0.2)
            return real_serialize(board)

        store = Store(self.config, lock_timeout=5)
        with (
            mock.patch("herdboard.store.secrets.choice", same_sequence_per_thread),
            mock.patch("herdboard.store.kanban_md.serialize", slow_serialize),
        ):
            errors = self.run_threads([lambda p=path: store.add(p, "x") for path in paths])
        self.assertEqual(errors, [])
        refs = [c.ref for c in store.list_cards()]
        self.assertEqual(len(refs), 2)
        self.assertEqual(len(set(refs)), 2)

    def test_simultaneous_first_adds_create_inbox_once(self):
        real_text = Store._new_board_text

        def slow_text(self, frontmatter=()):
            time.sleep(0.1)
            return real_text(self, frontmatter)

        store = Store(self.config, lock_timeout=5)
        with mock.patch.object(Store, "_new_board_text", slow_text):
            errors = self.run_threads([lambda: store.add(store.ensure_inbox(), "x") for _ in range(4)])
        self.assertEqual(errors, [])
        self.assertEqual(len(store.list_cards()), 4)
        self.assertEqual(len({c.ref for c in store.list_cards()}), 4)

    def test_simultaneous_init_board_one_wins(self):
        identity = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))
        real_text = Store._new_board_text

        def slow_text(self, frontmatter=()):
            time.sleep(0.1)
            return real_text(self, frontmatter)

        store = Store(self.config, lock_timeout=5)
        with mock.patch.object(Store, "_new_board_text", slow_text):
            errors = self.run_threads([lambda: store.init_board(identity) for _ in range(2)])
        self.assertEqual(len(errors), 1)
        self.assertIs(type(errors[0]), HerdboardError)
        self.assertEqual(len(list((self.todos / "projects").glob("*.md"))), 1)

    def test_creation_goes_through_temp_file_and_replace(self):
        with mock.patch("herdboard.store.os.replace", wraps=os.replace) as replace:
            self.store.ensure_inbox()
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(
            sorted(p.name for p in self.todos.iterdir()),
            [".herdboard.lock", "inbox.md", "projects"],
        )

    def test_created_board_follows_umask(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        path = self.store.ensure_inbox()
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_locked_store_blocks_creation(self):
        with open(self.todos / ".herdboard.lock", "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            with self.assertRaises(ConflictError):
                self.store.ensure_inbox()
        self.assertFalse(self.store.inbox_path.exists())


class CrlfTest(StoreTestCase):
    def put_crlf(self, name="p"):
        return self.put(name, BASIC.replace("\n", "\r\n"))

    def assertPureCrlf(self, path):
        data = path.read_bytes()
        self.assertIsNone(re.search(rb"(?<!\r)\n", data))
        self.assertIsNone(re.search(rb"\r(?!\n)", data))

    def test_add(self):
        path = self.put_crlf()
        view = self.store.add(path, "fresh", column="Doing")
        self.assertPureCrlf(path)
        self.assertEqual(view.column, "Doing")
        self.assertRegex(path.read_text(newline=""), r"- \[ \] fresh \^t[0-9a-z]{3}\r\n")

    def test_move_and_done(self):
        path = self.put_crlf()
        view = self.store.move("tabc", "Todo", position="top")
        self.assertEqual((view.column, view.checked), ("Todo", False))
        self.assertPureCrlf(path)
        self.assertTrue(self.store.done("tabc").checked)
        self.assertPureCrlf(path)
        self.assertFalse(self.store.move("tabc", "Doing").checked)
        self.assertPureCrlf(path)

    def test_id_assignment_keeps_existing_ids_and_crlf(self):
        path = self.put_crlf()
        self.store.move("p:1", "Doing")
        text = path.read_text(newline="")
        self.assertEqual(text.count("^tabc"), 1)
        self.assertNotIn("^tabc\r ^", text)
        self.assertEqual(len(re.findall(r" \^t[0-9a-z]{3}\r\n", text)), 4)
        self.assertPureCrlf(path)

    def test_complete_marker_recognised(self):
        path = self.put_crlf()
        self.assertTrue(self.store.move("tabc", "Done").checked)
        self.assertFalse(self.store.move("tabc", "Doing").checked)
        self.assertPureCrlf(path)


class WriteTest(StoreTestCase):
    def test_column_changed_between_resolve_and_lock_raises_conflict(self):
        path = self.put("p", BASIC)

        class Racy(Store):
            def _resolve(self, ref):
                located = super()._resolve(ref)
                path.write_text(
                    path.read_text()
                    .replace("## Doing\n\n- [ ] three ^tabc\n", "## Doing\n\n")
                    .replace("## Todo\n\n", "## Todo\n\n- [ ] three ^tabc\n")
                )
                return located

        with self.assertRaises(ConflictError):
            Racy(self.config).move("tabc", "Done")
        self.assertIn("## Todo\n\n- [ ] three ^tabc\n", path.read_text())
        self.assertNotIn("[x] three", path.read_text())

    def test_only_expected_lines_change(self):
        path = self.put("p", BASIC)
        before = path.read_text()
        self.store.move("tabc", "Todo")
        expected = before.replace("## Doing\n\n- [ ] three ^tabc\n", "## Doing\n\n").replace(
            "\tbody line\n", "\tbody line\n- [ ] three ^tabc\n", 1
        )
        # Cards without IDs also gain IDs on the first write.
        after = path.read_text()
        stripped = re.sub(r" \^t(?!abc)[0-9a-z]{3}$", "", after, flags=re.M)
        self.assertEqual(stripped, expected)
        self.assertEqual(len(re.findall(r" \^t(?!abc)[0-9a-z]{3}$", after, re.M)), 3)

    def test_second_write_changes_only_target(self):
        path = self.put("p", BASIC)
        self.store.add(path, "seed")
        before = path.read_text()
        ref = self.store.list_cards()[0].ref
        self.store.move(ref, "Doing")
        after = path.read_text()
        self.assertEqual(sorted(after.splitlines()), sorted(before.splitlines()))

    def test_mode_preserved_and_no_temp_files(self):
        path = self.put("p", BASIC, mode=0o640)
        self.store.done("tabc")
        self.assertEqual(path.stat().st_mode & 0o777, 0o640)
        self.assertEqual([p.name for p in path.parent.iterdir()], ["p.md"])
        self.assertEqual(sorted(p.name for p in self.todos.iterdir()), [".herdboard.lock", "projects"])

    def test_one_store_wide_lock_file(self):
        self.put("p", BASIC)
        self.store.ensure_inbox()
        self.store.add(self.store.inbox_path, "x")
        self.store.done("tabc")
        self.assertTrue((self.todos / ".herdboard.lock").exists())
        self.assertFalse(list(self.todos.rglob(".*.md.lock")))

    def test_other_card_edited_between_resolve_and_write_is_kept(self):
        path = self.put("p", BASIC)

        class Racy(Store):
            def _resolve(self, ref):
                located = super()._resolve(ref)
                path.write_text(path.read_text().replace("- [ ] one", "- [ ] one edited"))
                return located

        Racy(self.config).move("tabc", "Done")
        text = path.read_text()
        self.assertIn("- [ ] one edited", text)
        self.assertIn("- [x] three ^tabc", text)

    def test_target_card_edited_raises_conflict(self):
        path = self.put("p", BASIC)

        class Racy(Store):
            def _resolve(self, ref):
                located = super()._resolve(ref)
                path.write_text(path.read_text().replace("three", "three edited"))
                return located

        edited = path.read_text().replace("three", "three edited")
        with self.assertRaises(ConflictError):
            Racy(self.config).move("tabc", "Done")
        self.assertEqual(path.read_text(), edited)

    def test_target_card_removed_raises_conflict(self):
        path = self.put("p", BASIC)

        class Racy(Store):
            def _resolve(self, ref):
                located = super()._resolve(ref)
                path.write_text(path.read_text().replace("- [ ] three ^tabc\n", ""))
                return located

        with self.assertRaises(ConflictError):
            Racy(self.config).done("tabc")

    def test_temp_ref_shifted_raises_conflict(self):
        path = self.put("p", BASIC)

        class Racy(Store):
            def _resolve(self, ref):
                located = super()._resolve(ref)
                path.write_text(path.read_text().replace("- [ ] one\n", ""))
                return located

        with self.assertRaises(ConflictError):
            Racy(self.config).done("p:1")

    def test_locked_board_raises_conflict(self):
        path = self.put("p", BASIC)
        before = path.read_text()
        with open(self.todos / ".herdboard.lock", "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            with self.assertRaises(ConflictError) as caught:
                self.store.done("tabc")
            self.assertIn("locked", str(caught.exception))
            with self.assertRaises(ConflictError):
                self.store.add(path, "x")
        self.assertEqual(path.read_text(), before)
        self.assertTrue(self.store.done("tabc").checked)


class TriageColumnTests(StoreTestCase):
    def test_add_to_triage_inserts_the_column_first(self):
        path = self.put(
            "p",
            BASIC.replace("- [ ] one", "- [ ] one ^taaa")
            .replace("- [ ] two #tag 📅 2026-01-02", "- [ ] two #tag 📅 2026-01-02 ^tbbb")
            .replace("- [x] four", "- [x] four ^tccc"),
        )
        before = path.read_text()
        view = self.store.add(path, "report the crash", column="Triage")
        self.assertEqual((view.column, view.title), ("Triage", "report the crash"))
        board = kanban_md.parse(path.read_text())
        self.assertEqual([c.title for c in board.columns], ["Triage", "Todo", "Doing", "Done"])
        self.assertEqual([c.title for c in board.column("Triage").cards], ["report the crash"])
        # The whole file: the new column as the plugin writes one, and every other byte as it was.
        block = f"## Triage\n\n- [ ] report the crash ^{view.ref}\n\n\n"
        self.assertEqual(path.read_text(), before.replace("## Todo\n", block + "## Todo\n", 1))

    def test_a_new_triage_column_has_the_layout_of_a_new_board(self):
        """Obsidian Kanban must open the made column: its first card goes where `insert_card` puts it.

        Compared with the same card added to a column that `new_board` wrote.
        """
        path = self.put(
            "p",
            BASIC.replace("- [ ] one", "- [ ] one ^taaa")
            .replace("- [ ] two #tag 📅 2026-01-02", "- [ ] two #tag 📅 2026-01-02 ^tbbb")
            .replace("- [x] four", "- [x] four ^tccc"),
        )
        view = self.store.add(path, "report the crash", column="Triage")
        made = path.read_text().split("## Triage", 1)[1].split("## Todo", 1)[0]
        board = kanban_md.parse(kanban_md.new_board(["Triage", "Backlog"], None))
        kanban_md.insert_card(board.column("Triage"), kanban_md.Card([f"- [ ] report the crash ^{view.ref}"]))
        written = kanban_md.serialize(board).split("## Triage", 1)[1].split("## Backlog", 1)[0]
        self.assertEqual(made, written)

    def test_add_to_triage_uses_the_existing_column_in_any_case(self):
        path = self.put("p", board_text(col("Triage"), col("Todo")))
        view = self.store.add(path, "report", column="triage")
        self.assertEqual(view.column, "Triage")
        board = kanban_md.parse(path.read_text())
        self.assertEqual([c.title for c in board.columns], ["Triage", "Todo"])

    def test_add_without_a_column_skips_triage(self):
        path = self.put("p", board_text(col("Triage"), col("Backlog"), col("Done", "**Complete**")))
        view = self.store.add(path, "later")
        self.assertEqual(view.column, "Backlog")

    def test_a_board_with_only_triage_has_no_other_default_column(self):
        path = self.put("p", board_text(col("Triage", "- [ ] sent ^tabc")))
        before = path.read_text()
        with self.assertRaises(HerdboardError) as caught:
            self.store.add(path, "later")
        self.assertIn("no column apart from Triage", str(caught.exception))
        self.assertEqual(path.read_text(), before)

    def test_move_to_triage_creates_the_column(self):
        path = self.put("p", BASIC)
        view = self.store.move("tabc", "Triage")
        self.assertEqual(view.column, "Triage")
        self.assertEqual([c.title for c in kanban_md.parse(path.read_text()).columns][0], "Triage")

    def test_move_to_an_unknown_column_still_fails(self):
        path = self.put("p", BASIC)
        before = path.read_text()
        with self.assertRaises(NotFoundError):
            self.store.move("tabc", "Nonsense")
        self.assertEqual(path.read_text(), before)

    def test_move_to_another_board_from_triage_creates_triage_there(self):
        self.put("p", board_text(col("Triage", "- [ ] sent ^tabc"), col("Todo")))
        target = self.put("q", board_text(col("Backlog"), col("Done", "**Complete**")))
        moved = self.store.move_to_board("tabc", target)
        self.assertEqual((moved.board, moved.column), ("q", "Triage"))
        self.assertEqual([c.title for c in kanban_md.parse(target.read_text()).columns], ["Triage", "Backlog", "Done"])

    def test_move_to_another_board_from_backlog_skips_triage(self):
        self.put("p", board_text(col("Backlog", "- [ ] sent ^tabc"), col("Todo")))
        target = self.put("q", board_text(col("Triage"), col("Todo"), col("Done", "**Complete**")))
        self.assertEqual(self.store.move_to_board("tabc", target).column, "Todo")

    def test_move_to_another_board_and_column_creates_triage(self):
        self.put("p", BASIC)
        target = self.put("q", board_text(col("Todo")))
        moved = self.store.move_to_board("tabc", target, "Triage")
        self.assertEqual(moved.column, "Triage")


class SnapshotTests(StoreTestCase):
    def test_snapshot_gives_columns_in_file_order_with_cards(self):
        path = self.put("proj", BASIC)
        with mock.patch.object(self.store, "_read", wraps=self.store._read) as read:
            snap = self.store.snapshot(path)
        self.assertEqual(read.call_count, 1)
        self.assertIsInstance(snap, BoardSnapshot)
        self.assertEqual((snap.name, snap.path), ("proj", path))
        self.assertEqual([c.title for c in snap.columns], ["Todo", "Doing", "Done"])
        self.assertEqual([c.completed for c in snap.columns], [False, False, True])
        self.assertEqual([c.limit for c in snap.columns], [None, None, None])
        self.assertEqual(
            [[card.title for card in c.cards] for c in snap.columns],
            [["one", "two #tag 📅 2026-01-02"], ["three"], ["four"]],
        )
        self.assertEqual(snap.columns[0].cards[0].ref, "proj:1")
        self.assertEqual(snap.columns[1].cards[0].ref, "tabc")

    def test_snapshot_reports_limit(self):
        path = self.put("proj", board_text("## Todo (3)\n\n- [ ] one\n\n"))
        self.assertEqual(self.store.snapshot(path).columns[0].limit, 3)


if __name__ == "__main__":
    unittest.main()
