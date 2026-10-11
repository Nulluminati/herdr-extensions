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

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "plugins/herdboard/src"
sys.path.insert(0, str(SRC))

from herdboard import cli  # noqa: E402
from herdboard.model import ConflictError  # noqa: E402
from herdboard.store import Store  # noqa: E402


def git(directory, *args):
    subprocess.run(["git", "-C", str(directory), *args], check=True, capture_output=True)


class CliTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.todos = root / "todos"
        self.config = root / "config.toml"
        self.config.write_text(f'todos_dir = "{self.todos}"\n')
        self.repo = root / "api"
        self.repo.mkdir()
        git(self.repo, "init", "-q")
        git(self.repo, "remote", "add", "origin", "https://github.com/acme/api.git")
        self.plain = root / "plain"
        self.plain.mkdir()

    def run_cli(self, *args, directory=None, expect=0):
        argv = ["--config", str(self.config)]
        if directory is not None:
            argv += ["-C", str(directory)]
        argv += list(args)
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(argv)
        self.assertEqual(code, expect, f"stdout={out.getvalue()!r} stderr={err.getvalue()!r}")
        return out.getvalue(), err.getvalue()

    def run_json(self, *args, directory=None, expect=0):
        out, _ = self.run_cli("--json", *args, directory=directory, expect=expect)
        return json.loads(out)

    def init_repo_board(self):
        return self.run_json("init", directory=self.repo)["board"]


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
                "ref", "board", "path", "column", "title", "checked", "due", "tags", "body",
                "column_limit",
            },
        )

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

    def test_add_without_board_hints(self):
        out, _ = self.run_cli("add", "x", directory=self.repo)
        self.assertIn("herdboard init", out)
        self.assertIn("(inbox)", out)
        data = self.run_json("add", "y", directory=self.repo)
        self.assertIn("herdboard init", data["hint"])
        self.assertEqual(data["card"]["board"], "inbox")

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

    def test_default_view_in_repo_without_board_is_global(self):
        self.run_cli("add", "inbox card", directory=self.repo)
        titles = [c["title"] for c in self.run_json("list", directory=self.repo)["cards"]]
        self.assertEqual(titles, ["inbox card"])

    def test_default_view_outside_git_is_global(self):
        self.init_repo_board()
        self.run_cli("add", "repo card", directory=self.repo)
        self.run_cli("add", "inbox card", directory=self.plain)
        titles = [c["title"] for c in self.run_json("list", directory=self.plain)["cards"]]
        self.assertEqual(titles, ["inbox card"])

    def test_view_repo_without_board_errors(self):
        _, err = self.run_cli("list", "--view", "repo", directory=self.repo, expect=1)
        self.assertIn("herdboard init", err)

    def test_view_all_shows_board_headings(self):
        self.init_repo_board()
        self.run_cli("add", "repo card", directory=self.repo)
        self.run_cli("add", "inbox card", directory=self.plain)
        out, _ = self.run_cli("list", "--view", "all", directory=self.plain)
        self.assertIn("# inbox", out)
        self.assertIn("# acme-api", out)
        data = self.run_json("list", "--view", "all", directory=self.plain)
        self.assertEqual(len(data["cards"]), 2)

    def test_view_global_before_inbox_exists_is_empty(self):
        data = self.run_json("list", "--view", "global", directory=self.plain)
        self.assertEqual(data, {"cards": []})


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
