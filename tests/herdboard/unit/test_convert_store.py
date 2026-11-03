import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdboard import activity, attachments, cli, convert_store
from herdboard.config import Config
from herdboard.convert_store import ConvertError, split_title
from herdboard.model import OldFormatError
from herdboard.store import Store
from support.paths import ROOT

FIXTURES = ROOT / "tests/herdboard/fixtures/convert"


class SplitTitleTests(unittest.TestCase):
    def test_every_field(self):
        fields = split_title("Billing v2 #type/feature #billing #api 📅 2026-11-15 [[#^tpar]] → ENG-7")
        self.assertEqual(
            fields,
            {
                "title": "Billing v2",
                "type": "feature",
                "tags": ["billing", "api"],
                "due": "2026-11-15",
                "parent": "tpar",
                "promoted": "ENG-7",
            },
        )

    def test_plain_text(self):
        self.assertEqual(split_title("Just text")["title"], "Just text")

    def test_a_title_of_only_tags(self):
        self.assertEqual(split_title("#a #b")["title"], "Untitled")


class ConvertTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.enterContext(mock.patch.dict(os.environ, {"XDG_STATE_HOME": str(self.root / "state")}))
        self.todos = self.root / "todos"
        (self.todos / "projects").mkdir(parents=True)
        shutil.copy(FIXTURES / "inbox-crlf.md", self.todos / "inbox.md")
        shutil.copy(FIXTURES / "project.md", self.todos / "projects" / "acme-api.md")
        data = self.todos / ".herdboard"
        (data / "log").mkdir(parents=True)
        (data / "log" / "tpar.md").write_text("- 2026-03-04 05:06 · user · Started.\n")
        (data / "log" / "tar1.md").write_text("- 2026-01-01 00:00 · user · Old.\n")
        (data / "log" / "a7k2.md").write_text("- 2026-01-01 00:00 · herdboard · Spawned.\n")
        (data / "log" / "ENG-12.md").write_text("- 2026-01-01 00:00 · user · Linear.\n")
        (data / "log" / "tgon.md").write_text("- 2026-01-01 00:00 · user · Deleted card.\n")
        (data / "attachments" / "tpar").mkdir(parents=True)
        (data / "attachments" / "tpar" / "plan.md").write_text("plan")
        (data / "attachments" / "ENG-12").mkdir(parents=True)
        (data / "attachments" / "ENG-12" / "spec.md").write_text("spec")
        (data / "automations.toml").write_text("")
        (self.todos / "notes").mkdir()
        (self.todos / "notes" / "inbox.md").write_text("global note")
        (self.todos / "notes" / "acme-api.md").write_text("project note")
        self.config = Config(todos_dir=self.todos)
        self.store = Store(self.config, lock_timeout=0.2)

    def convert(self, **kwargs):
        kwargs.setdefault("busy", lambda refs: [])
        return convert_store.convert_store(self.store, **kwargs)

    def board(self, name):
        return self.todos / "boards" / name


class ConvertStoreTests(ConvertTestCase):
    def test_dry_run_changes_nothing(self):
        (self.todos / ".herdboard.lock").touch()
        before = sorted(str(p.relative_to(self.todos)) for p in self.todos.rglob("*"))
        result = self.convert(dry_run=True)
        self.assertTrue(result["dry_run"])
        self.assertEqual([b["name"] for b in result["boards"]], ["inbox", "acme-api"])
        self.assertEqual(sorted(str(p.relative_to(self.todos)) for p in self.todos.rglob("*")), before)

    def test_old_layout_is_refused_by_other_commands(self):
        with self.assertRaises(OldFormatError):
            self.store.list_cards()

    def test_columns_cards_and_done_column(self):
        self.convert()
        snapshot = self.store.snapshot(self.board("acme-api"), archive=True)
        self.assertEqual([c.title for c in snapshot.columns], ["Triage", "Todo", "Shipped"])
        self.assertEqual([c.completed for c in snapshot.columns], [False, False, True])
        self.assertEqual(snapshot.columns[1].limit, 3)
        todo = {c.ref: c for c in snapshot.columns[1].cards}
        self.assertEqual(list(todo)[:3], ["tpar", "tkid", "tpro"])
        self.assertEqual(len(todo), 5)
        inbox = self.store.snapshot(self.board("inbox"))
        self.assertEqual([c.completed for c in inbox.columns], [False, True])

    def test_card_fields(self):
        self.convert()
        parent = self.store.show("tpar")
        self.assertEqual(
            (parent.title, parent.type, parent.tags, parent.due),
            ("Billing v2", "feature", ["billing", "api"], "2026-11-15"),
        )
        self.assertEqual(parent.body, ["Details of billing", "- [ ] Choose a provider", "\tNested note"])
        self.assertEqual(self.store.show("tkid").parent, "tpar")
        self.assertEqual(self.store.show("tpro").promoted, "ENG-7")
        self.assertEqual(self.store.show("tbug").type, "bug")

    def test_created_is_the_first_log_entry(self):
        self.convert()
        text = (self.board("acme-api") / "cards" / "tpar" / "card.md").read_text()
        self.assertIn("created = 2026-03-04T05:06:00", text)

    def test_crlf_and_space_indent(self):
        self.convert()
        card = self.store.show("tinb")
        self.assertEqual(
            (card.title, card.tags, card.body), ("Inbox card", ["home"], ["Space indented body", "- [ ] item"])
        )

    def test_cards_without_a_valid_id_get_one(self):
        result = self.convert()
        changes = result["boards"][1]["new_ids"]
        self.assertEqual([c["old"] for c in changes], [None, "NOT-AN-ID"])
        titles = [c.title for c in self.store.list_cards([self.board("acme-api")])]
        self.assertIn("No ID yet", titles)
        self.assertIn("Bad id", titles)

    def test_archive_keeps_its_order_newest_first(self):
        self.convert()
        self.assertEqual([c.ref for c in self.store.archived_cards([self.board("acme-api")])], ["tar2", "tar1"])

    def test_board_settings_and_linear_tables(self):
        self.convert()
        meta = self.store.board_meta(self.board("acme-api"))
        self.assertEqual((meta.repo, meta.merge, meta.error), ("github.com/acme/api", "pr", None))
        self.assertEqual([(link.id, [x.name for x in link.labels]) for link in meta.links], [("p-plat", ["repo:api"])])
        self.assertEqual([p.identifier for p in meta.pins], ["ENG-5"])

    def test_logs_attachments_and_notes_move(self):
        result = self.convert()
        self.assertEqual([e["message"] for e in activity.read(self.config, "tpar")], ["Started."])
        self.assertEqual([e["message"] for e in activity.read(self.config, "tar1")], ["Old."])
        self.assertEqual([e["message"] for e in activity.read(self.config, "a7k2")], ["Spawned."])
        self.assertEqual([e["message"] for e in activity.read(self.config, "ENG-12")], ["Linear."])
        self.assertEqual([e["name"] for e in attachments.listing(self.config, "tpar")], ["plan.md"])
        self.assertEqual([e["name"] for e in attachments.listing(self.config, "ENG-12")], ["spec.md"])
        self.assertEqual((self.board("inbox") / "notes.md").read_text(), "global note")
        self.assertEqual((self.board("acme-api") / "notes.md").read_text(), "project note")
        self.assertEqual(result["data_without_card"], ["tgon"])

    def test_old_files_move_to_a_backup_in_the_state_folder(self):
        result = self.convert()
        backup = Path(result["backup"])
        self.assertTrue(backup.is_relative_to(self.root / "state"))
        for name in ("inbox.md", "projects/acme-api.md", "notes/inbox.md", ".herdboard/log/tpar.md"):
            self.assertTrue((backup / name).is_file(), name)
        for name in ("inbox.md", "projects", "notes", ".herdboard/log", ".herdboard/attachments"):
            self.assertFalse((self.todos / name).exists(), name)
        self.assertTrue((self.todos / ".herdboard" / "automations.toml").exists())
        self.assertFalse((self.todos / "boards.tmp").exists())
        self.assertEqual(len(self.store.list_cards()), 9)

    def test_a_second_run_is_refused(self):
        self.convert()
        with self.assertRaises(ConvertError):
            self.convert()

    def test_nothing_to_convert(self):
        shutil.rmtree(self.todos)
        self.todos.mkdir()
        with self.assertRaises(ConvertError):
            self.convert()

    def test_running_cards_are_refused_by_name(self):
        with self.assertRaises(ConvertError) as caught:
            self.convert(busy=lambda refs: [ref for ref in refs if ref == "tpar"])
        self.assertIn("tpar", str(caught.exception))
        self.assertFalse((self.todos / "boards").exists())

    def test_a_dry_run_names_the_running_cards_and_writes_nothing(self):
        result = self.convert(dry_run=True, busy=lambda refs: [ref for ref in refs if ref == "tpar"])
        self.assertEqual(result["running"], ["tpar"])
        self.assertFalse((self.todos / "boards").exists())
        self.assertFalse((self.todos / "boards.tmp").exists())

    def test_a_conversion_reports_no_running_cards(self):
        self.assertEqual(self.convert()["running"], [])

    def test_a_failure_mid_build_leaves_the_old_store(self):
        before = (self.todos / "inbox.md").read_bytes()
        with mock.patch("herdboard.convert_store.shutil.copytree", side_effect=OSError("disk full")):
            with self.assertRaises(ConvertError) as caught:
                self.convert()
        self.assertIn("disk full", str(caught.exception))
        self.assertFalse((self.todos / "boards").exists())
        self.assertFalse((self.todos / "boards.tmp").exists())
        self.assertFalse((self.todos / ".herdboard" / "linear").exists())
        self.assertEqual((self.todos / "inbox.md").read_bytes(), before)
        self.assertTrue((self.todos / ".herdboard" / "log" / "tpar.md").exists())

    def test_a_left_over_temp_folder_is_removed(self):
        (self.todos / "boards.tmp" / "junk").mkdir(parents=True)
        self.convert()
        self.assertFalse((self.todos / "boards" / "junk").exists())

    def test_a_dry_run_keeps_a_left_over_temp_folder_and_names_it(self):
        (self.todos / "boards.tmp" / "junk").mkdir(parents=True)
        result = self.convert(dry_run=True)
        self.assertTrue((self.todos / "boards.tmp" / "junk").is_dir())
        self.assertEqual(result["left_over"], str(self.todos / "boards.tmp"))

    def test_a_failed_backup_copy_leaves_the_old_store(self):
        before = (self.todos / "inbox.md").read_bytes()
        real = shutil.copy2

        def copy2(source, target, *args, **kwargs):
            if Path(target).is_relative_to(self.root / "state"):
                raise OSError("state disk full")
            return real(source, target, *args, **kwargs)

        with mock.patch("herdboard.convert_store.shutil.copy2", side_effect=copy2):
            with self.assertRaises(ConvertError) as caught:
                self.convert()
        self.assertIn("state disk full", str(caught.exception))
        self.assertFalse((self.todos / "boards").exists())
        self.assertFalse((self.todos / "boards.tmp").exists())
        self.assertFalse((self.todos / ".herdboard" / "linear").exists())
        self.assertEqual((self.todos / "inbox.md").read_bytes(), before)
        self.assertEqual(list((self.root / "state").glob("**/backup-*")), [])
        self.assertEqual(len(self.convert()["boards"]), 2)

    def test_a_failed_removal_of_the_old_files_names_them(self):
        real = shutil.rmtree

        def rmtree(path, *args, **kwargs):
            if Path(path) == self.todos / "notes":
                raise OSError("busy")
            return real(path, *args, **kwargs)

        with mock.patch("herdboard.convert_store.shutil.rmtree", side_effect=rmtree):
            with self.assertRaises(ConvertError) as caught:
                self.convert()
        message = str(caught.exception)
        self.assertIn(str(self.todos / "notes"), message)
        self.assertIn("backup", message)
        self.assertEqual(len(self.store.list_cards()), 9)
        backup = next((self.root / "state").glob("**/backup-*"))
        self.assertTrue((backup / "notes" / "inbox.md").is_file())
        self.assertFalse((self.todos / "inbox.md").exists())

    def test_sub_cards_follow_a_parent_that_gets_a_new_id(self):
        (self.todos / "projects" / "zeta.md").write_text(
            "## Todo\n\n- [ ] Same ID as acme ^tpar\n- [ ] Child [[#^tpar]] ^tzk1\n"
        )
        result = self.convert()
        change = result["boards"][2]["new_ids"][0]
        self.assertEqual((change["old"], change["title"]), ("tpar", "Same ID as acme"))
        self.assertEqual(change["data_stays_with"], "acme-api")
        self.assertEqual(self.store.show("tzk1").parent, change["new"])
        self.assertEqual(self.store.show("tkid").parent, "tpar")

    def test_a_new_id_never_takes_the_id_of_a_later_card(self):
        (self.todos / "projects" / "aaa.md").write_text("## Todo\n\n- [ ] No ID, before acme\n")
        letters = iter("parzzz")
        real = convert_store.secrets.choice
        with mock.patch.object(
            convert_store.secrets, "choice", side_effect=lambda seq: next(letters, None) or real(seq)
        ):
            result = self.convert()
        self.assertEqual(result["boards"][1]["new_ids"][0]["new"], "tzzz")
        self.assertEqual(self.store.show("tpar").title, "Billing v2")
        self.assertEqual(self.store.show("tkid").parent, "tpar")
        self.assertEqual([e["message"] for e in activity.read(self.config, "tpar")], ["Started."])

    def test_the_running_check_covers_linear_issues_and_automations(self):
        with mock.patch.object(convert_store, "planned_refs", return_value=["ENG-99", "tpar"]):
            for ref in ("ENG-12", "a7k2", "ENG-99"):
                with self.subTest(ref=ref), self.assertRaises(ConvertError) as caught:
                    self.convert(busy=lambda refs, ref=ref: [r for r in refs if r == ref])
                self.assertIn(ref, str(caught.exception))
            seen = []
            self.convert(dry_run=True, busy=lambda refs: seen.extend(refs) or [])
        self.assertEqual(len(seen), len(set(seen)))
        self.assertFalse((self.todos / "boards").exists())

    def test_a_review_counts_as_running_only_while_its_reviewer_pane_lives(self):
        from herdboard import agent_review, plan, start

        record = {"ref": "tpar", "agent_review": {"pane_id": "p7"}}
        self.enterContext(mock.patch.object(start, "find_record", return_value=("tpar", record)))
        self.enterContext(mock.patch.object(start, "agent_live", return_value=False))
        self.enterContext(mock.patch.object(plan, "running_planner", return_value=None))
        with mock.patch.object(agent_review, "_reviewer_status", return_value=None) as status:
            self.assertEqual(convert_store.running_cards(["tpar"]), [])
        status.assert_called_once_with("p7")
        with mock.patch.object(agent_review, "_reviewer_status", return_value="working"):
            self.assertEqual(convert_store.running_cards(["tpar"]), ["tpar"])

    def test_configured_complete_markers_replace_complete(self):
        config_file = self.root / "config.toml"
        config_file.write_text('complete_markers = ["Fertig"]\n')
        self.convert(config_file=config_file)
        self.assertEqual(
            [c.completed for c in self.store.snapshot(self.board("acme-api")).columns], [False, False, False]
        )

    def test_a_failure_removes_the_data_folders_it_made_in_existing_folders(self):
        (self.todos / ".herdboard" / "automations").mkdir()
        (self.todos / ".herdboard" / "linear" / "ENG-1").mkdir(parents=True)
        with mock.patch("herdboard.convert_store.os.rename", side_effect=OSError("disk full")):
            with self.assertRaises(ConvertError):
                self.convert()
        self.assertEqual(list((self.todos / ".herdboard" / "automations").iterdir()), [])
        self.assertEqual([p.name for p in (self.todos / ".herdboard" / "linear").iterdir()], ["ENG-1"])

    def test_a_project_named_inbox_is_refused(self):
        (self.todos / "projects" / "inbox.md").write_text("## Todo\n")
        with self.assertRaises(ConvertError) as caught:
            self.convert(dry_run=True)
        self.assertIn(str(self.todos / "projects" / "inbox.md"), str(caught.exception))

    def test_space_indents_keep_their_nesting(self):
        (self.todos / "projects" / "zeta.md").write_text(
            "## Todo\n\n- [ ] Two spaces ^tzk2\n  Body\n  - [ ] item\n    - nested\n"
        )
        self.convert()
        self.assertEqual(self.store.show("tzk2").body, ["Body", "- [ ] item", "  - nested"])

    def test_data_with_an_unknown_name_is_reported(self):
        (self.todos / ".herdboard" / "log" / "notes-old.md").write_text("- x\n")
        (self.todos / ".herdboard" / "attachments" / "misc").mkdir()
        result = self.convert()
        self.assertEqual(result["data_without_card"], ["misc", "notes-old", "tgon"])

    def test_old_settings_are_read_from_the_config_file(self):
        config_file = self.root / "config.toml"
        config_file.write_text('complete_markers = ["Shipped"]\n')
        board = self.todos / "projects" / "acme-api.md"
        board.write_text(board.read_text().replace("**Complete**", "**Shipped**"))
        self.convert(config_file=config_file)
        self.assertEqual(
            [c.completed for c in self.store.snapshot(self.board("acme-api")).columns], [False, False, True]
        )

    def test_boards_and_a_new_old_file_fail(self):
        self.convert()
        (self.todos / "inbox.md").write_text("written by an old copy")
        with self.assertRaises(OldFormatError) as caught:
            self.store.list_cards()
        self.assertIn("inbox.md", str(caught.exception))


class ConvertCliTests(ConvertTestCase):
    def run_cli(self, *args, expect=0, running=()):
        config = self.root / "config.toml"
        config.write_text(f'todos_dir = "{self.todos}"\n')
        out, err = io.StringIO(), io.StringIO()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            mock.patch.object(convert_store, "running_cards", lambda refs: list(running)),
        ):
            code = cli.main(["--config", str(config), "-C", str(self.root), *args])
        self.assertEqual(code, expect, f"{out.getvalue()!r} {err.getvalue()!r}")
        return out.getvalue()

    def test_other_commands_name_convert_store(self):
        error = json.loads(self.run_cli("--json", "list", expect=1))["error"]
        self.assertEqual(error["code"], "old_format")
        self.assertIn("herdboard convert-store", error["message"])

    def test_dry_run_then_convert(self):
        result = json.loads(self.run_cli("--json", "convert-store", "--dry-run"))
        self.assertTrue(result["dry_run"])
        self.assertFalse((self.todos / "boards").exists())
        out = self.run_cli("convert-store")
        self.assertIn("Converted", out)
        self.assertIn("Old files moved to", out)
        self.assertEqual(len(json.loads(self.run_cli("--json", "list", "--view", "all"))["cards"]), 9)

    def test_a_dry_run_names_the_running_cards(self):
        out = self.run_cli("convert-store", "--dry-run", running=["tpar"])
        self.assertIn("Would convert", out)
        self.assertIn("Stop the agents, planners and reviewers of tpar before the conversion", out)

    def test_a_dry_run_names_the_left_over_folder_and_the_data_of_a_new_id(self):
        (self.todos / "boards.tmp").mkdir()
        (self.todos / "projects" / "zeta.md").write_text("## Todo\n\n- [ ] Same ID as acme ^tpar\n")
        out = self.run_cli("convert-store", "--dry-run")
        self.assertIn(f"{self.todos / 'boards.tmp'} is left over from a failed conversion", out)
        self.assertIn("    The log and attachments of tpar go to tpar on acme-api\n", out)

    def test_log_and_attach_outside_a_repository_refuse_the_old_layout(self):
        (self.root / "spec.md").write_text("spec")
        for args in (("log", "ENG-12", "note"), ("attach", "ENG-12", str(self.root / "spec.md")), ("log", "a7k2", "x")):
            with self.subTest(args=args):
                error = json.loads(self.run_cli("--json", *args, expect=1))["error"]
                self.assertEqual(error["code"], "old_format")
        self.assertFalse((self.todos / ".herdboard" / "linear").exists())
        self.assertFalse((self.todos / ".herdboard" / "automations").exists())

    def test_doctor_reports_the_old_layout(self):
        result = json.loads(self.run_cli("--json", "doctor", "--offline", expect=1))
        self.assertIn("old_format", [f["code"] for f in result["findings"]])


if __name__ == "__main__":
    unittest.main()
