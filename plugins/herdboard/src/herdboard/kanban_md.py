"""Parse and write Obsidian Kanban boards without changing unknown lines."""

from __future__ import annotations

import json
import re

from .model import CARD_START, Board, Card, Column, FormatError, NotFoundError

SETTINGS_START = "%% kanban:settings"
MARKER_LINE = re.compile(r"^\*\*.+\*\*\r?$")


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


def insert_card(column: Column, card: Card, position: str = "bottom") -> None:
    if position not in ("top", "bottom"):
        raise ValueError(f"position must be 'top' or 'bottom', not {position!r}")
    indexes = [i for i, item in enumerate(column.items) if isinstance(item, Card)]
    if indexes:
        index = indexes[0] if position == "top" else indexes[-1] + 1
    else:
        # Where the plugin writes a first card: after the blank line under the
        # heading and after a `**Complete**`-style marker line.
        index = 0
        if column.items and column.items[0].strip() == "":
            index = 1
            if len(column.items) > 1 and MARKER_LINE.match(column.items[1]):
                index = 2
    column.items.insert(index, card)


def remove_card(column: Column, card: Card) -> None:
    for index, item in enumerate(column.items):
        if item is card:
            del column.items[index]
            return
    raise NotFoundError("Card is not in this column")


def new_board(
    columns: list[str],
    complete_column: str | None,
    frontmatter: list[tuple[str, str]] = (),
) -> str:
    parts = ["---\n\nkanban-plugin: board\n"]
    parts.extend(f"{key}: {value}\n" for key, value in frontmatter)
    parts.append("\n---\n\n")
    for title in columns:
        marker = "**Complete**\n" if title == complete_column else ""
        parts.append(f"## {title}\n\n{marker}\n\n")
    settings = json.dumps({"kanban-plugin": "board"}, separators=(",", ":"))
    parts.append(f"\n\n{SETTINGS_START}\n```\n{settings}\n```\n%%")
    return "".join(parts)


def set_frontmatter_value(text: str, key: str, value: str | None) -> str:
    """`text` with the top-level `key: value` line of the frontmatter set, or removed for None.

    The line keeps its place. A new line goes before the closing `---` (before the blank
    lines that end the frontmatter). Every other line keeps its bytes.
    """
    newline = "\r\n" if "\r\n" in text else "\n"
    lines = text.split(newline)
    end = next((i for i in range(1, len(lines)) if lines[i] == "---"), None)
    if not lines or lines[0] != "---" or end is None:
        raise FormatError("frontmatter must be closed")
    key_line = re.compile(rf"^{re.escape(key)}:(\s|$)")
    matches = [i for i in range(1, end) if key_line.match(lines[i])]
    if len(matches) > 1:
        raise FormatError(f"duplicate key {key!r} in frontmatter")
    found = matches[0] if matches else None
    if found is not None:
        # Blank and comment lines under the key are not part of its value.
        after = [line for line in lines[found + 1:end] if line.strip() and not line.lstrip().startswith("#")]
        if after and after[0][:1] in (" ", "\t", "-"):
            raise FormatError(f"{key!r} has a nested value in frontmatter; it must be one value")
    new = [] if value is None else [f"{key}: {value}"]
    if found is not None:
        lines[found:found + 1] = new
    else:
        position = end
        while position > 1 and not lines[position - 1].strip():
            position -= 1
        lines[position:position] = new
    return newline.join(lines)
