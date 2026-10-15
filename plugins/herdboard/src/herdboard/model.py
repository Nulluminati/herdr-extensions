"""Shared data types for herdboard boards.

Every element keeps the exact source lines it was parsed from, so a board that
is parsed and serialized without changes gives the same bytes. Mutations change
only the elements they touch.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import re


CARD_START = re.compile(r"^- \[(?P<check>.)\] ")
BLOCK_ID = re.compile(r" \^(?P<id>[A-Za-z0-9-]+)$")
DUE_DATE = re.compile(r"📅 (?P<date>\d{4}-\d{2}-\d{2})")
TAG = re.compile(r"(?<!\S)#(?P<tag>[^\s#]+)")
# A card's type is an Obsidian nested tag, such as `#type/bug`.
TYPE_TAG_PREFIX = "type/"
TYPE_TAG = re.compile(r"(?<!\S)#type/[^\s#]*")
# A sub-card's link to its parent card on the same board: an Obsidian block link.
PARENT_LINK = re.compile(r"\[\[#\^(?P<id>t[0-9a-z]{3})\]\]")


class HerdboardError(Exception):
    """A failure that the CLI explains without a traceback."""

    code = "error"
    exit_code = 1


class FormatError(HerdboardError):
    code = "format"


class ConflictError(HerdboardError):
    """The card changed on disk, or another writer holds the lock."""

    code = "conflict"
    exit_code = 2


class DuplicateIdError(HerdboardError):
    code = "duplicate_id"


class NotFoundError(HerdboardError):
    code = "not_found"


class ArchivedError(NotFoundError):
    """The card is in the board's archive, where commands do not change it."""

    code = "archived"


class CardActiveError(HerdboardError):
    """The card still has work in progress, such as a worktree or a running agent."""

    code = "card_active"


class ParentError(HerdboardError):
    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class UnknownTypeError(HerdboardError):
    """A card type that is not in the config's `types`."""

    code = "unknown_type"


class IoError(HerdboardError):
    """A board, config or note file cannot be read or written."""

    code = "io"


@dataclass
class Card:
    """A `- [c] ` line plus every indented line below it."""

    lines: list[str]

    @property
    def first_line(self) -> str:
        return self.lines[0]

    @property
    def check_char(self) -> str:
        match = CARD_START.match(self.first_line)
        if match is None:
            raise FormatError(f"Not a card line: {self.first_line!r}")
        return match.group("check")

    @property
    def checked(self) -> bool:
        return self.check_char != " "

    @property
    def block_id(self) -> str | None:
        match = BLOCK_ID.search(self.first_line)
        return match.group("id") if match else None

    @property
    def title(self) -> str:
        """First line without the checkbox and the block ID."""
        text = CARD_START.sub("", self.first_line, count=1)
        return BLOCK_ID.sub("", text)

    @property
    def body(self) -> list[str]:
        """Indented lines below the first line, as written."""
        return self.lines[1:]

    @property
    def tags(self) -> list[str]:
        return [match.group("tag") for match in TAG.finditer(self.title)]

    @property
    def due(self) -> str | None:
        match = DUE_DATE.search(self.title)
        return match.group("date") if match else None

    def set_checked(self, checked: bool) -> None:
        mark = "x" if checked else " "
        self.lines[0] = CARD_START.sub(f"- [{mark}] ", self.first_line, count=1)

    def set_block_id(self, block_id: str) -> None:
        if self.block_id is not None:
            raise FormatError(f"Card already has ID {self.block_id}")
        self.lines[0] = f"{self.first_line} ^{block_id}"


@dataclass
class Column:
    """A `## Title` section. `items` keeps cards and other lines in file order."""

    heading_line: str
    items: list[Card | str] = field(default_factory=list)
    archive: bool = False

    @property
    def title(self) -> str:
        """Heading text without `## ` and without a trailing card limit."""
        text = self.heading_line[3:]
        return re.sub(r" \(\d+\)$", "", text)

    @property
    def limit(self) -> int | None:
        match = re.search(r" \((\d+)\)$", self.heading_line)
        return int(match.group(1)) if match else None

    @property
    def cards(self) -> list[Card]:
        return [item for item in self.items if isinstance(item, Card)]

    def has_marker(self, markers: list[str]) -> bool:
        """True when a `**<marker>**` line marks this column complete."""
        wanted = {f"**{marker}**" for marker in markers}
        return any(isinstance(item, str) and item in wanted for item in self.items)


@dataclass
class Board:
    """A whole board file.

    - `frontmatter`: lines from the opening `---` to the closing `---`, inclusive.
    - `preamble`: lines between the frontmatter and the first heading.
    - `columns`: every `## ` section before the archive.
    - `archive_lines`: the `***` line and lines up to `## Archive` (empty when there is no archive).
    - `archive`: the `## Archive` section, or None.
    - `tail`: the `%% kanban:settings` block and everything after it, as raw text.
    - `newline`: the line ending of the file. Lines never contain it.
    """

    frontmatter: list[str]
    preamble: list[str]
    columns: list[Column]
    archive_lines: list[str]
    archive: Column | None
    tail: str
    newline: str = "\n"

    def frontmatter_value(self, key: str) -> str | None:
        return frontmatter_scalars(self.frontmatter).get(key)

    def all_cards(self) -> list[tuple[Column, Card]]:
        """Cards in file order, without archived cards."""
        return [(column, card) for column in self.columns for card in column.cards]

    def column(self, title: str) -> Column:
        for column in self.columns:
            if column.title.casefold() == title.casefold():
                return column
        raise NotFoundError(f"No column named {title!r}")


def frontmatter_scalars(lines: list[str]) -> dict[str, str]:
    """Top-level `key: value` pairs from frontmatter lines.

    Only plain scalars are read. Lists and mappings are ignored here; the
    Linear steps read them with a YAML parser.
    """
    values: dict[str, str] = {}
    for line in lines:
        match = re.match(r"^([A-Za-z0-9_-]+):\s*(\S.*?)\s*$", line)
        if match:
            values[match.group(1)] = match.group(2).strip("'\"")
    return values


def parent_of(title: str) -> str | None:
    """The card ID that a sub-card's `[[#^id]]` link names, or None."""
    match = PARENT_LINK.search(title)
    return match.group("id") if match else None


def with_parent(title: str, parent: str | None) -> str:
    """The first-line text with its parent link removed, then `[[#^parent]]` added at the end."""
    text = " ".join(PARENT_LINK.sub("", title).split())
    return f"{text} [[#^{parent}]]" if parent else text


def split_title(title: str) -> tuple[str, list[str], str | None]:
    """A card's first-line text as (text, tags, due date). Tags, the date and the parent link leave the text."""
    tags = [match.group("tag") for match in TAG.finditer(title)]
    due_match = DUE_DATE.search(title)
    text = PARENT_LINK.sub("", DUE_DATE.sub("", TAG.sub("", title)))
    return " ".join(text.split()), tags, due_match.group("date") if due_match else None


def join_title(text: str, tags: list[str], due: str | None) -> str:
    """The first-line text of a card: text, then `#tags`, then `📅 date`."""
    text = " ".join(text.split())
    if not text:
        raise HerdboardError("A card needs a title")
    parts = [text, *(f"#{tag}" for tag in tags)]
    if due:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", due):
            raise HerdboardError(f"Due date {due!r} is not YYYY-MM-DD")
        try:
            date.fromisoformat(due)
        except ValueError as error:
            raise HerdboardError(f"Due date {due!r} is not a real date") from error
        parts.append(f"📅 {due}")
    return " ".join(parts)


def card_type(tags: list[str]) -> str | None:
    """The name in the first `type/<name>` tag, or None."""
    for tag in tags:
        if tag.startswith(TYPE_TAG_PREFIX) and len(tag) > len(TYPE_TAG_PREFIX):
            return tag[len(TYPE_TAG_PREFIX):]
    return None


def plain_tags(tags: list[str]) -> list[str]:
    """The tags without the `type/<name>` tags."""
    return [tag for tag in tags if not tag.startswith(TYPE_TAG_PREFIX)]


def with_type(title: str, kind: str | None) -> str:
    """The first-line text with its type tags removed, then `#type/<kind>` added before the other tags."""
    text = " ".join(TYPE_TAG.sub("", title).split())
    if not kind:
        return text
    tag = f"#{TYPE_TAG_PREFIX}{kind}"
    starts = [m.start() for p in (TAG, DUE_DATE, PARENT_LINK) if (m := p.search(text))]
    if not starts:
        return f"{text} {tag}"
    at = min(starts)
    return f"{text[:at]}{tag} {text[at:]}"


def parse_tags(text: str) -> list[str]:
    """Tags typed in a form: separated by spaces or commas, with or without `#`. Duplicates drop."""
    tags: list[str] = []
    for part in re.split(r"[\s,]+", text):
        tag = part.strip().lstrip("#")
        if tag and tag not in tags:
            tags.append(tag)
    return tags

