import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdboard import activity, attachments, cli, mockup, plan, start
from herdboard.model import ConflictError, HerdboardError, NotFoundError
from herdboard.store import Store
from support.herdboard_store import StoreTestCase, write_board

DONE = ("Done", [{"id": "t222", "title": "two"}])


class ArchiveStoreTest(StoreTestCase):
    def test_archive_moves_the_folder_and_stamps_it(self):
        path = self.put("p", [("Todo", [{"id": "t111", "title": "one"}]), DONE])
        (path / "cards" / "t222" / "log.md").write_text("- log\n")
        card = self.store.archive("t222")
        self.assertTrue(card.archived)
        self.assertEqual(card.column, "Archive")
        self.assertFalse((path / "cards" / "t222").exists())
        self.assertTrue((path / "archive" / "t222" / "log.md").is_file())
        self.assertIn("archived = ", self.card_text(path, "t222", "archive"))
        self.assertEqual(self.columns(path)["Done"], [])
        self.assertNotIn("t222", [c.ref for c in self.store.list_cards()])

    def test_crash_before_the_board_write_hides_the_card_and_loses_nothing(self):
        path = self.put("p", [("Todo", []), DONE])
        with mock.patch.object(Store, "_write_board", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.store.archive("t222")
        self.assertEqual([c.ref for c in self.store.list_cards()], [])
        self.assertEqual([c.ref for c in self.store.archived_cards()], ["t222"])
        self.assertTrue(path.exists())

    def test_archived_cards_are_newest_first(self):
        path = self.put("p", [("Todo", [{"id": "t111", "title": "one"}]), DONE])
        self.store.archive("t111")
        file = path / "archive" / "t111" / "card.md"
        file.write_text(file.read_text().replace("archived = 20", "archived = 19"))
        self.store.archive("t222")
        self.assertEqual([c.ref for c in self.store.archived_cards()], ["t222", "t111"])

    def test_snapshot_has_the_archive_only_when_asked(self):
        path = self.put("p", [DONE], archive=[{"id": "t000", "title": "old"}])
        self.assertIsNone(self.store.snapshot(path).archive)
        self.assertEqual([c.ref for c in self.store.snapshot(path, archive=True).archive.cards], ["t000"])

    def test_show_finds_an_archived_card_only_when_asked(self):
        self.put("p", [DONE], archive=[{"id": "t000", "title": "old"}])
        with self.assertRaises(NotFoundError) as caught:
            self.store.show("t000")
        self.assertEqual(caught.exception.code, "archived")
        card = self.store.show("t000", archived=True)
        self.assertEqual((card.title, card.archived), ("old", True))

    def test_unarchive_moves_the_folder_to_the_done_column(self):
        path = self.put("p", [("Todo", []), ("Done", [])], archive=[{"id": "t000", "title": "old"}])
        card = self.store.unarchive("t000")
        self.assertEqual((card.column, card.checked, card.archived), ("Done", True, False))
        self.assertEqual(self.columns(path)["Done"], ["t000"])
        self.assertNotIn("archived", self.card_text(path, "t000"))
        self.assertFalse((path / "archive" / "t000").exists())

    def test_crash_after_the_unarchive_rename_leaves_an_orphan_that_shows(self):
        path = self.put("p", [("Todo", []), ("Done", [])], archive=[{"id": "t000", "title": "old"}])
        with mock.patch.object(Store, "_write_board", side_effect=OSError("disk full")), self.assertRaises(OSError):
            self.store.unarchive("t000")
        self.assertEqual([(c.ref, c.column) for c in self.store.list_cards()], [("t000", "Todo")])
        self.assertTrue(path.exists())

    def test_unarchive_needs_a_done_column(self):
        path = self.put("p", [("Todo", [])], archive=[{"id": "t000", "title": "old"}])
        with self.assertRaises(HerdboardError):
            self.store.unarchive("t000")
        self.assertTrue((path / "archive" / "t000").exists())

    def test_unarchive_of_a_card_that_is_not_archived(self):
        self.put("p", [DONE])
        with self.assertRaises(NotFoundError):
            self.store.unarchive("t222")

    def test_unarchive_of_a_sub_card_with_an_archived_parent(self):
        path = self.put(
            "p",
            [("Done", [])],
            archive=[{"id": "t111", "title": "parent"}, {"id": "t222", "title": "child", "parent": "t111"}],
        )
        self.assertEqual(self.store.archived_parent("t222").ref, "t111")
        self.assertIsNone(self.store.archived_parent("t111"))
        with self.assertRaises(HerdboardError) as caught:
            self.store.unarchive("t222")
        self.assertEqual(caught.exception.code, "parent_archived")
        self.store.unarchive("t222", with_parent=True)
        self.assertEqual(self.columns(path)["Done"], ["t111", "t222"])

    def test_commands_refuse_an_archived_card(self):
        self.put("p", [("Todo", []), ("Done", [])], archive=[{"id": "t000", "title": "old"}])
        for call in (
            lambda: self.store.move("t000", "Todo"),
            lambda: self.store.done("t000"),
            lambda: self.store.edit("t000", title="x"),
            lambda: self.store.delete("t000"),
            lambda: self.store.archive("t000"),
        ):
            with self.subTest(call=call), self.assertRaises(NotFoundError) as caught:
                call()
            self.assertEqual(caught.exception.code, "archived")

    def test_archive_refuses_a_card_that_the_check_blocks(self):
        path = self.put("p", [DONE])
        with self.assertRaises(HerdboardError) as caught:
            self.store.archive("t222", blocked=lambda ref: "its agent still runs")
        self.assertEqual(caught.exception.code, "card_active")
        self.assertTrue((path / "cards" / "t222").exists())

    def test_archive_refuses_a_parent_with_sub_cards_on_the_board(self):
        self.put("p", [("Todo", [{"id": "t111", "title": "p"}, {"id": "t222", "title": "c", "parent": "t111"}])])
        with self.assertRaises(HerdboardError) as caught:
            self.store.archive("t111")
        self.assertEqual(caught.exception.code, "has_sub_cards")

    def test_archive_done_archives_the_done_column(self):
        path = self.put(
            "p",
            [
                ("Todo", [{"id": "t111", "title": "open"}]),
                ("Done", [{"id": "t222", "title": "a"}, {"id": "t333", "title": "b"}]),
            ],
        )
        archived, skipped = self.store.archive_done(path, blocked=lambda ref: "busy" if ref == "t333" else None)
        self.assertEqual([c.ref for c in archived], ["t222"])
        self.assertEqual([(c.ref, why) for c, why in skipped], [("t333", "busy")])
        self.assertEqual(self.columns(path)["Done"], ["t333"])
        self.assertTrue((path / "archive" / "t222").is_dir())

    def test_archive_done_keeps_a_parent_whose_sub_cards_stay(self):
        path = self.put(
            "p",
            [
                ("Todo", [{"id": "t111", "title": "kid", "parent": "t222"}]),
                ("Done", [{"id": "t222", "title": "parent"}]),
            ],
        )
        archived, skipped = self.store.archive_done(path)
        self.assertEqual(archived, [])
        self.assertEqual([(c.ref, why) for c, why in skipped], [("t222", "it has sub-cards that stay on the board")])

    def test_archive_done_on_an_empty_done_column(self):
        path = self.put("p", [("Done", [])])
        self.assertEqual(self.store.archive_done(path), ([], []))

    def test_archive_done_stops_when_a_card_changed(self):
        path = self.put("p", [DONE])
        store = self.store

        def blocked(ref):
            store.edit(ref, title="changed")
            return None

        with self.assertRaises(ConflictError):
            store.archive_done(path, blocked=blocked)
        self.assertEqual(self.columns(path)["Done"], ["t222"])

    def test_a_log_entry_for_an_archived_card_lands_in_its_folder(self):
        path = self.put("p", [DONE])
        self.store.archive("t222")
        activity.append(self.store, "t222", "note", "user")
        self.assertIn("note", (path / "archive" / "t222" / "log.md").read_text())
        self.assertEqual([e["message"] for e in activity.read(self.config, "t222")], ["note"])

    def test_attachments_move_with_archive_and_unarchive(self):
        path = self.put("p", [DONE])
        source = self.todos.parent / "spec.md"
        source.write_text("spec")
        attachments.add(self.store, "t222", source)
        self.store.archive("t222")
        self.assertEqual([e["name"] for e in attachments.listing(self.config, "t222")], ["spec.md"])
        self.store.unarchive("t222")
        self.assertTrue((path / "cards" / "t222" / "attachments" / "spec.md").is_file())


class ArchiveBlockerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}))

    def record(self, **record):
        path = start.state_dir() / "starts.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"t111": record}))

    def test_a_card_without_a_start_record_is_free(self):
        self.assertIsNone(start.archive_blocker("t111"))

    def test_a_worktree_that_still_exists_blocks(self):
        worktree = self.root / "wt"
        worktree.mkdir()
        self.record(worktree_path=str(worktree))
        self.assertEqual(
            start.archive_blocker("t111"), f"its worktree still exists at {worktree}. Finish the card first"
        )

    def test_a_running_planner_blocks(self):
        path = start.state_dir() / "plans.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"t111": {"pane_id": "p9"}}))
        with mock.patch.object(plan, "_pane_exists", return_value=True) as exists:
            self.assertEqual(start.archive_blocker("t111"), "its planner still runs in pane p9")
        exists.assert_called_once_with("p9")
        with mock.patch.object(plan, "_pane_exists", return_value=False):
            self.assertIsNone(start.archive_blocker("t111"))

    def test_a_running_agent_blocks(self):
        self.record(worktree_path=str(self.root / "gone"), agent_name="hb-t111", pane_id="p1")
        with mock.patch.object(start, "agent_live", return_value=True) as live:
            self.assertEqual(start.archive_blocker("t111"), "its agent still runs")
        live.assert_called_once()
        with mock.patch.object(start, "agent_live", return_value=False):
            self.assertIsNone(start.archive_blocker("t111"))


class ArchiveCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(root / "state")}))
        self.todos = root / "todos"
        self.todos.mkdir()
        self.config = root / "config.toml"
        self.config.write_text(f'todos_dir = "{self.todos}"\n')
        self.plain = root / "plain"
        self.plain.mkdir()
        self.board = write_board(
            self.todos,
            "p",
            [
                ("Todo", [{"id": "t111", "title": "open"}]),
                ("Done", [{"id": "t222", "title": "a"}, {"id": "t333", "title": "b"}]),
            ],
        )
        self.blocked = self.enterContext(mock.patch.object(start, "archive_blocker", return_value=None))

    def run_cli(self, *args, expect=0):
        argv = ["--config", str(self.config), "-C", str(self.plain), *args]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        self.assertEqual(code, expect, f"stdout={out.getvalue()!r} stderr={err.getvalue()!r}")
        return out.getvalue(), err.getvalue()

    def run_json(self, *args, expect=0):
        return json.loads(self.run_cli("--json", *args, expect=expect)[0])

    def test_archive_and_unarchive_a_card(self):
        card = self.run_json("archive", "t111")["card"]
        self.assertEqual((card["ref"], card["column"], card["archived"]), ("t111", "Archive", True))
        self.blocked.assert_called_once_with("t111")
        out, _ = self.run_cli("unarchive", "t111")
        self.assertEqual(out, "t111 → Done (p)\n")
        self.assertEqual(self.run_json("show", "t111")["card"]["checked"], True)

    def test_archive_removes_the_mockup_drafts(self):
        for ref in ("t111", "t222"):
            mockup.drafts_dir(ref).mkdir(parents=True)
        self.run_json("archive", "t111")
        self.assertFalse(mockup.drafts_dir("t111").exists())
        self.run_json("archive", "--done", "--board", "p")
        self.assertFalse(mockup.drafts_dir("t222").exists())

    def test_archive_text_output(self):
        out, _ = self.run_cli("archive", "t111")
        self.assertEqual(out, "t111 → Archive (p)\n")

    def test_archive_logs_the_change(self):
        self.run_cli("archive", "t111")
        self.run_cli("unarchive", "t111")
        log = self.run_json("show", "t111")["log"]
        self.assertEqual([entry["message"] for entry in log], ["Archived.", "Unarchived."])

    def test_an_archived_card_takes_log_entries_but_no_moves(self):
        self.run_cli("archive", "t111")
        self.run_json("log", "t111", "note")
        error = self.run_json("move", "t111", "Todo", expect=1)["error"]
        self.assertEqual(error["code"], "archived")

    def test_archive_refuses_an_active_card(self):
        self.blocked.return_value = "its agent still runs"
        error = self.run_json("archive", "t111", expect=1)["error"]
        self.assertEqual(error, {"code": "card_active", "message": "Cannot archive t111: its agent still runs"})

    def test_archive_done_archives_the_done_column(self):
        self.blocked.side_effect = lambda ref: "its agent still runs" if ref == "t333" else None
        result = self.run_json("archive", "--done", "--board", "p")
        self.assertEqual([card["ref"] for card in result["archived"]], ["t222"])
        self.assertEqual(result["skipped"], [{"ref": "t333", "reason": "its agent still runs"}])
        out, _ = self.run_cli("archive", "--done", "--board", "p")
        self.assertEqual(out, "Archived 0 cards from p.\nKept t333: its agent still runs\n")

    def test_archive_needs_a_ref_or_done(self):
        for args in (("archive",), ("archive", "t111", "--done"), ("archive", "t111", "--board", "p")):
            with self.subTest(args=args):
                self.assertEqual(self.run_json(*args, expect=1)["error"]["code"], "usage")

    def test_archive_done_in_a_repository_without_a_board(self):
        repo = self.plain.parent / "api"
        repo.mkdir()
        subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
        subprocess.run(
            ["git", "-C", str(repo), "remote", "add", "origin", "https://github.com/acme/api.git"], check=True
        )
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["--config", str(self.config), "-C", str(repo), "--json", "archive", "--done"])
        self.assertEqual(code, 0)
        result = json.loads(out.getvalue())
        self.assertEqual(result["archived"], [])
        self.assertTrue((self.todos / "boards/acme-api/board.toml").exists())
        self.assertFalse((self.todos / "boards/inbox").exists())

    def test_archive_done_needs_a_done_column(self):
        write_board(self.todos, "q", [("Todo", [])])
        error = self.run_json("archive", "--done", "--board", "q", expect=1)["error"]
        self.assertIn("no done column", error["message"])

    def test_list_archived_refuses_linear_and_column_filters(self):
        for args in (("--source", "linear"), ("--column", "Done")):
            with self.subTest(args=args):
                error = self.run_json("list", "--board", "p", "--archived", *args, expect=1)["error"]
                self.assertEqual(error["code"], "usage")

    def family(self):
        write_board(
            self.todos,
            "f",
            [("Done", [])],
            archive=[{"id": "t444", "title": "parent"}, {"id": "t555", "title": "child", "parent": "t444"}],
        )
        return self.todos / "boards" / "f"

    def test_unarchive_declined_in_a_terminal(self):
        board = self.family()
        before = (board / "board.toml").read_text()
        with (
            mock.patch.object(cli, "_interactive", return_value=True),
            mock.patch.object(sys, "stdin", io.StringIO("n\n")),
        ):
            out, err = self.run_cli("unarchive", "t555")
        self.assertEqual(out, "Nothing unarchived.\n")
        self.assertIn("t444", err)
        self.assertEqual((board / "board.toml").read_text(), before)
        self.assertTrue((board / "archive" / "t555").is_dir())

    def test_list_archived(self):
        self.run_cli("archive", "t222")
        cards = self.run_json("list", "--board", "p", "--archived")["cards"]
        self.assertEqual([(c["ref"], c["column"]) for c in cards], [("t222", "Archive")])
        self.assertNotIn("t222", [c["ref"] for c in self.run_json("list", "--board", "p")["cards"]])

    def test_commands_with_side_effects_refuse_an_archived_card(self):
        self.run_cli("archive", "t111")
        attachment = self.todos / "spec.md"
        attachment.write_text("spec")
        for args in (
            ("start", "t111"),
            ("plan", "t111"),
            ("review", "t111"),
            ("merge", "t111"),
            ("attach", "t111", str(attachment)),
            ("attach", "t111", "--remove", "spec.md"),
        ):
            with self.subTest(args=args):
                self.assertEqual(self.run_json(*args, expect=1)["error"]["code"], "archived")
        self.assertEqual(self.run_json("show", "t111")["attachments"], [])
        self.assertFalse((start.state_dir() / "starts.json").exists())

    def test_unarchive_of_a_sub_card_asks_to_unarchive_the_parent(self):
        self.family()
        error = self.run_json("unarchive", "t555", expect=1)["error"]
        self.assertEqual(error["code"], "confirmation_required")
        self.assertIn("t444", error["message"])
        out, _ = self.run_cli("unarchive", "t555", "--yes")
        self.assertEqual(out, "t555 → Done (f)\nAlso unarchived the parent t444.\n")
        self.assertEqual(self.run_json("show", "t444")["log"][-1]["message"], "Unarchived.")

    def test_show_and_log_take_an_archived_card(self):
        self.run_cli("archive", "t111")
        self.assertEqual(self.run_json("show", "t111")["card"]["title"], "open")
        self.run_json("log", "t111", "note")
        self.assertEqual(self.run_json("show", "t111")["log"][-1]["message"], "note")

    def test_linear_cards_are_not_archived(self):
        for command in ("archive", "unarchive"):
            error = self.run_json(command, "ENG-12", expect=1)["error"]
            self.assertEqual(error["code"], "linear_archive")


if __name__ == "__main__":
    unittest.main()
