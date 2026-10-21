"""The scratchpad notes: one Markdown file per board in `<todos_dir>/notes`."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/herdboard/src"))
from herdboard import notes  # noqa: E402
from herdboard.config import Config  # noqa: E402
from herdboard.model import HerdboardError, IoError  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
