import contextlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import cli, kanban_md, plan, start  # noqa: E402
from herdboard.config import Config  # noqa: E402
from herdboard.model import ConflictError, HerdboardError, NotFoundError  # noqa: E402
from herdboard.store import Store  # noqa: E402

SETTINGS = '%% kanban:settings\n```\n{"kanban-plugin":"board"}\n```\n%%'
HEAD = "---\n\nkanban-plugin: board\n\n---\n\n"


def board_text(*columns: str, archive: str = "") -> str:
    return HEAD + "".join(columns) + archive + "\n\n" + SETTINGS


def col(title: str, *lines: str) -> str:
    return f"## {title}\n\n" + "".join(f"{line}\n" for line in lines) + "\n\n"


class ArchiveStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.todos = Path(self.tmp.name) / "todos"
        (self.todos / "projects").mkdir(parents=True)
        self.store = Store(Config(todos_dir=self.todos), lock_timeout=0.2)

    def put(self, text):
        path = self.todos / "projects/p.md"
        path.write_text(text)
        return path

    def test_archive_adds_the_obsidian_archive_section(self):
        path = self.put(board_text(col("Todo", "- [ ] one ^t111"), col("Done", "**Complete**", "- [x] two ^t222")))
        card = self.store.archive("t222")
        self.assertTrue(card.archived)
        self.assertEqual(card.column, "Archive")
        text = path.read_text()
        self.assertIn("\n***\n\n## Archive\n\n- [x] two ^t222\n", text)
        self.assertTrue(text.endswith(SETTINGS))
        board = kanban_md.parse(text)
        self.assertEqual([c.title for c in board.columns], ["Todo", "Done"])
        self.assertEqual([c.title for c in board.archive.cards], ["two"])
        self.assertNotIn("t222", [c.ref for c in self.store.list_cards()])

    def test_archive_appends_to_an_existing_archive(self):
        path = self.put(board_text(
            col("Todo", "- [ ] one ^t111"), col("Done"),
            archive="***\n\n## Archive\n\n- [x] old ^t000\n\n",
        ))
        self.store.archive("t111")
        board = kanban_md.parse(path.read_text())
        self.assertEqual([c.block_id for c in board.archive.cards], ["t000", "t111"])
        self.assertEqual(board.archive_lines, ["***", ""])

    def test_archive_gives_an_id_to_a_card_without_one(self):
        path = self.put(board_text(col("Todo", "- [ ] one"), col("Done")))
        card = self.store.archive("p:1")
        self.assertRegex(card.ref, r"^t[0-9a-z]{3}$")
        self.assertEqual(kanban_md.parse(path.read_text()).archive.cards[0].block_id, card.ref)

    def test_archived_cards_lists_the_archive(self):
        self.put(board_text(col("Todo", "- [ ] one ^t111"), archive="***\n\n## Archive\n\n- [x] old ^t000\n- [x] no id\n\n"))
        cards = self.store.archived_cards()
        self.assertEqual([(c.ref, c.title, c.archived, c.column) for c in cards],
                         [("t000", "old", True, "Archive"), ("p:archive-2", "no id", True, "Archive")])

    def test_snapshot_has_the_archive(self):
        path = self.put(board_text(col("Todo"), archive="***\n\n## Archive\n\n- [x] old ^t000\n\n"))
        snap = self.store.snapshot(path)
        self.assertEqual([c.title for c in snap.columns], ["Todo"])
        self.assertEqual([c.ref for c in snap.archive.cards], ["t000"])
        self.assertIsNone(self.store.snapshot(self.put(board_text(col("Todo")))).archive)

    def test_show_finds_an_archived_card_only_when_asked(self):
        self.put(board_text(col("Todo"), archive="***\n\n## Archive\n\n- [x] old ^t000\n\n"))
        card = self.store.show("t000", archived=True)
        self.assertEqual((card.title, card.archived, card.column), ("old", True, "Archive"))
        self.assertEqual(self.store.ensure_id("t000", archived=True).ref, "t000")
        for call in (lambda: self.store.show("t000"), lambda: self.store.ensure_id("t000")):
            with self.assertRaises(NotFoundError) as caught:
                call()
            self.assertEqual(caught.exception.code, "archived")

    def test_show_and_ensure_id_take_an_archive_reference(self):
        path = self.put(board_text(col("Done"), archive="***\n\n## Archive\n\n- [x] old ^t000\n- [x] no id\n\n"))
        card = self.store.show("p:archive-2", archived=True)
        self.assertEqual((card.ref, card.title, card.archived), ("p:archive-2", "no id", True))
        with self.assertRaises(NotFoundError):
            self.store.show("p:archive-2")
        given = self.store.ensure_id("p:archive-2", archived=True)
        self.assertRegex(given.ref, r"^t[0-9a-z]{3}$")
        self.assertTrue(given.archived)
        board = kanban_md.parse(path.read_text())
        self.assertEqual([c.block_id for c in board.archive.cards], ["t000", given.ref])

    def test_archive_by_temporary_reference_checks_the_card_id(self):
        self.put(board_text(col("Todo", "- [ ] one ^t111"), col("Done")))
        with self.assertRaises(HerdboardError) as caught:
            self.store.archive("p:1", blocked=lambda ref: "its agent still runs" if ref == "t111" else None)
        self.assertEqual(caught.exception.code, "card_active")

    def test_unarchive_a_card_without_an_id_by_its_archive_reference(self):
        path = self.put(board_text(col("Done", "**Complete**"), archive="***\n\n## Archive\n\n- [x] old ^t000\n- [ ] no id\n\n"))
        card = self.store.unarchive("p:archive-2")
        self.assertRegex(card.ref, r"^t[0-9a-z]{3}$")
        self.assertEqual((card.title, card.column, card.checked), ("no id", "Done", True))
        board = kanban_md.parse(path.read_text())
        self.assertEqual([c.block_id for c in board.column("Done").cards], [card.ref])
        with self.assertRaises(NotFoundError):
            self.store.unarchive("p:archive-5")

    def test_unarchive_by_archive_reference_stops_when_the_card_changed(self):
        self.put(board_text(col("Done", "**Complete**"), archive="***\n\n## Archive\n\n- [x] no id\n\n"))
        original = self.store._read

        def changed(read_path):
            board = original(read_path)
            if self.store.lock_path.exists():
                board.archive.cards[0].lines[0] = "- [x] edited"
            return board

        self.store._read = changed
        with self.assertRaises(ConflictError):
            self.store.unarchive("p:archive-1")

    def test_unarchive_of_a_sub_card_with_an_archived_parent(self):
        path = self.put(board_text(
            col("Done", "**Complete**"),
            archive="***\n\n## Archive\n\n- [x] parent ^t111\n- [x] child [[#^t111]] ^t222\n\n",
        ))
        self.assertEqual(self.store.archived_parent("t222").ref, "t111")
        self.assertIsNone(self.store.archived_parent("t111"))
        before = path.read_text()
        with self.assertRaises(HerdboardError) as caught:
            self.store.unarchive("t222")
        self.assertEqual(caught.exception.code, "parent_archived")
        self.assertIn("t111", str(caught.exception))
        self.assertEqual(path.read_text(), before)
        card = self.store.unarchive("t222", with_parent=True)
        self.assertEqual(card.column, "Done")
        board = kanban_md.parse(path.read_text())
        self.assertEqual([c.block_id for c in board.column("Done").cards], ["t111", "t222"])
        self.assertEqual(board.archive.cards, [])

    def test_commands_refuse_an_archived_card(self):
        path = self.put(board_text(col("Todo"), col("Done"), archive="***\n\n## Archive\n\n- [x] old ^t000\n\n"))
        before = path.read_text()
        for call in (lambda: self.store.move("t000", "Todo"), lambda: self.store.done("t000"),
                     lambda: self.store.delete("t000"), lambda: self.store.archive("t000")):
            with self.assertRaises(NotFoundError) as caught:
                call()
            self.assertEqual(caught.exception.code, "archived")
            self.assertIn("unarchive", str(caught.exception))
        self.assertEqual(path.read_text(), before)

    def test_unarchive_moves_the_card_to_the_done_column(self):
        path = self.put(board_text(
            col("Todo"), col("Done", "**Complete**", "- [x] two ^t222"),
            archive="***\n\n## Archive\n\n- [ ] old ^t000\n\n",
        ))
        card = self.store.unarchive("t000")
        self.assertEqual((card.column, card.checked, card.archived), ("Done", True, False))
        board = kanban_md.parse(path.read_text())
        self.assertEqual([c.block_id for c in board.column("Done").cards], ["t222", "t000"])
        self.assertEqual(board.archive.cards, [])

    def test_unarchive_of_a_card_that_is_not_archived(self):
        self.put(board_text(col("Todo", "- [ ] one ^t111"), col("Done")))
        with self.assertRaises(NotFoundError):
            self.store.unarchive("t111")
        with self.assertRaises(NotFoundError):
            self.store.unarchive("tzzz")

    def test_archive_refuses_a_card_that_the_check_blocks(self):
        path = self.put(board_text(col("Todo", "- [ ] one ^t111"), col("Done")))
        before = path.read_text()
        seen = []

        def blocked(ref):
            seen.append(ref)
            return "its worktree still exists"

        with self.assertRaises(HerdboardError) as caught:
            self.store.archive("t111", blocked=blocked)
        self.assertEqual(caught.exception.code, "card_active")
        self.assertIn("its worktree still exists", str(caught.exception))
        self.assertEqual(seen, ["t111"])
        self.assertEqual(path.read_text(), before)

    def test_archive_refuses_a_parent_with_sub_cards_on_the_board(self):
        self.put(board_text(col("Todo", "- [ ] parent ^t111", "- [ ] child [[#^t111]] ^t222"), col("Done")))
        with self.assertRaises(HerdboardError) as caught:
            self.store.archive("t111")
        self.assertEqual(caught.exception.code, "has_sub_cards")
        self.store.archive("t222")
        self.store.archive("t111")

    def test_archive_done_archives_the_done_column(self):
        path = self.put(board_text(
            col("Todo", "- [ ] open ^t111"),
            col("Done", "**Complete**", "- [x] a ^t222", "- [x] b", "\tdetail", "- [x] busy ^t333"),
        ))
        archived, skipped = self.store.archive_done(
            path, blocked=lambda ref: "its agent still runs" if ref == "t333" else None)
        self.assertEqual([c.title for c in archived], ["a", "b"])
        self.assertEqual([(c.ref, reason) for c, reason in skipped], [("t333", "its agent still runs")])
        board = kanban_md.parse(path.read_text())
        self.assertEqual([c.title for c in board.column("Done").cards], ["busy"])
        self.assertEqual([c.title for c in board.archive.cards], ["a", "b"])
        self.assertEqual(board.archive.cards[1].body, ["\tdetail"])
        self.assertRegex(board.archive.cards[1].block_id, r"^t[0-9a-z]{3}$")

    def test_archive_done_keeps_a_parent_whose_sub_cards_stay(self):
        path = self.put(board_text(
            col("Todo", "- [ ] open child [[#^t111]] ^t444"),
            col("Done", "**Complete**", "- [x] parent ^t111", "- [x] other ^t555",
                "- [x] done child [[#^t555]] ^t666"),
        ))
        archived, skipped = self.store.archive_done(path)
        self.assertEqual([c.ref for c in archived], ["t555", "t666"])
        self.assertEqual([(c.ref, reason) for c, reason in skipped],
                         [("t111", "it has sub-cards that stay on the board")])
        board = kanban_md.parse(path.read_text())
        self.assertEqual([c.block_id for c in board.column("Done").cards], ["t111"])

    def test_archive_done_on_an_empty_done_column(self):
        path = self.put(board_text(col("Todo", "- [ ] open ^t111"), col("Done", "**Complete**")))
        before = path.read_text()
        self.assertEqual(self.store.archive_done(path), ([], []))
        self.assertEqual(path.read_text(), before)

    def test_archive_done_checks_sub_cards_again_under_the_lock(self):
        path = self.put(board_text(col("Todo"), col("Done", "**Complete**", "- [x] parent ^t111", "- [x] other ^t222")))
        original = self.store._read

        def child_added(read_path):
            board = original(read_path)
            if self.store.lock_path.exists():
                kanban_md.insert_card(board.column("Todo"), kanban_md.Card(["- [ ] new child [[#^t111]] ^t333"]))
            return board

        self.store._read = child_added
        archived, skipped = self.store.archive_done(path)
        self.assertEqual([c.ref for c in archived], ["t222"])
        self.assertEqual([(c.ref, reason) for c, reason in skipped],
                         [("t111", "it has sub-cards that stay on the board")])

    def test_archive_done_stops_when_a_card_changed(self):
        path = self.put(board_text(col("Done", "**Complete**", "- [x] a ^t222")))
        original = self.store._read

        def changed(read_path):
            board = original(read_path)
            if self.store.lock_path.exists():
                board.columns[0].cards[0].lines[0] = "- [x] edited ^t222"
            return board

        self.store._read = changed
        with self.assertRaises(ConflictError):
            self.store.archive_done(path)



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
        self.assertEqual(start.archive_blocker("t111"),
                         f"its worktree still exists at {worktree}. Finish the card first")

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
        (self.todos / "projects").mkdir(parents=True)
        self.config = root / "config.toml"
        self.config.write_text(f'todos_dir = "{self.todos}"\n')
        self.plain = root / "plain"
        self.plain.mkdir()
        self.board = self.todos / "projects/p.md"
        self.board.write_text(board_text(
            col("Todo", "- [ ] open ^t111"),
            col("Done", "**Complete**", "- [x] a ^t222", "- [x] b ^t333"),
        ))
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

    def test_list_archived(self):
        self.run_cli("archive", "t222")
        cards = self.run_json("list", "--board", "p", "--archived")["cards"]
        self.assertEqual([(c["ref"], c["column"]) for c in cards], [("t222", "Archive")])
        self.assertNotIn("t222", [c["ref"] for c in self.run_json("list", "--board", "p")["cards"]])

    def test_commands_with_side_effects_refuse_an_archived_card(self):
        self.run_cli("archive", "t111")
        attachment = self.todos / "spec.md"
        attachment.write_text("spec")
        for args in (("start", "t111"), ("plan", "t111"), ("review", "t111"), ("merge", "t111"),
                     ("attach", "t111", str(attachment)), ("attach", "t111", "--remove", "spec.md")):
            with self.subTest(args=args):
                self.assertEqual(self.run_json(*args, expect=1)["error"]["code"], "archived")
        self.assertEqual(self.run_json("show", "t111")["attachments"], [])
        self.assertFalse((start.state_dir() / "starts.json").exists())

    def test_unarchive_of_a_sub_card_asks_to_unarchive_the_parent(self):
        self.board.write_text(board_text(
            col("Done", "**Complete**"),
            archive="***\n\n## Archive\n\n- [x] parent ^t111\n- [x] child [[#^t111]] ^t222\n\n",
        ))
        error = self.run_json("unarchive", "t222", expect=1)["error"]
        self.assertEqual(error["code"], "confirmation_required")
        self.assertIn("t111", error["message"])
        out, _ = self.run_cli("unarchive", "t222", "--yes")
        self.assertEqual(out, "t222 → Done (p)\nAlso unarchived the parent t111.\n")
        self.assertEqual(self.run_json("show", "t111")["log"][-1]["message"], "Unarchived.")

    def test_show_and_log_take_an_archive_reference(self):
        self.board.write_text(board_text(col("Done", "**Complete**"), archive="***\n\n## Archive\n\n- [x] no id\n\n"))
        self.assertEqual(self.run_json("show", "p:archive-1")["card"]["title"], "no id")
        ref = self.run_json("log", "p:archive-1", "note")["ref"]
        self.assertRegex(ref, r"^t[0-9a-z]{3}$")
        self.assertEqual(self.run_json("show", ref)["log"][-1]["message"], "note")

    def test_linear_cards_are_not_archived(self):
        for command in ("archive", "unarchive"):
            error = self.run_json(command, "ENG-12", expect=1)["error"]
            self.assertEqual(error["code"], "linear_archive")


if __name__ == "__main__":
    unittest.main()
