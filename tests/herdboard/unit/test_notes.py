"""The scratchpad notes: one Markdown file per board in `<todos_dir>/notes`."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from herdboard import notes
from herdboard.config import Config
from herdboard.model import HerdboardError, IoError


class NoteTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = Config(todos_dir=Path(self.temp.name) / "todos")

    def test_notes_live_in_a_folder_of_the_todos(self):
        self.assertEqual(notes.notes_dir(self.config), self.config.todos_dir / "notes")
        self.assertEqual(notes.note_path(self.config, "acme-api"), self.config.todos_dir / "notes" / "acme-api.md")
        self.assertEqual(notes.note_path(self.config, "inbox"), self.config.todos_dir / "notes" / "inbox.md")

    def test_a_board_name_with_a_folder_is_not_a_note(self):
        for name in ("", ".", "..", "a/b", "../x"):
            with self.assertRaises(HerdboardError, msg=name):
                notes.note_path(self.config, name)

    def test_a_missing_note_is_empty(self):
        self.assertEqual(notes.read_note(notes.note_path(self.config, "acme")), ("", None))

    def test_the_first_save_creates_the_folder_and_the_file(self):
        path = notes.note_path(self.config, "acme")
        stamp = notes.write_note(path, "# Ideas\n", None)
        self.assertEqual(path.read_text(), "# Ideas\n")
        self.assertEqual(stamp, path.stat().st_mtime_ns)
        self.assertEqual(notes.read_note(path), ("# Ideas\n", stamp))
        # No temporary file stays.
        self.assertEqual([entry.name for entry in path.parent.iterdir()], ["acme.md"])

    def test_a_save_after_a_save_needs_no_force(self):
        path = notes.note_path(self.config, "acme")
        stamp = notes.write_note(path, "one", None)
        notes.write_note(path, "two", stamp)
        self.assertEqual(path.read_text(), "two")

    def test_a_save_over_an_outside_edit_raises(self):
        path = notes.note_path(self.config, "acme")
        stamp = notes.write_note(path, "one", None)
        path.write_text("outside")
        os.utime(path, ns=(stamp + 10**9, stamp + 10**9))
        with self.assertRaises(notes.NoteChanged):
            notes.write_note(path, "mine", stamp)
        self.assertEqual(path.read_text(), "outside")
        notes.write_note(path, "mine", stamp, force=True)
        self.assertEqual(path.read_text(), "mine")

    def test_a_note_created_outside_after_the_read_raises(self):
        path = notes.note_path(self.config, "acme")
        path.parent.mkdir(parents=True)
        path.write_text("outside")
        with self.assertRaises(notes.NoteChanged):
            notes.write_note(path, "mine", None)

    def test_a_note_removed_outside_after_the_read_raises(self):
        path = notes.note_path(self.config, "acme")
        stamp = notes.write_note(path, "one", None)
        path.unlink()
        with self.assertRaises(notes.NoteChanged):
            notes.write_note(path, "mine", stamp)

    def test_a_new_note_follows_the_umask(self):
        umask = os.umask(0o022)
        self.addCleanup(os.umask, umask)
        path = notes.note_path(self.config, "acme")
        notes.write_note(path, "one", None)
        self.assertEqual(path.stat().st_mode & 0o777, 0o644)

    def test_a_save_keeps_the_mode_of_the_file(self):
        path = notes.note_path(self.config, "acme")
        notes.write_note(path, "one", None)
        path.chmod(0o600)
        notes.write_note(path, "two", path.stat().st_mtime_ns)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_a_write_error_is_an_io_error(self):
        path = notes.note_path(self.config, "acme")
        with patch("herdboard.notes.os.replace", side_effect=PermissionError("denied")):
            with self.assertRaises(IoError):
                notes.write_note(path, "mine", None)
        self.assertFalse(path.exists())
        self.assertEqual(list(path.parent.iterdir()), [])

    def test_a_read_error_is_an_io_error(self):
        path = notes.note_path(self.config, "acme")
        path.parent.mkdir(parents=True)
        path.write_bytes(b"\xff\xfe bad")
        with self.assertRaises(IoError):
            notes.read_note(path)


class PreviewTests(unittest.TestCase):
    """The preview shows each blank line of the note as an empty row."""

    def test_each_blank_line_becomes_an_empty_paragraph(self):
        self.assertEqual(notes.preview_markdown("one\n\n\ntwo"), "one\n\n&nbsp;\n\n&nbsp;\n\ntwo")

    def test_leading_and_trailing_blank_lines_stay(self):
        self.assertEqual(notes.preview_markdown("\n# Head\n\n- 1\n"), "\n&nbsp;\n\n# Head\n\n&nbsp;\n\n- 1\n")

    def test_lines_without_a_blank_line_between_stay_as_they_are(self):
        self.assertEqual(notes.preview_markdown("# Head\n- 1\n- 2"), "# Head\n- 1\n- 2")

    def test_a_blank_line_of_spaces_counts_as_blank(self):
        self.assertEqual(notes.preview_markdown("a\n  \nb"), "a\n\n&nbsp;\n\nb")

    def test_blank_lines_in_a_code_fence_stay(self):
        text = "```\nx\n\n\ny\n```\n\nafter"
        self.assertEqual(notes.preview_markdown(text), "```\nx\n\n\ny\n```\n\n&nbsp;\n\nafter")
        tilde = "~~~py\nx\n\ny\n~~~"
        self.assertEqual(notes.preview_markdown(tilde), tilde)

    def test_a_blank_line_between_list_items_keeps_the_list(self):
        # The empty row goes into the item above, so the next item is in the same list.
        self.assertEqual(notes.preview_markdown("1. a\n\n1. b"), "1. a\n\n   &nbsp;\n\n1. b")
        self.assertEqual(notes.preview_markdown("- a\n\n\n- b"), "- a\n\n  &nbsp;\n\n  &nbsp;\n\n- b")
        self.assertEqual(notes.preview_markdown("10) a\n\n11)  b"), "10) a\n\n    &nbsp;\n\n11)  b")

    def test_a_blank_line_in_a_list_item_keeps_the_item(self):
        self.assertEqual(notes.preview_markdown("- a\n\n  more"), "- a\n\n  &nbsp;\n\n  more")
        self.assertEqual(notes.preview_markdown("- a\n\n  - b"), "- a\n\n  &nbsp;\n\n  - b")
        self.assertEqual(notes.preview_markdown("- a\nlazy\n\n  more"), "- a\nlazy\n\n  &nbsp;\n\n  more")

    def test_a_blank_line_in_a_nested_list_goes_into_the_item_at_its_level(self):
        self.assertEqual(notes.preview_markdown("- a\n  - b\n\n  - c"), "- a\n  - b\n\n    &nbsp;\n\n  - c")
        self.assertEqual(notes.preview_markdown("- a\n  - b\n\n- c"), "- a\n  - b\n\n  &nbsp;\n\n- c")
        self.assertEqual(notes.preview_markdown("- a\n  - b\n\n  more"), "- a\n  - b\n\n  &nbsp;\n\n  more")

    def test_a_blank_line_after_a_list_ends_it(self):
        self.assertEqual(notes.preview_markdown("- a\n\nafter"), "- a\n\n&nbsp;\n\nafter")
        self.assertEqual(notes.preview_markdown("- a\n\n"), "- a\n\n&nbsp;\n\n")
        # A new list of another kind is not a sibling item.
        self.assertEqual(notes.preview_markdown("- a\n\n1. b"), "- a\n\n&nbsp;\n\n1. b")

    def test_a_code_fence_in_a_list_item_keeps_its_blank_lines(self):
        text = "- a\n\n  ```\n  x\n\n  y\n  ```"
        self.assertEqual(notes.preview_markdown(text), "- a\n\n  &nbsp;\n\n  ```\n  x\n\n  y\n  ```")

    def test_a_shorter_fence_does_not_close_a_longer_one(self):
        text = "````\n```\n\nx\n```\n````\n\nafter"
        self.assertEqual(notes.preview_markdown(text), "````\n```\n\nx\n```\n````\n\n&nbsp;\n\nafter")
        tildes = "~~~~\n~~~\n\n~~~~~\n\nafter"
        self.assertEqual(notes.preview_markdown(tildes), "~~~~\n~~~\n\n~~~~~\n\n&nbsp;\n\nafter")

    def test_a_fence_with_an_info_string_does_not_close_a_fence(self):
        text = "```\nx\n```py\n\ny\n```  \n\nafter"
        self.assertEqual(notes.preview_markdown(text), "```\nx\n```py\n\ny\n```  \n\n&nbsp;\n\nafter")

    def test_a_fence_of_the_other_character_does_not_close_a_fence(self):
        text = "```\n~~~\n\n```"
        self.assertEqual(notes.preview_markdown(text), text)

    def test_an_empty_note_stays_empty(self):
        self.assertEqual(notes.preview_markdown(""), "")


if __name__ == "__main__":
    unittest.main()
