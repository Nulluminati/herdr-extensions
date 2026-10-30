import contextlib
import fcntl
import io
import json
import re
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import cli, kanban_md  # noqa: E402
from herdboard.store import Store  # noqa: E402
from support.herdboard_cli import CliTestCase  # noqa: E402

FIXTURES = ROOT / "tests/fixtures/herdboard/migrate"
ID = re.compile(r" \^t[0-9a-z]{3}$", re.M)


class MigrateCliTestCase(CliTestCase):
    def setUp(self):
        super().setUp()
        self.todo = self.repo / "TODO.md"
        self.todo.write_bytes((FIXTURES / "loose-text.md").read_bytes())
        self.board = self.todos / "projects/acme-api.md"

    def migrate(self, *args, tty=False, answer="", expect=0, directory=None):
        argv = ["--config", str(self.config), "-C", str(directory or self.repo), *args]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err), \
                mock.patch.object(cli, "_interactive", return_value=tty), \
                mock.patch.object(sys, "stdin", io.StringIO(answer)):
            code = cli.main(argv)
        self.assertEqual(code, expect, f"stdout={out.getvalue()!r} stderr={err.getvalue()!r}")
        return out.getvalue(), err.getvalue()

    def migrate_json(self, *args, **kwargs):
        out, _ = self.migrate("--json", *args, **kwargs)
        return json.loads(out)


class PreviewTests(MigrateCliTestCase):
    def test_text_preview_groups_by_column(self):
        self.todo.write_bytes((FIXTURES / "atx.md").read_bytes())
        out, _ = self.migrate("migrate", str(self.todo))
        self.assertIn(str(self.board), out)
        self.assertIn("## Backlog (6)\n", out)
        self.assertNotIn("## Done", out)
        self.assertIn("  - [ ] water the ferns #garden\n", out)
        self.assertIn("  - [ ] label the jars #kitchen-ideas\n    - glass ones\n      - small sizes\n    - tin ones\n", out)
        self.assertNotIn("^t", out)

    def test_checked_cards_appear_under_done(self):
        self.todo.write_text("# A\n- [ ] open\n- [x] closed\n")
        out, _ = self.migrate("migrate", str(self.todo))
        self.assertRegex(out, r"## Backlog \(1\)\n  - \[ \] open #a\n## Done \(1\)\n  - \[x\] closed #a\n")

    def test_text_preview_lists_non_list_lines(self):
        out, _ = self.migrate("migrate", str(self.todo))
        self.assertIn("Non-list lines (4)", out)
        self.assertIn("line 1: card: Intro line before any list", out)
        self.assertIn("line 3: attached: A note about the first item", out)

    def test_no_terminal_without_yes_writes_nothing(self):
        out, _ = self.migrate("migrate", str(self.todo))
        self.assertFalse(self.board.exists())
        self.assertIn("Nothing written", out)
        self.assertIn("--yes", out)

    def test_json_preview(self):
        data = self.migrate_json("migrate", str(self.todo))
        self.assertEqual(set(data), {"board", "preview", "cards", "written"})
        self.assertEqual(data["board"], str(self.board))
        self.assertEqual(data["cards"], 5)
        self.assertIs(data["written"], False)
        self.assertFalse(self.board.exists())
        column = data["preview"]["columns"][0]
        self.assertEqual(column["column"], "Backlog")
        first = column["cards"][1]
        self.assertEqual(first, {"title": "first item", "tag": None, "checked": False,
                                 "body": ["\tA note about the first item", "\tan indented note"]})
        self.assertEqual(data["preview"]["non_list"][1],
                         {"line": 3, "text": "A note about the first item",
                          "action": "attached", "section": None})

    def test_json_never_prompts_even_on_a_terminal(self):
        data = self.migrate_json("migrate", str(self.todo), tty=True, answer="y\n")
        self.assertIs(data["written"], False)
        self.assertFalse(self.board.exists())


class WriteTests(MigrateCliTestCase):
    def test_yes_writes_and_prints_original_path(self):
        before = self.todo.read_bytes()
        out, _ = self.migrate("migrate", str(self.todo), "--yes")
        self.assertTrue(self.board.is_file())
        self.assertIn(f"Wrote 5 cards to {self.board}", out)
        self.assertIn(f"Original file left unchanged: {self.todo}", out)
        self.assertEqual(self.todo.read_bytes(), before)

    def test_json_yes(self):
        data = self.migrate_json("migrate", str(self.todo), "--yes")
        self.assertIs(data["written"], True)
        self.assertEqual(data["cards"], 5)
        text = self.board.read_text()
        self.assertEqual(len(ID.findall(text)), 5)
        self.assertIn("repo: github.com/acme/api", text)

    def test_round_trip(self):
        self.todo.write_bytes((FIXTURES / "atx.md").read_bytes())
        self.migrate("migrate", str(self.todo), "--yes")
        text = self.board.read_text()
        self.assertEqual(kanban_md.serialize(kanban_md.parse(text)), text)
        self.assertEqual(len(kanban_md.parse(text).column("Backlog").cards), 6)

    def test_terminal_confirmation_yes(self):
        out, err = self.migrate("migrate", str(self.todo), tty=True, answer="y\n")
        self.assertTrue(self.board.is_file())
        self.assertIn("Write 5 cards", err)
        self.assertIn("Original file left unchanged", out)

    def test_terminal_confirmation_no_and_eof(self):
        for answer in ("n\n", "\n", ""):
            out, _ = self.migrate("migrate", str(self.todo), tty=True, answer=answer)
            self.assertFalse(self.board.exists())
            self.assertIn("Nothing written", out)

    def test_yes_on_terminal_does_not_prompt(self):
        _, err = self.migrate("migrate", str(self.todo), "--yes", tty=True)
        self.assertNotIn("Write 5", err)
        self.assertTrue(self.board.is_file())

    def test_second_run_appends_at_the_bottom(self):
        self.migrate("migrate", str(self.todo), "--yes")
        first = self.board.read_text()
        self.todo.write_text("# More\n- extra\n")
        self.migrate("migrate", str(self.todo), "--yes")
        cards = [c.title for c in kanban_md.parse(self.board.read_text()).column("Backlog").cards]
        self.assertEqual(cards[-1], "extra #more")
        self.assertEqual(len(cards), 6)
        self.assertTrue(set(ID.findall(first)) <= set(ID.findall(self.board.read_text())))

    def test_existing_board_cards_unchanged(self):
        self.init_repo_board()
        self.run_cli("add", "keep me", "--column", "Todo", directory=self.repo)
        before = self.board.read_text()
        self.migrate("migrate", str(self.todo), "--yes")
        kept = [line for line in self.board.read_text().split("\n")
                if not any(t in line for t in ("item", "Intro", "note", "indented"))]
        self.assertEqual("\n".join(kept), before)


class TargetTests(MigrateCliTestCase):
    def test_board_option_names_an_existing_board(self):
        self.init_repo_board()
        other = self.todos / "projects/homelab.md"
        other.write_text(self.board.read_text().replace("github.com/acme/api", "github.com/acme/lab"))
        data = self.migrate_json("migrate", str(self.todo), "--board", "homelab", "--yes")
        self.assertEqual(data["board"], str(other))
        self.assertIn("first item", other.read_text())
        self.assertNotIn("first item", self.board.read_text())

    def test_unknown_board_is_a_user_error(self):
        data = self.migrate_json("migrate", str(self.todo), "--board", "nope", "--yes", expect=1)
        self.assertEqual(data["error"]["code"], "not_found")

    def test_inbox_board_is_created(self):
        data = self.migrate_json("migrate", str(self.todo), "--board", "inbox", "--yes")
        self.assertEqual(data["board"], str(self.todos / "inbox.md"))
        self.assertTrue((self.todos / "inbox.md").is_file())

    def test_file_outside_a_repository_goes_to_inbox(self):
        loose = self.plain / "TODO.md"
        loose.write_bytes(self.todo.read_bytes())
        data = self.migrate_json("migrate", str(loose), "--yes")
        self.assertEqual(data["board"], str(self.todos / "inbox.md"))

    def test_repository_comes_from_the_file_not_the_working_directory(self):
        data = self.migrate_json("migrate", str(self.todo), "--yes", directory=self.plain)
        self.assertEqual(data["board"], str(self.board))

    def test_relative_path_is_resolved_against_the_directory(self):
        out, _ = self.migrate("migrate", "TODO.md", "--yes")
        self.assertIn(str(self.todo), out)


class ErrorTests(MigrateCliTestCase):
    def test_missing_file(self):
        data = self.migrate_json("migrate", str(self.repo / "nope.md"), expect=1)
        self.assertEqual(data["error"]["code"], "not_found")

    def test_file_without_items(self):
        self.todo.write_text("\n\n=====\n\n")
        data = self.migrate_json("migrate", str(self.todo), "--yes", expect=1)
        self.assertIn("nothing to migrate", data["error"]["message"].lower())
        self.assertFalse(self.board.exists())

    def test_not_utf8(self):
        self.todo.write_bytes(b"- \xff\xfe\n")
        data = self.migrate_json("migrate", str(self.todo), "--yes", expect=1)
        self.assertEqual(data["error"]["code"], "io")

    def test_lock_held_exits_2(self):
        self.todos.mkdir(parents=True, exist_ok=True)
        with open(self.todos / ".herdboard.lock", "a") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            fast = lambda config: Store(config, lock_timeout=0.05)  # noqa: E731
            with mock.patch.object(cli, "Store", side_effect=fast):
                data = self.migrate_json("migrate", str(self.todo), "--yes", expect=2)
        self.assertEqual(data["error"]["code"], "conflict")
        self.assertFalse(self.board.exists())

    def test_checked_item_on_board_without_done_column(self):
        self.init_repo_board()
        self.board.write_text(self.board.read_text().replace("## Done", "## Finished")
                              .replace("**Complete**\n", ""))
        before = self.board.read_text()
        self.todo.write_text("- [x] a\n")
        data = self.migrate_json("migrate", str(self.todo), "--yes", expect=1)
        self.assertIn("no done column", data["error"]["message"])
        self.assertEqual(self.board.read_text(), before)

    def test_unwritable_board(self):
        self.init_repo_board()
        text = self.board.read_text().replace("kanban-plugin: board", "kanban-plugin: other")
        self.board.write_text(text)
        self.migrate_json("migrate", str(self.todo), "--yes", expect=1)
        self.assertEqual(self.board.read_text(), text)


class FixtureStyleTests(MigrateCliTestCase):
    def test_every_fixture_converts_and_round_trips(self):
        for fixture in sorted(FIXTURES.glob("*.md")):
            with self.subTest(fixture=fixture.name):
                self.board.unlink(missing_ok=True)
                self.todo.write_bytes(fixture.read_bytes())
                data = self.migrate_json("migrate", str(self.todo), "--yes")
                self.assertGreater(data["cards"], 0)
                text = self.board.read_text()
                self.assertEqual(kanban_md.serialize(kanban_md.parse(text)), text)
                self.assertEqual(self.todo.read_bytes(), fixture.read_bytes())


class ReviewFixTests(MigrateCliTestCase):
    def test_preview_keeps_source_order_when_first_column_is_done(self):
        self.put_board("Done", "**Complete**")
        self.todo.write_text("- [x] first\n- second\n")
        data = self.migrate_json("migrate", str(self.todo))
        column = data["preview"]["columns"]
        self.assertEqual([(c["column"], [x["title"] for x in c["cards"]]) for c in column],
                         [("Done", ["first", "second"])])
        self.migrate("migrate", str(self.todo), "--yes")
        written = kanban_md.parse(self.board.read_text()).column("Done").cards
        self.assertEqual([c.title for c in written], ["first", "second"])

    def put_board(self, *column):
        text = ("---\n\nkanban-plugin: board\nrepo: github.com/acme/api\n\n---\n\n"
                f"## {column[0]}\n\n" + "".join(f"{line}\n" for line in column[1:])
                + "\n\n%% kanban:settings\n```\n{\"kanban-plugin\":\"board\"}\n```\n%%\n")
        self.board.parent.mkdir(parents=True, exist_ok=True)
        self.board.write_text(text)

    def test_preview_lists_dropped_ids_and_ambiguous_lines(self):
        self.todo.write_text("- root ^tabc\n\t- a\n - stray\n")
        data = self.migrate_json("migrate", str(self.todo))
        preview = data["preview"]
        self.assertEqual(preview["dropped_ids"], [{"card": "root", "id": "tabc"}])
        self.assertEqual(preview["non_list"],
                         [{"line": 3, "text": "stray", "action": "ambiguous",
                           "section": None, "parent": "root"}])
        out, _ = self.migrate("migrate", str(self.todo))
        self.assertIn("line 3: ambiguous: stray (under: root)", out)
        self.assertIn("Dropped source IDs (1)", out)
        self.assertIn("  ^tabc: root", out)

    def test_changed_repo_after_preview_is_a_conflict_with_no_write(self):
        self.put_board("Backlog", "- [ ] one ^taaa")
        self.todo.write_text("- new\n")

        def change_repo(_prompt):
            self.board.write_text(self.board.read_text().replace("acme/api", "acme/other"))
            return True

        with mock.patch.object(cli, "_confirm", side_effect=change_repo):
            self.migrate("migrate", str(self.todo), tty=True, expect=2)
        self.assertNotIn("new", self.board.read_text())
        self.assertIn("repo: github.com/acme/other", self.board.read_text())

    def test_explicit_board_stays_path_based(self):
        self.put_board("Backlog", "- [ ] one ^taaa")
        self.todo.write_text("- new\n")

        def change_repo(_prompt):
            self.board.write_text(self.board.read_text().replace("acme/api", "acme/other"))
            return True

        with mock.patch.object(cli, "_confirm", side_effect=change_repo):
            self.migrate("migrate", str(self.todo), "--board", "acme-api", tty=True)
        self.assertIn("- [ ] new ^t", self.board.read_text())


if __name__ == "__main__":
    unittest.main()
