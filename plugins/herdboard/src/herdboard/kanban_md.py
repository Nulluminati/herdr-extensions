"""Read the old Obsidian Kanban Markdown boards, for `convert-store` only.

herdboard stored boards in this format before the folder layout of `card_file`. Only
`convert_store` imports this module. The parser keeps every source line, so a board that is
parsed and serialized without changes gives the same bytes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import FormatError, NotFoundError

CARD_START = re.compile(r"^- \[(?P<check>.)\] ")
BLOCK_ID = re.compile(r" \^(?P<id>[A-Za-z0-9-]+)$")
DUE_DATE = re.compile(r"📅 (?P<date>\d{4}-\d{2}-\d{2})")
TAG = re.compile(r"(?<!\S)#(?P<tag>[^\s#]+)")
# A card's type was an Obsidian nested tag, such as `#type/bug`.
TYPE_TAG_PREFIX = "type/"
# A sub-card's link to its parent card on the same board: an Obsidian block link.
PARENT_LINK = re.compile(r"\[\[#\^(?P<id>t[0-9a-z]{3})\]\]")
# The Linear issue that `promote` made from the card, after an arrow on the first line.
PROMOTED = re.compile(r"→ (?P<identifier>[A-Za-z][A-Za-z0-9]*-\d+)(?!\S)")
SETTINGS_START = "%% kanban:settings"
MARKER_LINE = re.compile(r"^\*\*.+\*\*\r?$")


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


def _is(line: str, text: str) -> bool:
    return line.rstrip("\r") == text


def _parse_items(lines: list[str]) -> list[Card | str]:
    items: list[Card | str] = []
    for line in lines:
        if CARD_START.match(line):
            items.append(Card([line]))
        elif items and isinstance(items[-1], Card) and line.startswith(("\t", " ")):
            items[-1].lines.append(line)
        else:
            items.append(line)
    return items


def _find_archive(lines: list[str], archive_titles: tuple[str, ...]) -> tuple[int, int] | None:
    """Indexes of the `***` line and the archive heading that follows it.

    As in the Obsidian Kanban plugin, a heading is the archive only when its
    title is in `archive_titles` and the previous non-blank line is `***`.
    """
    seen_column = False
    for index, line in enumerate(lines):
        if not line.startswith("## "):
            continue
        if seen_column and line[3:] in archive_titles:
            before = index - 1
            while before >= 0 and lines[before].strip() == "":
                before -= 1
            if before >= 0 and _is(lines[before], "***"):
                return before, index
        seen_column = True
    return None


def parse(text: str, archive_titles: tuple[str, ...] = ("Archive",)) -> Board:
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    tail = ""
    for index, line in enumerate(lines):
        if line.startswith(SETTINGS_START):
            tail = newline.join(lines[index:])
            lines = lines[:index]
            break

    frontmatter: list[str] = []
    start = 0
    if lines and _is(lines[0], "---"):
        for index in range(1, len(lines)):
            if _is(lines[index], "---"):
                frontmatter = lines[: index + 1]
                start = index + 1
                break

    body = lines[start:]
    archive_at = _find_archive(body, archive_titles)
    star_index, heading_index = archive_at if archive_at else (len(body), len(body))

    preamble: list[str] = []
    archive_lines: list[str] = []
    sections: list[tuple[str, list[str], bool]] = []  # heading, body, is_archive
    for index, line in enumerate(body):
        if index == heading_index:
            sections.append((line, [], True))
        elif star_index <= index < heading_index:
            archive_lines.append(line)
        elif line.startswith("## ") and index < heading_index:
            sections.append((line, [], False))
        elif sections:
            sections[-1][1].append(line)
        else:
            preamble.append(line)

    columns: list[Column] = []
    archive: Column | None = None
    for heading, section_body, is_archive in sections:
        column = Column(heading, _parse_items(section_body), archive=is_archive)
        if is_archive:
            archive = column
        else:
            columns.append(column)
    return Board(frontmatter, preamble, columns, archive_lines, archive, tail, newline)


def _column_lines(column: Column) -> list[str]:
    lines = [column.heading_line]
    for item in column.items:
        if isinstance(item, Card):
            lines.extend(item.lines)
        else:
            lines.append(item)
    return lines


def serialize(board: Board) -> str:
    lines = list(board.frontmatter) + list(board.preamble)
    for column in board.columns:
        lines.extend(_column_lines(column))
    lines.extend(board.archive_lines)
    if board.archive is not None:
        lines.extend(_column_lines(board.archive))
    if board.tail:
        lines.append(board.tail)
    return board.newline.join(lines)
