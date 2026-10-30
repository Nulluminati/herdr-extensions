"""Card attachments: large documents, such as a plan, in a hidden folder next to the activity log."""

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from herdboard import activity, attachments
from herdboard.config import load_config
from herdboard.model import HerdboardError, NotFoundError
from herdboard.store import Store
from support.herdboard_cli import CliTestCase


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        config = self.root / "config.toml"
        config.write_text(f'todos_dir = "{self.root / "todos"}"\n')
        self.store = Store(load_config(config))
        self.folder = self.root / "todos" / ".herdboard" / "attachments"
        self.doc = self.root / "spec.md"
        self.doc.write_text("# Spec\n\nLong text.\n", encoding="utf-8")

    def test_add_copies_the_file_into_the_card_folder(self):
        added = attachments.add(self.store, "t3m9", self.doc)
        path = self.folder / "t3m9" / "spec.md"
        self.assertEqual(added, {"name": "spec.md", "path": str(path), "size": 19})
        self.assertEqual(path.read_text(encoding="utf-8"), "# Spec\n\nLong text.\n")
        self.assertTrue(self.doc.exists())

    def test_add_takes_another_name(self):
        attachments.add(self.store, "ENG-12", self.doc, name="design.md")
        self.assertTrue((self.folder / "ENG-12" / "design.md").is_file())

    def test_add_replaces_an_attachment_with_the_same_name(self):
        attachments.add(self.store, "t3m9", self.doc)
        self.doc.write_text("Short.\n", encoding="utf-8")
        attachments.add(self.store, "t3m9", self.doc)
        self.assertEqual((self.folder / "t3m9" / "spec.md").read_text(encoding="utf-8"), "Short.\n")

    def test_listing_is_sorted_by_name(self):
        attachments.add(self.store, "t3m9", self.doc, name="z.md")
        attachments.add(self.store, "t3m9", self.doc, name="a.md")
        self.assertEqual([a["name"] for a in attachments.listing(self.store.config, "t3m9")], ["a.md", "z.md"])

    def test_a_card_without_attachments_has_an_empty_listing(self):
        self.assertEqual(attachments.listing(self.store.config, "t3m9"), [])

    def test_remove_deletes_one_attachment(self):
        attachments.add(self.store, "t3m9", self.doc)
        removed = attachments.remove(self.store, "t3m9", "spec.md")
        self.assertEqual(removed["name"], "spec.md")
        self.assertEqual(attachments.listing(self.store.config, "t3m9"), [])

    def test_remove_of_a_missing_attachment_fails(self):
        with self.assertRaises(NotFoundError):
            attachments.remove(self.store, "t3m9", "spec.md")

    def test_a_missing_source_file_fails(self):
        with self.assertRaises(NotFoundError):
            attachments.add(self.store, "t3m9", self.root / "missing.md")

    def test_names_that_leave_the_folder_are_refused(self):
        for name in ("../x.md", "a/b.md", ".hidden", "", "plan.md\n"):
            with self.subTest(name=name), self.assertRaises(HerdboardError):
                attachments.add(self.store, "t3m9", self.doc, name=name)

    def test_a_reference_that_is_not_a_card_id_is_refused(self):
        with self.assertRaises(HerdboardError):
            attachments.add(self.store, "../x", self.doc)

    def test_plan_text_reads_the_plan_attachment(self):
        self.assertIsNone(attachments.plan_text(self.store.config, "t3m9"))
        attachments.add(self.store, "t3m9", self.doc, name="plan.md")
        self.assertEqual(attachments.plan_text(self.store.config, "t3m9"), "# Spec\n\nLong text.\n")
        self.assertEqual(attachments.plan_path(self.store.config, "t3m9"), self.folder / "t3m9" / "plan.md")

    def test_a_plan_that_is_not_utf8_text_is_refused(self):
        image = self.root / "photo.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n\xff\xfe")
        with self.assertRaises(HerdboardError):
            attachments.add(self.store, "t3m9", image, name="plan.md")
        self.assertEqual(attachments.listing(self.store.config, "t3m9"), [])
        self.assertEqual(attachments.add(self.store, "t3m9", image)["size"], 10)

    def test_add_copies_the_file_in_parts(self):
        # A large document is not read into memory at once.
        with mock.patch.object(Path, "read_bytes", side_effect=AssertionError("read whole")):
            attachments.add(self.store, "t3m9", self.doc, name="plan.md")
        self.assertEqual(attachments.plan_text(self.store.config, "t3m9"), "# Spec\n\nLong text.\n")

    def test_a_plan_for_an_agent_that_cannot_be_read_is_left_out_with_a_warning(self):
        plan = self.folder / "t3m9" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_bytes(b"\xff\xfe")
        self.assertIsNone(attachments.plan_for_agent(self.store, "t3m9"))
        message = activity.read(self.store.config, "t3m9")[-1]["message"]
        self.assertTrue(message.startswith(f"The agent does not get the plan: {plan}: cannot read the plan"))
        plan.write_text("1. Do it.\n", encoding="utf-8")
        self.assertEqual(attachments.plan_for_agent(self.store, "t3m9"), "1. Do it.\n")

    def test_view_text_reads_a_text_attachment(self):
        self.assertEqual(attachments.view_text(self.doc), "# Spec\n\nLong text.\n")

    def test_view_text_is_none_for_a_binary_file(self):
        image = self.root / "shot.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        self.assertIsNone(attachments.view_text(image))
        latin = self.root / "old.txt"
        latin.write_bytes(b"caf\xe9\n")
        self.assertIsNone(attachments.view_text(latin))

    def test_view_text_is_none_for_a_file_too_large_to_show(self):
        big = self.root / "big.log"
        big.write_text("x" * (attachments.VIEW_LIMIT + 1), encoding="utf-8")
        self.assertIsNone(attachments.view_text(big))

    def test_view_text_of_a_missing_file_is_an_io_error(self):
        with self.assertRaises(HerdboardError) as caught:
            attachments.view_text(self.root / "gone.md")
        self.assertIn("gone.md: cannot read the attachment", str(caught.exception))

    def test_size_label(self):
        self.assertEqual(attachments.size_label(19), "19 B")
        self.assertEqual(attachments.size_label(2048), "2.0 KB")
        self.assertEqual(attachments.size_label(5 * 1024 * 1024 + 1), "5.0 MB")

    def test_cards_with_files_names_the_cards_that_have_an_attachment(self):
        attachments.add(self.store, "t3m9", self.doc)
        attachments.add(self.store, "ENG-12", self.doc)
        (self.folder / "tempt").mkdir()
        (self.folder / "tempt" / ".spec.md.x1y2").write_text("partial")
        (self.folder / "tgon").mkdir()
        self.assertEqual(attachments.cards_with_files(self.store.config), {"t3m9", "ENG-12"})

    def test_cards_with_files_is_empty_without_an_attachments_folder(self):
        self.assertEqual(attachments.cards_with_files(self.store.config), set())

    def test_mark_sets_the_board_status_and_survives_an_unreadable_folder(self):
        attachments.add(self.store, "t3m9", self.doc)
        cards = [mock.Mock(ref="t3m9", has_attachments=False), mock.Mock(ref="tabc", has_attachments=True)]
        attachments.mark(self.store.config, cards)
        self.assertEqual([card.has_attachments for card in cards], [True, False])
        with mock.patch.object(attachments, "card_files", side_effect=attachments.IoError("denied")):
            attachments.mark(self.store.config, cards)
        self.assertEqual([card.has_attachments for card in cards], [False, False])

    def test_mark_sets_planned_only_when_the_card_has_a_plan(self):
        attachments.add(self.store, "t3m9", self.doc, name="plan.md")
        attachments.add(self.store, "tabc", self.doc)
        cards = [mock.Mock(ref=ref, planned=None) for ref in ("t3m9", "tabc", "tdef")]
        attachments.mark(self.store.config, cards)
        self.assertEqual([card.planned for card in cards], [True, False, False])
        with mock.patch.object(attachments, "card_files", side_effect=attachments.IoError("denied")):
            attachments.mark(self.store.config, cards)
        self.assertEqual([card.planned for card in cards], [False, False, False])


class AttachCliTests(CliTestCase):
    def setUp(self):
        super().setUp()
        self.enterContext(mock.patch.object(activity, "author", return_value="user"))
        self.ref = self.run_json("add", "Fix the login timeout", directory=self.plain)["card"]["ref"]
        self.doc = self.plain / "spec.md"
        self.doc.write_text("# Spec\n", encoding="utf-8")

    def test_attach_copies_the_file_and_logs_it(self):
        data = self.run_json("attach", self.ref, "spec.md", directory=self.plain)
        self.assertEqual(data["ref"], self.ref)
        self.assertEqual(data["attachment"]["name"], "spec.md")
        log = self.run_json("show", self.ref, directory=self.plain)["log"]
        self.assertEqual(log[-1]["message"], "Attached spec.md.")

    def test_attach_with_a_name_and_text_output(self):
        out, _ = self.run_cli("attach", self.ref, str(self.doc), "--name", "design.md", directory=self.plain)
        self.assertEqual(out, f"{self.ref}: attached design.md\n")

    def test_remove_deletes_and_logs_it(self):
        self.run_json("attach", self.ref, "spec.md", directory=self.plain)
        data = self.run_json("attach", self.ref, "--remove", "spec.md", directory=self.plain)
        self.assertEqual(data["removed"]["name"], "spec.md")
        show = self.run_json("show", self.ref, directory=self.plain)
        self.assertEqual(show["attachments"], [])
        self.assertEqual(show["log"][-1]["message"], "Removed the attachment spec.md.")

    def test_attach_needs_a_file_or_remove_but_not_both(self):
        for args in ([], ["spec.md", "--remove", "spec.md"], ["--remove", "x.md", "--name", "y.md"]):
            with self.subTest(args=args):
                data = self.run_json("attach", self.ref, *args, directory=self.plain, expect=1)
                self.assertEqual(data["error"]["code"], "usage")

    def test_show_lists_the_attachments(self):
        self.run_json("attach", self.ref, "spec.md", directory=self.plain)
        data = self.run_json("show", self.ref, directory=self.plain)
        self.assertEqual([a["name"] for a in data["attachments"]], ["spec.md"])
        out, _ = self.run_cli("show", self.ref, directory=self.plain)
        self.assertIn("--- attachments ---\n" + data["attachments"][0]["path"] + "\n", out)

    def test_a_linear_issue_needs_no_connection(self):
        data = self.run_json("attach", "eng-123", "spec.md", directory=self.plain)
        self.assertEqual(data["ref"], "ENG-123")

    def test_attach_to_an_unknown_todo_fails(self):
        data = self.run_json("attach", "tzzz", "spec.md", directory=self.plain, expect=1)
        self.assertEqual(data["error"]["code"], "not_found")


if __name__ == "__main__":
    unittest.main()
