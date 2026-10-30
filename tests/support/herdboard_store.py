import tempfile
import unittest
from pathlib import Path
from herdboard.config import Config
from herdboard.store import Store


SETTINGS = '%% kanban:settings\n```\n{"kanban-plugin":"board"}\n```\n%%'


HEAD = "---\n\nkanban-plugin: board\n\n---\n\n"


def board_text(*columns: str, archive: str = "") -> str:
    return HEAD + "".join(columns) + archive + "\n\n" + SETTINGS


def col(title: str, *lines: str) -> str:
    return f"## {title}\n\n" + "".join(f"{line}\n" for line in lines) + "\n\n"


BASIC = board_text(
    col("Todo", "- [ ] one", "- [ ] two #tag 📅 2026-01-02", "\tbody line"),
    col("Doing", "- [ ] three ^tabc"),
    col("Done", "**Complete**", "- [x] four"),
    archive="***\n\n## Archive\n\n- [x] old\n\n",
)


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.todos = Path(self.tmp.name) / "todos"
        (self.todos / "projects").mkdir(parents=True)
        self.config = Config(todos_dir=self.todos)
        self.store = Store(self.config, lock_timeout=0.2)

    def put(self, name, text, mode=None):
        path = self.todos / ("inbox.md" if name == "inbox" else f"projects/{name}.md")
        path.write_text(text)
        if mode is not None:
            path.chmod(mode)
        return path
