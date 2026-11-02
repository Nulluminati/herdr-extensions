import fcntl
import os
import threading
import time
from unittest import mock

from herdboard import card_file
from herdboard.identity import RepoIdentity
from herdboard.model import (
    ArchivedError,
    ConflictError,
    DuplicateIdError,
    FormatError,
    HerdboardError,
    NotFoundError,
    OldFormatError,
)
from herdboard.store import BoardSnapshot, Store
from support.herdboard_store import StoreTestCase, write_card

IDENTITY = RepoIdentity("github.com/acme/api", "github.com", ("acme", "api"))


class BoardsTest(StoreTestCase):
    def test_paths_and_names(self):
        self.put("b")
        self.put("a")
        self.assertEqual([p.name for p in self.store.board_paths()], ["a", "b"])
        inbox = self.put("inbox")
        self.assertEqual(self.store.board_paths()[0], inbox)
        self.assertEqual(self.store.board_name(inbox), "inbox")

    def test_a_folder_without_board_toml_is_not_a_board(self):
        (self.todos / "boards" / "stray").mkdir(parents=True)
        self.assertEqual(self.store.board_paths(), [])

    def test_resolve_board(self):
        inbox = self.put("inbox")
        proj = self.put("acme-api", [("Todo", [])])
        self.assertEqual(self.store.resolve_board("inbox"), inbox)
        self.assertEqual(self.store.resolve_board("acme-api"), proj)
        for name in ("nope", "", "..", "a/b"):
            with self.subTest(name=name), self.assertRaises(NotFoundError):
                self.store.resolve_board(name)

    def test_ensure_inbox(self):
        path = self.store.ensure_inbox()
        self.assertEqual(path, self.todos / "boards" / "inbox")
        board = card_file.parse_board(self.board_text(path), path)
        self.assertEqual([c.name for c in board.columns], self.config.columns)
        self.assertEqual([c.name for c in board.columns if c.done], ["Done"])
        (path / "board.toml").write_text("format = 1\n")
        self.store.ensure_inbox()
        self.assertEqual(self.board_text(path), "format = 1\n")

    def test_init_board(self):
        path = self.store.init_board(IDENTITY, merge="pr")
        self.assertEqual(path, self.todos / "boards" / "acme-api")
        board = card_file.parse_board(self.board_text(path), path)
        self.assertEqual((board.repo, board.merge), ("github.com/acme/api", "pr"))
        self.assertEqual(self.store.board_for_repo(IDENTITY), path)
        with self.assertRaises(HerdboardError):
            self.store.init_board(IDENTITY)
        self.assertEqual(self.store.init_board(IDENTITY, exist_ok=True), path)

    def test_init_board_without_done_column(self):
        self.config.columns = ["Backlog", "Doing"]
        path = self.store.init_board(IDENTITY)
        board = card_file.parse_board(self.board_text(path), path)
        self.assertFalse(any(c.done for c in board.columns))

    def test_a_repository_named_inbox_gets_a_prefixed_board(self):
        self.store.ensure_inbox()
        identity = RepoIdentity("github.com/x/inbox", "github.com", ("inbox",))
        self.assertEqual(self.store.init_board(identity).name, "github.com-inbox")


class ReadTest(StoreTestCase):
    def test_list_cards(self):
        path = self.put("p")
        cards = self.store.list_cards()
        self.assertEqual([c.ref for c in cards], ["t001", "t002", "tabc", "t004"])
        two = cards[1]
        self.assertEqual(two.title, "two")
        self.assertEqual(two.tags, ["tag"])
        self.assertEqual(two.due, "2026-01-02")
        self.assertEqual(two.body, ["body line"])
        self.assertEqual(two.path, path)
        self.assertEqual(two.board, "p")
        self.assertEqual([c.checked for c in cards], [False, False, False, True])

    def test_show(self):
        self.put("p")
        self.assertEqual(self.store.show("tabc").column, "Doing")

    def test_unknown_refs(self):
        self.put("p")
        for ref in ("tzzz", "p:1", "nonsense"):
            with self.subTest(ref=ref), self.assertRaises(NotFoundError):
                self.store.show(ref)

    def test_archive_not_listed_and_raises_archived(self):
        self.put("p")
        self.assertNotIn("t005", [c.ref for c in self.store.list_cards()])
        with self.assertRaises(ArchivedError):
            self.store.show("t005")
        self.assertTrue(self.store.show("t005", archived=True).archived)

    def test_a_board_read_opens_no_archived_card(self):
        path = self.put("p")
        (path / "archive" / "t005" / "card.md").write_text("not a card")
        self.assertEqual(len(self.store.list_cards()), 4)
        with self.assertRaises(FormatError):
            self.store.archived_cards()

    def test_orphan_folder_shows_at_the_bottom_of_the_first_column(self):
        path = self.put("p")
        write_card(path / "cards", {"id": "t0rf", "title": "orphan"})
        snapshot = self.store.snapshot(path)
        self.assertEqual([c.ref for c in snapshot.columns[0].cards], ["t001", "t002", "t0rf"])
        before = self.board_text(path)
        self.store.move("tabc", "Done")
        self.assertNotEqual(self.board_text(path), before)
        self.assertEqual(self.columns(path)["Todo"], ["t001", "t002", "t0rf"])

    def test_listed_id_without_a_folder_is_skipped(self):
        path = self.put("p")
        (path / "board.toml").write_text(self.board_text(path).replace('"tabc"', '"tabc",\n    "tgon"'))
        self.assertEqual([c.ref for c in self.store.snapshot(path).columns[1].cards], ["tabc"])

    def test_folders_with_other_names_are_ignored(self):
        path = self.put("p")
        write_card(path / "cards", {"id": "notes", "title": "x"})
        self.assertEqual(len(self.store.list_cards()), 4)

    def test_duplicate_id_across_boards(self):
        self.put("a")
        self.put("b")
        with self.assertRaises(DuplicateIdError):
            self.store.show("tabc")
        with self.assertRaises(DuplicateIdError):
            self.store.move("tabc", "Done")

    def test_active_card_duplicated_in_an_archive_blocks_commands(self):
        path = self.put("p")
        write_card(path / "archive", {"id": "tabc", "title": "copy"})
        with self.assertRaises(DuplicateIdError):
            self.store.done("tabc")

    def test_bad_card_file_names_the_path(self):
        path = self.put("p")
        (path / "cards" / "tabc" / "card.md").write_text("+++\ntitle = \n+++\n")
        with self.assertRaises(FormatError) as caught:
            self.store.list_cards()
        self.assertIn("tabc", str(caught.exception))

    def test_unknown_board_format(self):
        path = self.put("p")
        (path / "board.toml").write_text(self.board_text(path).replace("format = 1", "format = 9"))
        with self.assertRaises(FormatError):
            self.store.list_cards()


class OldFormatTest(StoreTestCase):
    def test_old_layout_without_boards(self):
        (self.todos / "inbox.md").write_text("---\nkanban-plugin: board\n---\n")
        for call in (self.store.board_paths, self.store.ensure_inbox, lambda: self.store.resolve_board("inbox")):
            with self.subTest(call=call), self.assertRaises(OldFormatError) as caught:
                call()
            self.assertIn("herdboard convert-store", str(caught.exception))

    def test_old_project_file_without_boards(self):
        (self.todos / "projects").mkdir()
        (self.todos / "projects" / "p.md").write_text("x")
        with self.assertRaises(OldFormatError):
            self.store.board_paths()

    def test_boards_and_a_new_old_file(self):
        self.put("inbox")
        (self.todos / "inbox.md").write_text("x")
        with self.assertRaises(OldFormatError) as caught:
            self.store.list_cards()
        self.assertIn("inbox.md", str(caught.exception))


class IdsTest(StoreTestCase):
    def test_new_id_avoids_folders_on_other_boards_and_in_archives(self):
        path = self.put("p", [("Todo", [])])
        self.put("q", [("Todo", [{"id": "taaa", "title": "x"}])], archive=[{"id": "tbbb", "title": "y"}])
        draws = iter("aaabbbccc")
        with mock.patch("herdboard.store.secrets.choice", lambda alphabet: next(draws)):
            card = self.store.add(path, "new")
        self.assertEqual(card.ref, "tccc")

    def test_ids_unique_across_boards(self):
        a = self.put("a", [("Todo", [])])
        b = self.put("b", [("Todo", [])])
        refs = [self.store.add(path, "x").ref for path in (a, b) * 5]
        self.assertEqual(len(set(refs)), 10)


class AddTest(StoreTestCase):
    def test_add_writes_a_card_folder_then_lists_it(self):
        path = self.put("p")
        card = self.store.add(
            path, "  new card ", body=["line", "", ""], tags=["x", "x", "y"], type="bug", due="2026-03-04"
        )
        self.assertEqual(
            (card.column, card.title, card.tags, card.type, card.due),
            ("Todo", "new card", ["x", "y"], "bug", "2026-03-04"),
        )
        self.assertEqual(self.columns(path)["Todo"], ["t001", "t002", card.ref])
        text = self.card_text(path, card.ref)
        self.assertTrue(text.startswith('+++\ntitle = "new card"\n'))
        self.assertTrue(text.endswith("+++\n\nline\n"))
        self.assertIn("created = ", text)

    def test_hash_words_stay_in_the_title(self):
        path = self.put("p")
        card = self.store.add(path, "fix #123 now")
        self.assertEqual((card.title, card.tags), ("fix #123 now", []))

    def test_add_top_and_named_column(self):
        path = self.put("p")
        card = self.store.add(path, "x", column="doing", position="top")
        self.assertEqual(self.columns(path)["Doing"], [card.ref, "tabc"])

    def test_add_into_the_done_column_is_checked(self):
        path = self.put("p")
        self.assertTrue(self.store.add(path, "x", column="Done").checked)

    def test_add_rejects_bad_input_and_writes_nothing(self):
        path = self.put("p")
        before = self.board_text(path)
        for kwargs in ({"text": ""}, {"text": "a\nb"}, {"text": "x", "column": "Nope"}, {"text": "x", "due": "soon"}):
            with self.subTest(kwargs=kwargs), self.assertRaises(HerdboardError):
                self.store.add(path, **kwargs)
        self.assertEqual(self.board_text(path), before)
        self.assertEqual(len(list((path / "cards").iterdir())), 4)

    def test_crash_after_the_card_file_leaves_an_orphan_that_shows(self):
        path = self.put("p")
        real = Store._write_board
        with mock.patch.object(Store, "_write_board", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.store.add(path, "survivor")
        self.assertIn("survivor", [c.title for c in self.store.list_cards()])
        self.assertIs(Store._write_board, real)


class EditTest(StoreTestCase):
    def test_edit_rewrites_only_card_md(self):
        path = self.put("p")
        before = self.board_text(path)
        card = self.store.edit("t002", title="two!", body=["new"], tags=["a"], type="bug", due=None)
        self.assertEqual((card.title, card.body, card.tags, card.type, card.due), ("two!", ["new"], ["a"], "bug", None))
        self.assertEqual(self.board_text(path), before)
        self.assertNotIn("due", self.card_text(path, "t002"))

    def test_edit_keeps_what_it_is_not_given(self):
        self.put("p")
        card = self.store.edit("t002", title="renamed")
        self.assertEqual((card.tags, card.due, card.body), (["tag"], "2026-01-02", ["body line"]))

    def test_edit_keeps_unknown_keys(self):
        path = self.put("p")
        file = path / "cards" / "t002" / "card.md"
        file.write_text(file.read_text().replace("+++\n\n", 'colour = "red"\n+++\n\n', 1))
        self.store.edit("t002", title="x")
        self.assertIn('colour = "red"', file.read_text())

    def test_edit_rejects_a_bad_title(self):
        self.put("p")
        for title in ("", "a\nb"):
            with self.subTest(title=title), self.assertRaises(HerdboardError):
                self.store.edit("t002", title=title)

    def test_edit_fails_when_the_card_changed_since_it_was_read(self):
        self.put("p")
        seen = self.store.show("t002")
        self.store.edit("t002", title="changed")
        with self.assertRaises(ConflictError):
            self.store.edit("t002", title="mine", expected=seen)

    def test_conflict_when_the_card_changed_after_the_read(self):
        path = self.put("p")

        class Racy(Store):
            def _resolve(self, ref):
                located = super()._resolve(ref)
                file = path / "cards" / ref / "card.md"
                file.write_text(file.read_text().replace("three", "changed"))
                return located

        with self.assertRaises(ConflictError) as caught:
            Racy(self.config).edit("tabc", title="mine")
        self.assertEqual(caught.exception.exit_code, 2)
        self.assertEqual(self.store.show("tabc").title, "changed")

    def test_conflict_when_the_column_changed_after_the_read(self):
        path = self.put("p")

        class Racy(Store):
            def _resolve(self, ref):
                located = super()._resolve(ref)
                Store(self.config).move("tabc", "Todo")
                return located

        with self.assertRaises(ConflictError):
            Racy(self.config).move("tabc", "Done")
        self.assertEqual(self.columns(path)["Todo"][-1], "tabc")


class MoveTest(StoreTestCase):
    def test_move_rewrites_only_board_toml(self):
        path = self.put("p")
        before = self.card_text(path, "tabc")
        card = self.store.move("tabc", "Done")
        self.assertTrue(card.checked)
        self.assertEqual(self.columns(path)["Done"], ["t004", "tabc"])
        self.assertEqual(self.card_text(path, "tabc"), before)

    def test_move_top(self):
        path = self.put("p")
        self.store.move("tabc", "Todo", "top")
        self.assertEqual(self.columns(path)["Todo"], ["tabc", "t001", "t002"])

    def test_move_out_of_done_unchecks(self):
        self.put("p")
        self.assertFalse(self.store.move("t004", "Todo").checked)

    def test_move_unknown_column_changes_nothing(self):
        path = self.put("p")
        before = self.board_text(path)
        with self.assertRaises(NotFoundError):
            self.store.move("tabc", "Nope")
        self.assertEqual(self.board_text(path), before)

    def test_done_by_flag_and_by_name(self):
        path = self.put(
            "p",
            [("Todo", [{"id": "taaa", "title": "x"}]), ("Finished", [], {"done": True}), ("Done", [], {"done": False})],
        )
        self.assertEqual(self.store.done("taaa").column, "Finished")
        other = self.put("q", [("Todo", [{"id": "tbbb", "title": "x"}]), ("Done", [], {"done": False})])
        self.assertEqual(self.store.done("tbbb").column, "Done")
        self.assertTrue(path.exists() and other.exists())

    def test_done_without_done_column(self):
        self.put("p", [("Todo", [{"id": "taaa", "title": "x"}])])
        with self.assertRaises(HerdboardError):
            self.store.done("taaa")

    def test_shift(self):
        path = self.put("p")
        self.store.shift("t002", -1)
        self.assertEqual(self.columns(path)["Todo"], ["t002", "t001"])
        self.store.shift("t002", -1)
        self.assertEqual(self.columns(path)["Todo"], ["t002", "t001"])
        self.store.shift("t002", 1)
        self.assertEqual(self.columns(path)["Todo"], ["t001", "t002"])


class ToggleTest(StoreTestCase):
    def setUp(self):
        super().setUp()
        self.path = self.put(
            "p", [("Todo", [{"id": "taaa", "title": "x", "body": ["intro", "- [ ] one", "- [x] two"]}])]
        )

    def test_toggle_item(self):
        self.assertEqual(self.store.toggle_item("taaa", 1).body, ["intro", "- [x] one", "- [x] two"])
        self.assertEqual(self.store.toggle_item("taaa", 2).body, ["intro", "- [x] one", "- [ ] two"])

    def test_toggle_refuses_a_line_that_is_not_an_item(self):
        for index in (0, 3, -1):
            with self.subTest(index=index), self.assertRaises(HerdboardError):
                self.store.toggle_item("taaa", index)

    def test_toggle_fails_when_the_card_changed(self):
        seen = self.store.show("taaa")
        self.store.edit("taaa", body=["- [ ] other"])
        with self.assertRaises(ConflictError):
            self.store.toggle_item("taaa", 0, expected=seen)


class DeleteTest(StoreTestCase):
    def test_delete_removes_the_folder_with_log_and_attachments(self):
        path = self.put("p")
        (path / "cards" / "tabc" / "log.md").write_text("- log\n")
        (path / "cards" / "tabc" / "attachments").mkdir()
        (path / "cards" / "tabc" / "attachments" / "plan.md").write_text("plan")
        card = self.store.delete("tabc")
        self.assertEqual(card.title, "three")
        self.assertFalse((path / "cards" / "tabc").exists())
        self.assertEqual(self.columns(path)["Doing"], [])

    def test_crash_after_the_board_write_leaves_an_orphan(self):
        path = self.put("p")
        with (
            mock.patch("herdboard.store.shutil.rmtree", side_effect=OSError("busy")),
            self.assertRaises(HerdboardError),
        ):
            self.store.delete("tabc")
        self.assertEqual(self.columns(path)["Doing"], [])
        self.assertIn("tabc", [c.ref for c in self.store.list_cards()])

    def test_active_card_is_refused(self):
        path = self.put("p")
        with self.assertRaises(HerdboardError) as caught:
            self.store.delete("tabc", blocked=lambda ref: "its agent runs")
        self.assertEqual(caught.exception.code, "card_active")
        self.assertEqual(str(caught.exception), "Cannot delete tabc: its agent runs")
        self.assertTrue((path / "cards" / "tabc").exists())
        self.assertEqual(self.columns(path)["Doing"], ["tabc"])

    def test_delete_unknown_card(self):
        self.put("p")
        with self.assertRaises(NotFoundError):
            self.store.delete("tzzz")


class MoveToBoardTest(StoreTestCase):
    def test_folder_moves_with_log_and_attachments(self):
        a = self.put("a")
        b = self.put("b", [("Todo", []), ("Doing", []), ("Done", [])])
        (a / "cards" / "tabc" / "log.md").write_text("- log\n")
        card = self.store.move_to_board("tabc", b)
        self.assertEqual((card.board, card.column, card.ref), ("b", "Doing", "tabc"))
        self.assertTrue((b / "cards" / "tabc" / "log.md").is_file())
        self.assertFalse((a / "cards" / "tabc").exists())
        self.assertEqual(self.columns(a)["Doing"], [])
        self.assertEqual(self.columns(b)["Doing"], ["tabc"])

    def test_without_that_column_uses_the_default(self):
        self.put("a")
        b = self.put("b", [("Backlog", []), ("Done", [])])
        self.assertEqual(self.store.move_to_board("tabc", b).column, "Backlog")

    def test_named_column(self):
        self.put("a")
        b = self.put("b", [("Backlog", []), ("Done", [])])
        self.assertEqual(self.store.move_to_board("tabc", b, "Done").column, "Done")

    def test_same_board_is_refused(self):
        a = self.put("a")
        with self.assertRaises(HerdboardError):
            self.store.move_to_board("tabc", a)

    def test_active_card_is_refused(self):
        a = self.put("a")
        b = self.put("b", [("Todo", [])])
        with self.assertRaises(HerdboardError) as caught:
            self.store.move_to_board("tabc", b, blocked=lambda ref: "its agent runs")
        self.assertEqual(caught.exception.code, "card_active")
        self.assertTrue((a / "cards" / "tabc").exists())

    def test_crash_after_the_folder_move_leaves_the_card_on_the_target(self):
        self.put("a")
        b = self.put("b", [("Todo", [])])
        with mock.patch.object(Store, "_write_board", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.store.move_to_board("tabc", b)
        self.assertEqual([(c.board, c.ref) for c in self.store.list_cards([b])], [("b", "tabc")])
        self.assertNotIn("tabc", [c.ref for c in self.store.list_cards([self.todos / "boards" / "a"])])

    def test_sub_card_loses_its_parent(self):
        self.put("a", [("Todo", [{"id": "tpar", "title": "p"}, {"id": "tkid", "title": "k", "parent": "tpar"}])])
        b = self.put("b", [("Todo", [])])
        self.assertIsNone(self.store.move_to_board("tkid", b).parent)

    def test_parent_with_sub_cards_is_refused(self):
        self.put("a", [("Todo", [{"id": "tpar", "title": "p"}, {"id": "tkid", "title": "k", "parent": "tpar"}])])
        b = self.put("b", [("Todo", [])])
        with self.assertRaises(HerdboardError):
            self.store.move_to_board("tpar", b)


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
        paths = [self.put(name, [("Todo", [])]) for name in ("a", "b")]
        draws = {}

        def same_sequence_per_thread(alphabet):
            it = draws.setdefault(threading.get_ident(), iter("aaabbbcccdddeeefff"))
            return next(it)

        real = Store._write_card

        def slow_write(self, card):
            time.sleep(0.2)
            return real(self, card)

        store = Store(self.config, lock_timeout=5)
        with (
            mock.patch("herdboard.store.secrets.choice", same_sequence_per_thread),
            mock.patch.object(Store, "_write_card", slow_write),
        ):
            errors = self.run_threads([lambda p=path: store.add(p, "x") for path in paths])
        self.assertEqual(errors, [])
        refs = [c.ref for c in store.list_cards()]
        self.assertEqual(len(set(refs)), 2)

    def test_simultaneous_first_adds_create_inbox_once(self):
        store = Store(self.config, lock_timeout=5)
        errors = self.run_threads([lambda: store.add(store.ensure_inbox(), "x") for _ in range(4)])
        self.assertEqual(errors, [])
        self.assertEqual(len({c.ref for c in store.list_cards()}), 4)

    def test_simultaneous_init_board_one_wins(self):
        store = Store(self.config, lock_timeout=5)
        errors = self.run_threads([lambda: store.init_board(IDENTITY) for _ in range(2)])
        self.assertEqual(len(errors), 1)
        self.assertIs(type(errors[0]), HerdboardError)
        self.assertEqual(len(list((self.todos / "boards").iterdir())), 1)

    def test_writes_go_through_a_temp_file_and_replace(self):
        with mock.patch("herdboard.store.os.replace", wraps=os.replace) as replace:
            path = self.store.ensure_inbox()
        self.assertEqual(replace.call_count, 1)
        self.assertEqual(sorted(p.name for p in path.iterdir()), ["board.toml"])

    def test_created_files_follow_the_umask_and_keep_their_mode(self):
        old = os.umask(0o022)
        self.addCleanup(os.umask, old)
        path = self.store.ensure_inbox()
        self.assertEqual((path / "board.toml").stat().st_mode & 0o777, 0o644)
        (path / "board.toml").chmod(0o600)
        self.store.add(path, "x")
        self.assertEqual((path / "board.toml").stat().st_mode & 0o777, 0o600)

    def test_locked_store_raises_conflict_and_writes_nothing(self):
        path = self.put("p")
        before = self.board_text(path)
        with open(self.todos / ".herdboard.lock", "a") as other:
            fcntl.flock(other, fcntl.LOCK_EX)
            with self.assertRaises(ConflictError) as caught:
                self.store.done("tabc")
            self.assertIn("locked", str(caught.exception))
            with self.assertRaises(ConflictError):
                self.store.add(path, "x")
        self.assertEqual(self.board_text(path), before)
        self.assertTrue(self.store.done("tabc").checked)

    def test_one_store_wide_lock_file(self):
        self.put("p")
        self.store.done("tabc")
        self.assertTrue((self.todos / ".herdboard.lock").exists())
        self.assertFalse(list(self.todos.rglob("*.tmp")))


class TriageColumnTests(StoreTestCase):
    def test_add_to_triage_inserts_the_column_first(self):
        path = self.put("p")
        view = self.store.add(path, "report the crash", column="triage")
        self.assertEqual(view.column, "Triage")
        self.assertEqual(list(self.columns(path)), ["Triage", "Todo", "Doing", "Done"])

    def test_add_without_a_column_skips_triage(self):
        path = self.put("p", [("Triage", []), ("Backlog", [])])
        self.assertEqual(self.store.add(path, "x").column, "Backlog")

    def test_a_board_with_only_triage_has_no_default_column(self):
        path = self.put("p", [("Triage", [])])
        with self.assertRaises(HerdboardError):
            self.store.add(path, "x")

    def test_move_to_another_board_from_triage_creates_triage_there(self):
        self.put("a", [("Triage", [{"id": "taaa", "title": "x"}]), ("Todo", [])])
        b = self.put("b", [("Todo", [])])
        self.assertEqual(self.store.move_to_board("taaa", b).column, "Triage")
        self.assertEqual(list(self.columns(b)), ["Triage", "Todo"])


class SnapshotTests(StoreTestCase):
    def test_snapshot_gives_columns_in_order_with_cards(self):
        path = self.put("p", [("Todo", [{"id": "taaa", "title": "x"}], {"limit": 3}), ("Done", [])])
        snapshot = self.store.snapshot(path)
        self.assertIsInstance(snapshot, BoardSnapshot)
        self.assertEqual((snapshot.name, snapshot.path), ("p", path))
        self.assertEqual(
            [(c.title, c.limit, c.completed) for c in snapshot.columns], [("Todo", 3, False), ("Done", None, True)]
        )
        self.assertEqual(snapshot.columns[0].cards[0].column_limit, 3)
        self.assertIsNone(snapshot.archive)

    def test_snapshot_with_the_archive(self):
        path = self.put("p")
        archive = self.store.snapshot(path, archive=True).archive
        self.assertEqual([c.ref for c in archive.cards], ["t005"])
        self.assertTrue(archive.cards[0].archived)


class ConfigTest(StoreTestCase):
    def test_merge_and_tables(self):
        from herdboard.linear_links import ProjectLink
        from herdboard.linear_pins import IssuePin

        path = self.put("p", [("Todo", [])])
        self.store.set_merge(path, "local")
        self.store.update_links(path, lambda links: [*links, ProjectLink("p1", "Proj")])
        self.store.update_pins(path, lambda pins: [*pins, IssuePin("i1", "ENG-1")])
        meta = self.store.board_meta(path)
        self.assertEqual(
            (meta.merge, [link.id for link in meta.links], [p.identifier for p in meta.pins]),
            ("local", ["p1"], ["ENG-1"]),
        )
        self.store.set_merge(path, None)
        self.assertIsNone(self.store.board_meta(path).merge)
        self.assertIn("[[linear-projects]]", self.board_text(path))

    def test_bad_merge_is_reported(self):
        path = self.put("p", [("Todo", [])], merge="sometimes")
        self.assertIn("merge must be local or pr", self.store.board_meta(path).error)


if __name__ == "__main__":
    import unittest

    unittest.main()
