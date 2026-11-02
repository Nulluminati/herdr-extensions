"""Parse and write the files of a board folder: `board.toml` and each card's `card.md`.

`card.md` is TOML frontmatter between `+++` fences, then a plain Markdown body:

    +++
    title = "Fix the login"
    type = "bug"
    tags = ["auth"]
    due = 2026-11-15
    created = 2026-10-31T10:00:00-07:00
    +++

    Body text.

`board.toml` lists the columns in order, each with its card IDs in order. A card's
folder name is its ID, and only `board.toml` says which column holds it.

tomlkit keeps key order, comments and keys that herdboard does not know. The functions
here are pure: they do no locking and touch no files.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import tomlkit
from tomlkit.exceptions import TOMLKitError

from .model import FormatError
from .paths import TODO_REF

FORMAT = 1
FENCE = "+++"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
KNOWN_COLUMN_KEYS = ("name", "limit", "done", "cards")


def _toml(text: str, path: Path) -> tomlkit.TOMLDocument:
    try:
        return tomlkit.parse(text)
    except (TOMLKitError, ValueError) as error:
        raise FormatError(f"{path}: invalid TOML: {error}") from error


def _string_list(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def parse_due(text: str) -> date:
    """A `YYYY-MM-DD` due date. Raises FormatError for any other text."""
    if not DATE.fullmatch(text):
        raise FormatError(f"Due date {text!r} is not YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError as error:
        raise FormatError(f"Due date {text!r} is not a real date") from error


def check_title(title: str) -> str:
    """The title without outer spaces. Raises FormatError unless it is one non-empty line."""
    title = title.strip()
    if not title or "\n" in title or "\r" in title:
        raise FormatError("A card title must be one line of text")
    return title


# Cards


@dataclass
class CardFile:
    """One `card.md`: the frontmatter document and the body lines."""

    doc: tomlkit.TOMLDocument
    body: list[str] = field(default_factory=list)

    def _set(self, key: str, value: object) -> None:
        if value is None or value == []:
            self.doc.pop(key, None)
        else:
            self.doc[key] = value

    @property
    def title(self) -> str:
        return str(self.doc["title"])

    @title.setter
    def title(self, value: str) -> None:
        self.doc["title"] = check_title(value)

    @property
    def type(self) -> str | None:
        value = self.doc.get("type")
        return str(value) if value else None

    @type.setter
    def type(self, value: str | None) -> None:
        self._set("type", value or None)

    @property
    def tags(self) -> list[str]:
        return [str(tag) for tag in self.doc.get("tags", [])]

    @tags.setter
    def tags(self, value: list[str]) -> None:
        self._set("tags", list(value))

    @property
    def due(self) -> str | None:
        value = self.doc.get("due")
        return value.isoformat() if value is not None else None

    @due.setter
    def due(self, value: str | None) -> None:
        self._set("due", parse_due(value) if value else None)

    @property
    def parent(self) -> str | None:
        value = self.doc.get("parent")
        return str(value) if value else None

    @parent.setter
    def parent(self, value: str | None) -> None:
        self._set("parent", value)

    @property
    def promoted(self) -> str | None:
        value = self.doc.get("promoted")
        return str(value) if value else None

    @promoted.setter
    def promoted(self, value: str | None) -> None:
        self._set("promoted", value)

    @property
    def created(self) -> datetime | None:
        return self.doc.get("created")

    @property
    def archived(self) -> datetime | None:
        return self.doc.get("archived")

    @archived.setter
    def archived(self, value: datetime | None) -> None:
        self._set("archived", value)


def body_lines(body: list[str]) -> list[str]:
    """Body lines without trailing blank lines or trailing line endings."""
    lines = [line.rstrip("\r\n") for line in body]
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def new_card(
    title: str,
    *,
    created: datetime,
    type: str | None = None,
    tags: list[str] = (),
    due: str | None = None,
    parent: str | None = None,
    body: list[str] = (),
) -> CardFile:
    card = CardFile(tomlkit.document(), body_lines(list(body)))
    card.title = title
    card.type = type
    card.tags = list(tags)
    card.due = due
    card.parent = parent
    card.doc["created"] = created
    return card


def _check_card(doc: tomlkit.TOMLDocument, path: Path) -> None:
    title = doc.get("title")
    if not isinstance(title, str) or not title.strip() or "\n" in title:
        raise FormatError(f"{path}: needs a one-line text 'title'")
    for key in ("type", "parent", "promoted"):
        if key in doc and not isinstance(doc[key], str):
            raise FormatError(f"{path}: {key!r} must be text")
    if "parent" in doc and not TODO_REF.match(doc["parent"]):
        raise FormatError(f"{path}: 'parent' must be a card ID such as t1a2")
    if "tags" in doc and not _string_list(doc["tags"]):
        raise FormatError(f"{path}: 'tags' must be a list of text")
    if "due" in doc and (not isinstance(doc["due"], date) or isinstance(doc["due"], datetime)):
        raise FormatError(f"{path}: 'due' must be a date such as 2026-11-15")
    for key in ("created", "archived"):
        if key in doc and not isinstance(doc[key], datetime):
            raise FormatError(f"{path}: {key!r} must be a date and time")


def parse_card(text: str, path: Path) -> CardFile:
    lines = text.replace("\r\n", "\n").split("\n")
    end = next((i for i in range(1, len(lines)) if lines[i].rstrip() == FENCE), None)
    if not lines or lines[0].rstrip() != FENCE or end is None:
        raise FormatError(f"{path}: card must start with TOML frontmatter between {FENCE} lines")
    doc = _toml("\n".join(lines[1:end]) + "\n", path)
    _check_card(doc, path)
    body = lines[end + 1 :]
    if body and not body[0].strip():
        body = body[1:]
    return CardFile(doc, body_lines(body))


def dump_card(card: CardFile) -> str:
    text = f"{FENCE}\n{tomlkit.dumps(card.doc)}{FENCE}\n"
    body = body_lines(card.body)
    if body:
        text += "\n" + "\n".join(body) + "\n"
    return text


# Boards


@dataclass
class ColumnFile:
    """One `[[column]]` table. `table` keeps keys that herdboard does not know."""

    name: str
    cards: list[str] = field(default_factory=list)
    limit: int | None = None
    done: bool = False
    table: tomlkit.items.Table | None = None

    def to_table(self) -> tomlkit.items.Table:
        # A fresh table each time: a reused one keeps its old trailing blank lines.
        table = tomlkit.table()
        table["name"] = self.name
        if self.limit is not None:
            table["limit"] = self.limit
        if self.done:
            table["done"] = True
        for key, value in (self.table or {}).items():
            if key not in KNOWN_COLUMN_KEYS:
                table[key] = value
        cards = tomlkit.array()
        cards.extend(self.cards)
        cards.multiline(bool(self.cards))
        # `cards` goes last, so that a long list ends the table.
        table["cards"] = cards
        return table


@dataclass
class BoardFile:
    """One `board.toml`. `doc` keeps every key; `columns` is the working list of columns."""

    doc: tomlkit.TOMLDocument
    columns: list[ColumnFile] = field(default_factory=list)

    def _scalar(self, key: str) -> str | None:
        value = self.doc.get(key)
        return str(value) if value else None

    def _set_scalar(self, key: str, value: str | None) -> None:
        if value is None:
            self.doc.pop(key, None)
        else:
            self.doc[key] = value

    @property
    def repo(self) -> str | None:
        return self._scalar("repo")

    @repo.setter
    def repo(self, value: str | None) -> None:
        self._set_scalar("repo", value)

    @property
    def merge(self) -> str | None:
        return self._scalar("merge")

    @merge.setter
    def merge(self, value: str | None) -> None:
        self._set_scalar("merge", value)

    def tables(self, key: str) -> list[dict]:
        """The array of tables under `key` as plain values; empty when the key is not there."""
        value = self.doc.get(key)
        if value is None:
            return []
        return value.unwrap() if hasattr(value, "unwrap") else value

    def set_tables(self, key: str, items: list[dict]) -> None:
        """Set the array of tables under `key`, or remove it for an empty list."""
        if not items:
            self.doc.pop(key, None)
            return
        array = tomlkit.aot()
        for item in items:
            table = tomlkit.table()
            for name, value in item.items():
                if isinstance(value, list) and value and all(isinstance(v, dict) for v in value):
                    inline = tomlkit.array()
                    for entry in value:
                        row = tomlkit.inline_table()
                        row.update(entry)
                        inline.append(row)
                    value = inline
                table[name] = value
            array.append(table)
        # Tables go before the columns, so that the columns end the file.
        columns = self.doc.pop("column", None)
        self.doc.pop(key, None)
        self.doc[key] = array
        if columns is not None:
            self.doc["column"] = columns

    def column(self, name: str) -> ColumnFile | None:
        return next((column for column in self.columns if column.name.casefold() == name.casefold()), None)

    def column_of(self, card_id: str) -> ColumnFile | None:
        return next((column for column in self.columns if card_id in column.cards), None)

    def card_ids(self) -> list[str]:
        """Every listed card ID, in column order."""
        return [card_id for column in self.columns for card_id in column.cards]


def new_board(columns: list[str], *, repo: str | None = None, merge: str | None = None) -> BoardFile:
    """A board with `columns`. The column named Done (any case) is the done column."""
    doc = tomlkit.document()
    doc["format"] = FORMAT
    board = BoardFile(doc)
    board.repo = repo
    board.merge = merge
    done = next((name for name in columns if name.casefold() == "done"), None)
    board.columns = [ColumnFile(name, done=name == done) for name in columns]
    return board


def _column(table: object, index: int, path: Path) -> ColumnFile:
    where = f"{path}: column {index}"
    if not isinstance(table, dict):
        raise FormatError(f"{where} must be a table")
    name = table.get("name")
    if not isinstance(name, str) or not name.strip():
        raise FormatError(f"{where} needs a text 'name'")
    cards = table.get("cards", [])
    if not _string_list(cards):
        raise FormatError(f"{where} ({name}): 'cards' must be a list of card IDs")
    limit = table.get("limit")
    if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool)):
        raise FormatError(f"{where} ({name}): 'limit' must be a number")
    done = table.get("done", False)
    if not isinstance(done, bool):
        raise FormatError(f"{where} ({name}): 'done' must be true or false")
    return ColumnFile(str(name), [str(card) for card in cards], limit, done, table)


def parse_board(text: str, path: Path) -> BoardFile:
    doc = _toml(text, path)
    if doc.get("format") != FORMAT:
        raise FormatError(f"{path}: unknown board format {doc.get('format')!r}; this herdboard reads format {FORMAT}")
    for key in ("repo", "merge"):
        if key in doc and not isinstance(doc[key], str):
            raise FormatError(f"{path}: {key!r} must be text")
    # The Linear tables are checked where they are read, so a bad one leaves the board readable.
    if "column" in doc and not isinstance(doc["column"], list):
        raise FormatError(f"{path}: 'column' must be an array of tables ([[column]])")
    columns = [_column(table, index, path) for index, table in enumerate(doc.get("column", []), start=1)]
    return BoardFile(doc, columns)


def dump_board(board: BoardFile) -> str:
    doc = board.doc
    doc.pop("column", None)
    if board.columns:
        array = tomlkit.aot()
        for column in board.columns:
            array.append(column.to_table())
        doc["column"] = array
    return tomlkit.dumps(doc)
