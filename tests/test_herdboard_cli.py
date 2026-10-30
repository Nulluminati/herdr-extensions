import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "plugins/herdboard/src"
sys.path.insert(0, str(SRC))

from herdboard import cli  # noqa: E402
from herdboard.model import ConflictError  # noqa: E402
from herdboard.store import Store  # noqa: E402
from support.herdboard_cli import CliTestCase  # noqa: E402


class CommandTests(CliTestCase):
    def test_add_text_goes_to_inbox_outside_git(self):
        out, _ = self.run_cli("add", "buy milk", directory=self.plain)
        self.assertRegex(out, r"^t[0-9a-z]{3} → Backlog \(inbox\)\n$")
        self.assertNotIn("herdboard init", out)

    def test_add_json(self):
        data = self.run_json("add", "buy milk", "--column", "Todo", directory=self.plain)
        card = data["card"]
        self.assertEqual(card["title"], "buy milk")
        self.assertEqual(card["column"], "Todo")
        self.assertEqual(card["board"], "inbox")
        self.assertTrue(card["path"].endswith("inbox.md"))
        self.assertNotIn("hint", data)

    def test_add_to_triage_creates_the_column(self):
        card = self.run_json("add", "harness bug", "--column", "Triage", directory=self.plain)["card"]
        self.assertEqual((card["board"], card["column"]), ("inbox", "Triage"))
        data = self.run_json("list", directory=self.plain)
        self.assertEqual([c["title"] for c in data["boards"][0]["columns"]],
                         ["Triage", "Backlog", "Todo", "In progress", "Review", "Done"])

    def test_add_with_body(self):
        card = self.run_json("add", "notes", "--body", "line one\nline two\n")["card"]
        self.assertEqual(card["body"], ["\tline one", "\tline two"])
        self.assertEqual(self.run_json("show", card["ref"])["card"]["body"], card["body"])

    def test_add_with_empty_body(self):
        card = self.run_json("add", "notes", "--body", "")["card"]
        self.assertEqual(card["body"], [])

    def test_add_body_from_file(self):
        body = self.plain / "body.txt"
        body.write_text("line one\nline two é\n", encoding="utf-8")
        for path in (str(body), "body.txt"):
            with self.subTest(path=path):
                card = self.run_json("add", "notes", "--body-file", path,
                                     directory=self.plain)["card"]
                self.assertEqual(card["body"], ["\tline one", "\tline two é"])

    def test_add_body_options_are_mutually_exclusive(self):
        data = self.run_json("add", "notes", "--body", "", "--body-file", "missing.txt", expect=1)
        self.assertEqual(data["error"]["code"], "usage")
        self.assertEqual(self.run_json("list")["cards"], [])

    def test_add_unreadable_body_file_creates_no_card(self):
        data = self.run_json("add", "notes", "--body-file", "missing.txt", expect=1)
        self.assertEqual(data["error"]["code"], "not_found")
        self.assertEqual(self.run_json("list")["cards"], [])

    def test_add_subcard_with_body(self):
        parent = self.run_json("add", "parent")["card"]
        card = self.run_json("add", "child", "--parent", parent["ref"],
                             "--body", "child detail")["card"]
        self.assertEqual(card["parent"], parent["ref"])
        self.assertEqual(card["body"], ["\tchild detail"])

    def test_card_agent_can_add_body_but_cannot_edit_it(self):
        with mock.patch.dict(os.environ, {"HERDR_PANE_ID": "w1:p2"}), \
                mock.patch.object(cli, "card_agent_panes", return_value={"w1:p2"}):
            card = self.run_json("add", "notes", "--body", "original")["card"]
            data = self.run_json("edit", card["ref"], "--body", "changed", expect=1)
            self.assertEqual(data["error"]["code"], "agent_edit_denied")
            self.assertEqual(self.run_json("show", card["ref"])["card"]["body"], ["\toriginal"])

    def test_add_position_top(self):
        self.run_cli("add", "first", directory=self.plain)
        self.run_cli("add", "second", "--position", "top", directory=self.plain)
        out, _ = self.run_cli("list", directory=self.plain)
        self.assertLess(out.index("second"), out.index("first"))

    def test_list_text_and_empty(self):
        out, _ = self.run_cli("list", directory=self.plain)
        self.assertEqual(out, "No cards.\n")
        self.run_cli("add", "buy milk 📅 2026-01-02", "--column", "Todo", directory=self.plain)
        out, _ = self.run_cli("list", directory=self.plain)
        lines = out.splitlines()
        self.assertEqual(lines[0], "## Todo")
        self.assertRegex(lines[1], r"^  t[0-9a-z]{3}  buy milk 📅 2026-01-02$")
        self.assertEqual(len(lines), 2)
        self.assertNotIn("Backlog", out)

    def test_list_json_and_column_filter(self):
        self.run_cli("add", "a", "--column", "Todo", directory=self.plain)
        self.run_cli("add", "b", "--column", "Review", directory=self.plain)
        data = self.run_json("list", "--column", "review", directory=self.plain)
        self.assertEqual([card["title"] for card in data["cards"]], ["b"])
        self.assertEqual(
            set(data["cards"][0]),
            {
                "ref", "board", "path", "column", "title", "checked", "due", "tags", "body", "type",
                "column_limit", "source", "url", "state", "team", "pr", "read_only", "fetched_at",
                "stale", "parent", "archived",
            },
        )
        self.assertEqual(data["cards"][0]["source"], "todo")
        self.assertNotIn("linear", data)

    def test_list_json_names_every_column_of_each_board(self):
        self.run_cli("add", "a", "--column", "Todo", directory=self.plain)
        data = self.run_json("list", "--column", "review", directory=self.plain)
        self.assertEqual(data["cards"], [])
        self.assertEqual(len(data["boards"]), 1)
        board = data["boards"][0]
        self.assertEqual(board["name"], "inbox")
        self.assertTrue(board["path"].endswith("inbox.md"))
        self.assertEqual(
            board["columns"],
            [
                {"title": "Backlog", "limit": None, "done": False},
                {"title": "Todo", "limit": None, "done": False},
                {"title": "In progress", "limit": None, "done": False},
                {"title": "Review", "limit": None, "done": False},
                {"title": "Done", "limit": None, "done": True},
            ],
        )

    def test_list_json_without_a_board_has_no_boards(self):
        data = self.run_json("list", directory=self.plain)
        self.assertEqual(data["boards"], [])

    def test_list_json_with_source_linear_has_no_todo_boards(self):
        self.run_cli("add", "a", directory=self.plain)
        data = self.run_json("list", "--source", "linear", directory=self.plain)
        self.assertEqual(data["boards"], [])

    def test_edit_title_and_body(self):
        card = self.run_json("add", "buy milk", directory=self.plain)["card"]
        data = self.run_json("edit", card["ref"], "--title", "buy oat milk #errand",
                             "--body", "two litres\nfrom the market", directory=self.plain)
        self.assertEqual(data["card"]["title"], "buy oat milk #errand")
        self.assertEqual(data["card"]["body"], ["\ttwo litres", "\tfrom the market"])
        out, _ = self.run_cli("edit", card["ref"], "--body", "", directory=self.plain)
        self.assertEqual(out, f"{card['ref']} edited (inbox / Backlog)\n")
        self.assertEqual(self.run_json("show", card["ref"], directory=self.plain)["card"]["body"], [])

    def test_edit_body_from_a_file(self):
        card = self.run_json("add", "notes", directory=self.plain)["card"]
        body = self.plain / "body.txt"
        body.write_text("line one\nline two\n")
        data = self.run_json("edit", card["ref"], "--body-file", str(body), directory=self.plain)
        self.assertEqual(data["card"]["body"], ["\tline one", "\tline two"])

    def test_edit_needs_a_change(self):
        card = self.run_json("add", "notes", directory=self.plain)["card"]
        data = self.run_json("edit", card["ref"], directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_add_with_a_type(self):
        card = self.run_json("add", "fix login #auth", "--type", "Bug", directory=self.plain)["card"]
        self.assertEqual(card["title"], "fix login #type/bug #auth")
        self.assertEqual(card["type"], "bug")
        self.assertEqual(card["tags"], ["auth"])

    def test_a_type_tag_in_the_text_is_the_type(self):
        card = self.run_json("add", "fix login #type/chore", directory=self.plain)["card"]
        self.assertEqual((card["type"], card["tags"]), ("chore", []))

    def test_add_refuses_an_unknown_type(self):
        data = self.run_json("add", "fix login", "--type", "epic", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "unknown_type")
        self.assertIn("bug, feature, chore, docs, refactor", data["error"]["message"])

    def test_types_come_from_the_config(self):
        self.config.write_text(f'todos_dir = "{self.todos}"\ntypes = ["spike"]\n')
        card = self.run_json("add", "try it", "--type", "spike", directory=self.plain)["card"]
        self.assertEqual(card["type"], "spike")
        data = self.run_json("add", "fix", "--type", "bug", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "unknown_type")

    def test_edit_the_type(self):
        card = self.run_json("add", "fix login #auth", directory=self.plain)["card"]
        data = self.run_json("edit", card["ref"], "--type", "feature", directory=self.plain)
        self.assertEqual(data["card"]["title"], "fix login #type/feature #auth")
        data = self.run_json("edit", card["ref"], "--type", "bug", "--body", "detail", directory=self.plain)
        self.assertEqual((data["card"]["title"], data["card"]["body"]), ("fix login #type/bug #auth", ["\tdetail"]))
        data = self.run_json("edit", card["ref"], "--no-type", directory=self.plain)
        self.assertEqual((data["card"]["title"], data["card"]["type"]), ("fix login #auth", None))

    def test_edit_title_and_type(self):
        card = self.run_json("add", "fix login", directory=self.plain)["card"]
        data = self.run_json("edit", card["ref"], "--title", "fix logout #web", "--type", "bug",
                             directory=self.plain)
        self.assertEqual(data["card"]["title"], "fix logout #type/bug #web")

    def test_edit_refuses_an_unknown_type(self):
        card = self.run_json("add", "fix login", directory=self.plain)["card"]
        data = self.run_json("edit", card["ref"], "--type", "epic", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "unknown_type")

    def test_show_the_type(self):
        card = self.run_json("add", "fix login", "--type", "bug", directory=self.plain)["card"]
        out, _ = self.run_cli("show", card["ref"], directory=self.plain)
        self.assertIn("type: bug", out.splitlines())
        self.assertNotIn("tags: type/bug", out)

    def test_edit_refuses_a_linear_issue(self):
        data = self.run_json("edit", "ENG-12", "--title", "x", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "linear_edit")

    def test_move_to_another_board(self):
        board = self.init_repo_board()
        card = self.run_json("add", "buy milk", "--column", "Todo", directory=self.plain)["card"]
        data = self.run_json("move", card["ref"], "--board", Path(board).stem, directory=self.plain)
        self.assertEqual((data["card"]["board"], data["card"]["column"]), (Path(board).stem, "Todo"))
        self.assertEqual(data["card"]["ref"], card["ref"])
        data = self.run_json("move", card["ref"], "Review", "--board", "inbox", directory=self.plain)
        self.assertEqual((data["card"]["board"], data["card"]["column"]), ("inbox", "Review"))

    def test_move_up_and_down(self):
        refs = [self.run_json("add", t, directory=self.plain)["card"]["ref"] for t in ("a", "b", "c")]
        self.run_json("move", refs[2], "--up", directory=self.plain)
        titles = [c["title"] for c in self.run_json("list", directory=self.plain)["cards"]]
        self.assertEqual(titles, ["a", "c", "b"])
        self.run_json("move", refs[0], "--down", directory=self.plain)
        titles = [c["title"] for c in self.run_json("list", directory=self.plain)["cards"]]
        self.assertEqual(titles, ["c", "a", "b"])

    def test_up_does_not_mix_with_a_column(self):
        ref = self.run_json("add", "a", directory=self.plain)["card"]["ref"]
        data = self.run_json("move", ref, "Todo", "--up", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_move_needs_a_column_or_a_board(self):
        card = self.run_json("add", "buy milk", directory=self.plain)["card"]
        data = self.run_json("move", card["ref"], directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_a_linear_issue_cannot_change_board(self):
        data = self.run_json("move", "ENG-1", "Todo", "--board", "inbox", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_card_changes_update_the_sidebar_card(self):
        from herdboard import start as start_module
        card = self.run_json("add", "buy milk", directory=self.plain)["card"]
        with mock.patch.object(start_module, "update_card_token") as update:
            self.run_json("move", card["ref"], "Todo", directory=self.plain)
            self.run_json("edit", card["ref"], "--title", "buy oat milk", directory=self.plain)
            self.run_json("done", card["ref"], directory=self.plain)
            self.run_json("delete", card["ref"], "--yes", directory=self.plain)
        columns = [(call.args[0].column, call.kwargs.get("removed", False)) for call in update.call_args_list]
        self.assertEqual(columns, [("Todo", False), ("Todo", False), ("Done", False), ("Done", True)])

    def test_list_does_not_touch_the_sidebar(self):
        from herdboard import start as start_module
        self.run_json("add", "buy milk", directory=self.plain)
        with mock.patch.object(start_module, "update_card_token") as update:
            self.run_json("list", directory=self.plain)
        update.assert_not_called()

    def test_delete_with_yes(self):
        card = self.run_json("add", "buy milk", directory=self.plain)["card"]
        data = self.run_json("delete", card["ref"], "--yes", directory=self.plain)
        self.assertEqual(data["deleted"]["title"], "buy milk")
        self.assertEqual(self.run_json("list", directory=self.plain)["cards"], [])
        out, _ = self.run_cli("add", "eggs", directory=self.plain)
        ref = out.split()[0]
        out, _ = self.run_cli("delete", ref, "--yes", directory=self.plain)
        self.assertEqual(out, f"Deleted {ref}: eggs (inbox / Backlog)\n")

    def test_delete_without_a_terminal_needs_yes(self):
        card = self.run_json("add", "buy milk", directory=self.plain)["card"]
        data = self.run_json("delete", card["ref"], directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "confirmation_required")
        self.assertEqual(len(self.run_json("list", directory=self.plain)["cards"]), 1)

    def test_delete_asks_in_a_terminal(self):
        card = self.run_json("add", "buy milk", directory=self.plain)["card"]
        with mock.patch.object(cli, "_interactive", return_value=True), \
                mock.patch.object(cli, "_confirm", return_value=False):
            out, _ = self.run_cli("delete", card["ref"], directory=self.plain)
        self.assertEqual(out, "Nothing deleted.\n")
        self.assertEqual(len(self.run_json("list", directory=self.plain)["cards"]), 1)
        with mock.patch.object(cli, "_interactive", return_value=True), \
                mock.patch.object(cli, "_confirm", return_value=True):
            self.run_cli("delete", card["ref"], directory=self.plain)
        self.assertEqual(self.run_json("list", directory=self.plain)["cards"], [])

    def test_delete_refuses_a_linear_issue(self):
        data = self.run_json("delete", "ENG-12", "--yes", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "linear_delete")

    def test_show_text_and_json(self):
        card = self.run_json("add", "buy #errand 📅 2026-01-02", directory=self.plain)["card"]
        out, _ = self.run_cli("show", card["ref"], directory=self.plain)
        lines = out.splitlines()
        self.assertEqual(lines[1], "inbox / Backlog")
        self.assertIn(f"ref: {card['ref']}", lines)
        self.assertIn("due: 2026-01-02", lines)
        self.assertIn("tags: errand", lines)
        data = self.run_json("show", card["ref"], directory=self.plain)
        self.assertEqual(data["card"]["ref"], card["ref"])

    def test_json_cards_have_no_board_only_agent_fields(self):
        card = self.run_json("add", "buy", directory=self.plain)["card"]
        data = self.run_json("show", card["ref"], directory=self.plain)["card"]
        for name in ("agent_status", "planning_status", "review_status", "pr_write_status", "planned", "reviewed"):
            self.assertNotIn(name, data)

    def test_move_text_and_json(self):
        card = self.run_json("add", "x", directory=self.plain)["card"]
        out, _ = self.run_cli("move", card["ref"], "In progress", directory=self.plain)
        self.assertEqual(out, f"{card['ref']} → In progress (inbox)\n")
        data = self.run_json(
            "move", card["ref"], "Todo", "--position", "top", directory=self.plain
        )
        self.assertEqual(data["card"]["column"], "Todo")

    def test_done_text_and_json(self):
        card = self.run_json("add", "x", directory=self.plain)["card"]
        out, _ = self.run_cli("done", card["ref"], directory=self.plain)
        self.assertEqual(out, f"{card['ref']} → Done (inbox)\n")
        card = self.run_json("add", "y", directory=self.plain)["card"]
        data = self.run_json("done", card["ref"], directory=self.plain)
        self.assertTrue(data["card"]["checked"])


class RepositoryTests(CliTestCase):
    def test_init_text_and_json(self):
        out, _ = self.run_cli("init", directory=self.repo)
        self.assertEqual(out.strip(), str(self.todos / "projects/acme-api.md"))
        self.run_cli("init", directory=self.repo, expect=1)

    def test_init_json(self):
        data = self.run_json("init", directory=self.repo)
        self.assertEqual(data, {"board": str(self.todos / "projects/acme-api.md")})

    def test_init_outside_repository_errors(self):
        data = self.run_json("init", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "error")
        _, err = self.run_cli("init", directory=self.plain, expect=1)
        self.assertTrue(err.startswith("herdboard: "))

    def test_add_without_board_creates_repo_board(self):
        out, _ = self.run_cli("add", "x", directory=self.repo)
        self.assertNotIn("herdboard init", out)
        self.assertIn("(acme-api)", out)
        data = self.run_json("add", "y", directory=self.repo)
        self.assertNotIn("hint", data)
        self.assertEqual(data["card"]["board"], "acme-api")

    def test_init_then_add_goes_to_repo_board(self):
        self.init_repo_board()
        out, _ = self.run_cli("add", "x", "--column", "Todo", directory=self.repo)
        self.assertRegex(out, r"^t[0-9a-z]{3} → Todo \(acme-api\)\n$")
        data = self.run_json("add", "y", directory=self.repo)
        self.assertNotIn("hint", data)

    def test_add_board_overrides_context(self):
        self.init_repo_board()
        self.run_cli("add", "x", directory=self.plain)
        card = self.run_json("add", "y", "--board", "inbox", directory=self.repo)["card"]
        self.assertEqual(card["board"], "inbox")

    def test_default_view_in_repo_with_board(self):
        self.init_repo_board()
        self.run_cli("add", "repo card", directory=self.repo)
        self.run_cli("add", "inbox card", "--board", "inbox", directory=self.repo)
        titles = [c["title"] for c in self.run_json("list", directory=self.repo)["cards"]]
        self.assertEqual(titles, ["repo card"])

    def test_default_view_in_repo_without_board_is_repo(self):
        self.run_cli("add", "repo card", directory=self.repo)
        titles = [c["title"] for c in self.run_json("list", directory=self.repo)["cards"]]
        self.assertEqual(titles, ["repo card"])

    def test_default_view_outside_git_is_inbox(self):
        self.init_repo_board()
        self.run_cli("add", "repo card", directory=self.repo)
        self.run_cli("add", "inbox card", directory=self.plain)
        titles = [c["title"] for c in self.run_json("list", directory=self.plain)["cards"]]
        self.assertEqual(titles, ["inbox card"])

    def test_view_repo_without_board_creates_it(self):
        data = self.run_json("list", "--view", "repo", directory=self.repo)
        self.assertEqual(data["boards"][0]["name"], "acme-api")

    def test_view_all_shows_board_headings(self):
        self.init_repo_board()
        self.run_cli("add", "repo card", directory=self.repo)
        self.run_cli("add", "inbox card", directory=self.plain)
        out, _ = self.run_cli("list", "--view", "all", directory=self.plain)
        self.assertIn("# inbox", out)
        self.assertIn("# acme-api", out)
        data = self.run_json("list", "--view", "all", directory=self.plain)
        self.assertEqual(len(data["cards"]), 2)

    def test_view_inbox_before_inbox_exists_is_empty(self):
        data = self.run_json("list", "--view", "inbox", directory=self.plain)
        self.assertEqual(data, {"cards": [], "boards": []})


class ErrorTests(CliTestCase):
    def test_unknown_ref(self):
        _, err = self.run_cli("show", "tzzz", directory=self.plain, expect=1)
        self.assertEqual(err, "herdboard: No card tzzz\n")
        data = self.run_json("show", "tzzz", directory=self.plain, expect=1)
        self.assertEqual(data, {"error": {"code": "not_found", "message": "No card tzzz"}})

    def test_usage_error_text(self):
        out, err = self.run_cli("frobnicate", directory=self.plain, expect=1)
        self.assertEqual(out, "")
        self.assertTrue(err.startswith("herdboard: "))

    def test_usage_error_json(self):
        data = self.run_json("move", "onlyref", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_conflict_exits_2(self):
        card = self.run_json("add", "x", directory=self.plain)["card"]
        with mock.patch.object(Store, "done", side_effect=ConflictError("card changed")):
            data = self.run_json("done", card["ref"], directory=self.plain, expect=2)
            self.assertEqual(data["error"]["code"], "conflict")
            _, err = self.run_cli("done", card["ref"], directory=self.plain, expect=2)
            self.assertEqual(err, "herdboard: card changed\n")


    def test_non_utf8_board_is_io_error(self):
        self.todos.mkdir()
        bad = self.todos / "inbox.md"
        bad.write_bytes(b"## Todo\n\n- [ ] caf\xe9\n")
        data = self.run_json("list", "--view", "all", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "io")
        self.assertIn(str(bad), data["error"]["message"])
        out, err = self.run_cli("list", "--view", "all", directory=self.plain, expect=1)
        self.assertEqual(out, "")
        self.assertIn(str(bad), err)
        self.assertTrue(err.startswith("herdboard: "))

    def test_non_utf8_board_on_write_is_io_error(self):
        self.run_cli("add", "x", directory=self.plain)
        (self.todos / "inbox.md").write_bytes(b"\xff")
        data = self.run_json("add", "y", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "io")

    def test_non_utf8_config_is_io_error(self):
        self.config.write_bytes(b"\xff")
        data = self.run_json("list", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "io")
        self.assertIn(str(self.config), data["error"]["message"])


class LimitTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.run_cli("add", "a", "--column", "Todo", directory=self.plain)
        self.run_cli("add", "b", "--column", "Review", directory=self.plain)
        inbox = self.todos / "inbox.md"
        inbox.write_text(inbox.read_text().replace("## Todo\n", "## Todo (5)\n"))

    def test_text_heading_shows_limit(self):
        out, _ = self.run_cli("list", directory=self.plain)
        lines = out.splitlines()
        self.assertEqual(lines[0], "## Todo (5)")
        self.assertIn("## Review", lines)

    def test_json_has_limit(self):
        cards = self.run_json("list", directory=self.plain)["cards"]
        self.assertEqual({c["title"]: c["column_limit"] for c in cards}, {"a": 5, "b": None})

    def test_column_names_for_moves_exclude_limit(self):
        card = self.run_json("list", "--column", "Todo", directory=self.plain)["cards"][0]
        moved = self.run_json("move", card["ref"], "Review", directory=self.plain)["card"]
        self.assertEqual(moved["column"], "Review")
        back = self.run_json("move", card["ref"], "Todo", directory=self.plain)["card"]
        self.assertEqual((back["column"], back["column_limit"]), ("Todo", 5))
        self.assertEqual(
            len(self.run_json("list", "--column", "todo", directory=self.plain)["cards"]), 1
        )


class NoteTests(CliTestCase):
    def setUp(self):
        super().setUp()
        root = Path(self.tmp.name)
        self.vault = root / "vault"
        (self.vault / "sub").mkdir(parents=True)
        (self.vault / "plain.md").write_text("plain content\n")
        (self.vault / "sub" / "deep.md").write_text("deep content\n")
        (self.vault / "a-first").mkdir()
        (self.vault / "a-first" / "dup.md").write_text("first dup\n")
        (self.vault / "sub" / "dup.md").write_text("second dup\n")
        (root / "secret.md").write_text("TOP SECRET\n")
        self.config.write_text(f'todos_dir = "{self.todos}"\nvault_root = "{self.vault}"\n')

    def card_with_links(self, *links):
        card = self.run_json("add", "see " + links[0], directory=self.plain)["card"]
        inbox = self.todos / "inbox.md"
        body = "".join(f"\t{link}\n" for link in links[1:])
        inbox.write_text(inbox.read_text().replace(f"^{card['ref']}\n", f"^{card['ref']}\n{body}"))
        return card["ref"]

    def notes(self, ref):
        return {n["name"]: n for n in self.run_json("show", ref, directory=self.plain)["notes"]}

    def test_found_at_root_and_in_subfolder_with_alias(self):
        ref = self.card_with_links("[[plain]]", "[[deep|the deep one]]")
        notes = self.notes(ref)
        self.assertEqual(notes["plain"]["content"], "plain content\n")
        self.assertEqual(notes["plain"]["path"], str(self.vault / "plain.md"))
        self.assertEqual(notes["deep"]["content"], "deep content\n")
        self.assertEqual(notes["deep"]["path"], str(self.vault / "sub" / "deep.md"))

    def test_first_match_by_sorted_path(self):
        ref = self.card_with_links("[[dup]]")
        self.assertEqual(self.notes(ref)["dup"]["content"], "first dup\n")

    def test_missing_note_listed_with_null_content(self):
        ref = self.card_with_links("[[ghost]]")
        note = self.notes(ref)["ghost"]
        self.assertIsNone(note["content"])

    def test_note_outside_vault_is_not_read(self):
        ref = self.card_with_links("[[../secret]]")
        out, _ = self.run_cli("--json", "show", ref, directory=self.plain)
        self.assertNotIn("TOP SECRET", out)
        self.assertIsNone(json.loads(out)["notes"][0]["content"])
        out, _ = self.run_cli("show", ref, directory=self.plain)
        self.assertNotIn("TOP SECRET", out)

    def test_text_output_appends_notes(self):
        ref = self.card_with_links("[[plain]]", "[[ghost]]")
        out, _ = self.run_cli("show", ref, directory=self.plain)
        self.assertIn("--- plain ---\nplain content", out)
        self.assertIn("--- ghost ---", out)

    def test_without_vault_root_nothing_is_resolved(self):
        self.config.write_text(f'todos_dir = "{self.todos}"\n')
        ref = self.card_with_links("[[plain]]")
        data = self.run_json("show", ref, directory=self.plain)
        self.assertNotIn("notes", data)
        out, _ = self.run_cli("show", ref, directory=self.plain)
        self.assertNotIn("---", out)


class PullRequestReviewTests(CliTestCase):
    """The pull request review commands. `pr_reviews` is replaced, so nothing calls gh, Hunk or Herdr."""

    def setUp(self):
        super().setUp()
        from herdboard import pr_reviews
        self.pr_reviews = pr_reviews
        self.enterContext(mock.patch.object(pr_reviews, "base_repository", return_value="acme/api"))

    def patch(self, name, **options):
        return self.enterContext(mock.patch.object(self.pr_reviews, name, **options))

    def test_prs_lists_the_open_prs_with_markers(self):
        pr = self.pr_reviews.PullRequest(7, "Fix login", "https://github.com/acme/api/pull/7", "octo", True,
                                         "a" * 40, "fix", True, False)
        self.patch("list_page", return_value=self.pr_reviews.Page([pr], 1, None))
        self.patch("records", return_value={"acme/api#7": {"status": "reviewed", "reviewed_sha": "b" * 40}})
        data = self.run_json("prs", directory=self.repo)
        self.assertEqual(data["repo"], "acme/api")
        self.assertEqual(data["prs"][0]["number"], 7)
        self.assertTrue(data["prs"][0]["new_commits"])
        self.assertEqual((data["page"], data["pages"], data["total"]), (1, 1, 1))
        out, _ = self.run_cli("prs", directory=self.repo)
        self.assertIn("#7", out)
        self.assertIn("draft, review requested, agent: reviewed, new commits", out)
        self.assertNotIn("Page", out)

    def test_prs_pages_follow_the_cursors_and_take_a_search(self):
        pr = self.pr_reviews.PullRequest(9, "Old", "u", "octo", False, "a" * 40, "b", False, False)
        pages = [self.pr_reviews.Page([], 120, "c1"), self.pr_reviews.Page([pr], 120, "c2")]
        listing = self.patch("list_page", side_effect=pages)
        self.patch("records", return_value={})
        out, _ = self.run_cli("prs", "--search", "author:octo", "--page", "2", directory=self.repo)
        self.assertEqual([c.kwargs["after"] for c in listing.call_args_list], [None, "c1"])
        self.assertEqual({c.args[2] for c in listing.call_args_list}, {"author:octo"})
        self.assertIn("Page 2 of 3 (120 open). Next: --page 3", out)

    def test_a_page_past_the_end(self):
        self.patch("list_page", return_value=self.pr_reviews.Page([], 3, None))
        data = self.run_json("prs", "--page", "4", directory=self.repo, expect=1)
        self.assertEqual(data["error"]["code"], "usage")

    def test_prs_without_any(self):
        self.patch("list_page", return_value=self.pr_reviews.Page([], 0, None))
        self.patch("records", return_value={})
        out, _ = self.run_cli("prs", directory=self.repo)
        self.assertEqual(out, "No open pull requests in acme/api.\n")

    def test_pr_review_uses_the_main_checkout_and_the_options(self):
        request = self.patch("request", return_value={
            "key": "acme/api#7", "number": 7, "workspace_id": "w9", "pane_id": "w9:p1", "hunk_pane": "w9:p2",
            "hunk_session": "s1", "agent": "pi", "agent_name": "pr-api-7", "model": "zai/glm-5.3",
            "thinking": "high", "prompted": False})
        data = self.run_json("pr-review", "7", "--agent", "pi", "--model", "zai/glm-5.3", "--thinking", "high",
                             directory=self.repo)
        self.assertEqual(data["workspace_id"], "w9")
        checkout, repo, number, kind, model, thinking = request.call_args.args
        self.assertEqual(Path(checkout).resolve(), self.repo.resolve())
        self.assertEqual((repo, number, kind, model, thinking), ("acme/api", 7, "pi", "zai/glm-5.3", "high"))

    def test_pr_review_takes_the_remembered_level_of_its_model(self):
        self.config.write_text(f'todos_dir = "{self.todos}"\nstart_agent = "pi"\nstart_model = "zai/glm-5.3"\n'
                               'start_thinking = "low"\n[thinking]\n"zai/glm-5.3" = "xhigh"\n')
        request = self.patch("request", return_value={
            "key": "acme/api#7", "number": 7, "workspace_id": "w9", "pane_id": "w9:p1", "hunk_pane": "w9:p2",
            "hunk_session": "s1", "agent": "pi", "agent_name": "pr-api-7", "model": "zai/glm-5.3",
            "thinking": "xhigh", "prompted": False})
        self.run_json("pr-review", "7", directory=self.repo)
        self.assertEqual(request.call_args.args[3:], ("pi", "zai/glm-5.3", "xhigh"))

    def test_pr_review_takes_the_command_line_level_over_the_remembered_one(self):
        self.config.write_text(f'todos_dir = "{self.todos}"\nstart_agent = "pi"\nstart_model = "zai/glm-5.3"\n'
                               '[thinking]\n"zai/glm-5.3" = "xhigh"\n')
        request = self.patch("request", return_value={
            "key": "acme/api#7", "number": 7, "workspace_id": "w9", "pane_id": "w9:p1", "hunk_pane": "w9:p2",
            "hunk_session": "s1", "agent": "pi", "agent_name": "pr-api-7", "model": "zai/glm-5.3",
            "thinking": "max", "prompted": False})
        self.run_json("pr-review", "7", "--thinking", "max", directory=self.repo)
        self.assertEqual(request.call_args.args[3:], ("pi", "zai/glm-5.3", "max"))

    def test_pr_review_defaults_to_the_start_agent(self):
        self.config.write_text(f'todos_dir = "{self.todos}"\nstart_agent = "claude"\nstart_model = "opus"\n')
        request = self.patch("request", return_value={
            "key": "acme/api#7", "number": 7, "workspace_id": "w9", "pane_id": "w9:p1", "hunk_pane": "w9:p2",
            "hunk_session": "s1", "agent": "claude", "agent_name": "pr-api-7", "model": "opus",
            "thinking": None, "prompted": False})
        out, _ = self.run_cli("pr-review", "7", directory=self.repo)
        self.assertEqual(request.call_args.args[3:], ("claude", "opus", None))
        self.assertIn("PR #7: claude (opus) reviews in workspace w9", out)

    def test_pr_review_wait_returns_the_worker_code(self):
        wait = self.patch("wait", return_value=1)
        self.run_cli("pr-review-wait", "acme/api#7", directory=self.plain, expect=1)
        wait.assert_called_once_with("acme/api#7")

    def test_refresh_go_and_finish_take_the_number(self):
        self.patch("refresh", return_value={"key": "acme/api#7", "number": 7, "hunk_pane": "w9:p3",
                                            "hunk_session": "s2", "kept": 2, "dropped": ["a.py:1: old"],
                                            "agent": "claude", "model": None, "thinking": None})
        self.patch("go", return_value={"key": "acme/api#7", "workspace_id": "w9"})
        finish = self.patch("finish", return_value={"key": "acme/api#7", "number": 7, "worktree_removed": True,
                                                    "branch_deleted": True})
        out, _ = self.run_cli("pr-review-refresh", "7", directory=self.repo)
        self.assertIn("2 notes kept", out)
        self.assertIn("a.py:1: old", out)
        self.assertEqual(self.run_json("pr-review-go", "7", directory=self.repo)["workspace_id"], "w9")
        self.assertTrue(self.run_json("pr-review-finish", "7", "--force", directory=self.repo)["branch_deleted"])
        finish.assert_called_with("acme/api#7", force=True)

    def test_an_error_has_its_code(self):
        self.patch("finish", side_effect=self.pr_reviews.PrReviewError("notes left", "unsent_notes"))
        data = self.run_json("pr-review-finish", "7", directory=self.repo, expect=1)
        self.assertEqual(data["error"]["code"], "unsent_notes")

    def test_outside_a_repository(self):
        data = self.run_json("prs", directory=self.plain, expect=1)
        self.assertIn("Git repository", data["error"]["message"])


class ModuleTests(unittest.TestCase):
    def test_python_m_help(self):
        env = {**os.environ, "PYTHONPATH": str(SRC)}
        result = subprocess.run(
            [sys.executable, "-m", "herdboard", "--help"],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("usage: herdboard", result.stdout)


if __name__ == "__main__":
    unittest.main()
