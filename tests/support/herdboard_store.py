import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from herdboard import card_file
from herdboard.card_file import ColumnFile
from herdboard.config import Config
from herdboard.store import Store

CREATED = datetime(2026, 1, 1, 9, 0, tzinfo=timezone(timedelta(hours=-7)))


def write_card(folder: Path, spec) -> str:
    """Write `<folder>/<id>/card.md` from a title or a dict of fields. Returns the ID."""
    spec = {"title": spec} if isinstance(spec, str) else dict(spec)
    card_id = spec.pop("id")
    body = spec.pop("body", [])
    archived = spec.pop("archived", None)
    card = card_file.new_card(spec.pop("title"), created=spec.pop("created", CREATED), body=body, **spec)
    if archived is not None:
        card.archived = archived
    (folder / card_id).mkdir(parents=True, exist_ok=True)
    (folder / card_id / "card.md").write_text(card_file.dump_card(card))
    return card_id


def write_board(
    todos: Path,
    name: str,
    columns,
    archive=(),
    repo: str | None = None,
    merge: str | None = None,
) -> Path:
    """Write a board folder. `columns` is a list of (name, cards) or (name, cards, options):
    each card a dict with an `id` and the `card.md` fields. `options` sets `done` and `limit`.
    The column named Done is the done column unless an option says otherwise."""
    path = todos / "boards" / name
    path.mkdir(parents=True, exist_ok=True)
    board = card_file.new_board([], repo=repo, merge=merge)
    for column in columns:
        title, cards, options = (*column, {})[:3]
        done = options.get("done", title.casefold() == "done")
        ids = [write_card(path / "cards", card) for card in cards]
        board.columns.append(ColumnFile(title, ids, options.get("limit"), done))
    for card in archive:
        write_card(path / "archive", card)
    (path / "board.toml").write_text(card_file.dump_board(board))
    return path


BASIC = [
    (
        "Todo",
        [
            {"id": "t001", "title": "one"},
            {"id": "t002", "title": "two", "tags": ["tag"], "due": "2026-01-02", "body": ["body line"]},
        ],
    ),
    ("Doing", [{"id": "tabc", "title": "three"}]),
    ("Done", [{"id": "t004", "title": "four"}]),
]
BASIC_ARCHIVE = [{"id": "t005", "title": "old", "archived": CREATED}]


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.todos = Path(self.tmp.name) / "todos"
        self.todos.mkdir()
        self.config = Config(todos_dir=self.todos)
        self.store = Store(self.config, lock_timeout=0.2)

    def put(self, name, columns=None, archive=None, **kwargs):
        """A board folder; without columns, the BASIC board with its archive."""
        if columns is None:
            columns = BASIC
            archive = BASIC_ARCHIVE if archive is None else archive
        return write_board(self.todos, name, columns, archive or (), **kwargs)

    def card_text(self, path: Path, card_id: str, folder: str = "cards") -> str:
        return (path / folder / card_id / "card.md").read_text()

    def board_text(self, path: Path) -> str:
        return (path / "board.toml").read_text()

    def columns(self, path: Path) -> dict[str, list[str]]:
        board = card_file.parse_board(self.board_text(path), path / "board.toml")
        return {column.name: column.cards for column in board.columns}


def write_markdown_board(todos: Path, name: str, text: str) -> Path:
    """Write a board folder from a board in the old Markdown notation, with `convert_store`.

    Only files whose text changes are written, so a test can change one card the way an
    outside edit would. Card folders that the text no longer names are removed. Cards
    without an ID get a new one.
    """
    import shutil

    from herdboard.convert_store import convert_board

    path = todos / "boards" / name
    taken = {p.name for p in (todos / "boards").glob("*/*/*") if p.parent.name in ("cards", "archive")}
    own = {p.name for p in path.glob("*/*") if p.parent.name in ("cards", "archive")}
    converted = convert_board(
        name,
        path,
        text,
        taken=taken - own,
        now=CREATED,
        markers=["Complete"],
        archive_titles=("Archive",),
    )
    files = {path / "board.toml": card_file.dump_board(converted.file)}
    for folder, cards in (("cards", converted.cards), ("archive", converted.archive)):
        for card_id, card in cards.items():
            files[path / folder / card_id / "card.md"] = card_file.dump_card(card)
    for file, content in files.items():
        if not file.is_file() or file.read_text() != content:
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)
    for folder, cards in (("cards", converted.cards), ("archive", converted.archive)):
        for old in path.glob(f"{folder}/*"):
            if old.name not in cards:
                shutil.rmtree(old)
    return path


def render_markdown(path: Path) -> str:
    """A board folder in the old Markdown notation, for tests that compare card text."""

    def line(card_id: str, card, checked: bool) -> list[str]:
        parts = [card.title]
        parts += [f"#type/{card.type}"] if card.type else []
        parts += [f"#{tag}" for tag in card.tags]
        parts += [f"📅 {card.due}"] if card.due else []
        parts += [f"[[#^{card.parent}]]"] if card.parent else []
        parts += [f"→ {card.promoted}"] if card.promoted else []
        first = f"- [{'x' if checked else ' '}] {' '.join(parts)} ^{card_id}"
        return [first, *(f"\t{body}" if body else "\t" for body in card.body)]

    board = card_file.parse_board((path / "board.toml").read_text(), path / "board.toml")
    front = [f"repo: {board.repo}"] if board.repo else []
    front += [f"merge: {board.merge}"] if board.merge else []
    lines: list[str] = ["---", "", "kanban-plugin: board", *front, "", "---", ""]
    for column in board.columns:
        heading = f"## {column.name}" + (f" ({column.limit})" if column.limit is not None else "")
        lines += [heading, ""] + (["**Complete**"] if column.done else [])
        for card_id in column.cards:
            card_path = path / "cards" / card_id / "card.md"
            if card_path.is_file():
                lines += line(card_id, card_file.parse_card(card_path.read_text(), card_path), column.done)
        lines.append("")
    archived = [(p.parent.name, card_file.parse_card(p.read_text(), p)) for p in (path / "archive").glob("*/card.md")]
    if archived:
        lines += ["***", "", "## Archive", ""]
        for card_id, card in sorted(archived, key=lambda item: (item[1].archived or CREATED, item[0])):
            lines += line(card_id, card, True)
    return "\n".join(lines) + "\n"
