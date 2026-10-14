"""Card attachments: large documents, such as a plan, in a hidden folder next to the activity log."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "plugins/herdboard/src"))

from herdboard import activity, attachments  # noqa: E402
from herdboard.config import load_config  # noqa: E402
from herdboard.model import HerdboardError, NotFoundError  # noqa: E402
from herdboard.store import Store  # noqa: E402

from test_herdboard_cli import CliTestCase  # noqa: E402


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
