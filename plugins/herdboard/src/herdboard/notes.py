"""Scratchpad notes: one Markdown file per board, and the inbox's note for all boards.

The notes are plain Markdown in a folder of the todos, so Obsidian shows them. A note is the
user's own text: no agent or command writes it.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from pathlib import Path
import re
import tempfile

from .config import Config
from .model import HerdboardError, IoError

FOLDER = "notes"
# The note of the inbox board is the global note.
GLOBAL = "inbox"
# The first line of a fenced code block, or its last line. A closing line has nothing but
# spaces after the fence.
FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
CLOSING_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,}) *$")
# The marker of a list item and the spaces after it, up to the item's text.
LIST_ITEM = re.compile(r"^( *)([-*+]|\d{1,9}[.)])( {1,4})(?=\S)")
# A paragraph of one no-break space: one empty row in the preview.
EMPTY_ROW = "&nbsp;"


class NoteChanged(HerdboardError):
    """The note changed on disk after it was read."""

    code = "note_changed"


def notes_dir(config: Config) -> Path:
    return config.todos_dir / FOLDER


def note_path(config: Config, board: str) -> Path:
    """The note of a board, by the board's name (`Store.board_name`)."""
    if "/" in board or board in ("", ".", ".."):
        raise HerdboardError(f"No note for a board named {board!r}")
    return notes_dir(config) / f"{board}.md"


def preview_markdown(text: str) -> str:
    """The note's Markdown for the preview, with each blank line as an empty paragraph.

    Markdown draws a run of blank lines as one gap. The preview shows each blank line as
    one empty row instead, as the note was typed. Blank lines in a code fence stay. In a list,
    the empty paragraph is indented into the list item, so the list and the item stay whole."""
    if not text:
        return ""
    end = "\n" if text.endswith("\n") else ""
    source = (text[:-1] if end else text).split("\n")
    lines: list[str] = []
    # The open list items, outermost first.
    items: list[_ListItem] = []
    # The opening fence of the code block that the line is in: a closing fence is the same
    # character, at least as long.
    fence = None
    after_blank = False
    for index, line in enumerate(source):
        if fence is not None:
            closing = CLOSING_FENCE.match(line)
            if closing and closing.group(1)[0] == fence[0] and len(closing.group(1)) >= len(fence):
                fence = None
        elif not line.strip():
            following = next((later for later in source[index + 1:] if later.strip()), None)
            if not lines or lines[-1]:
                lines.append("")
            lines.extend([" " * _empty_row_indent(items, following) + EMPTY_ROW, ""])
            after_blank = True
            continue
        else:
            _follow_lists(items, line, after_blank)
            match = FENCE.match(line)
            if match:
                fence = match.group(1)
        after_blank = False
        lines.append(line)
    return "\n".join(lines) + end


@dataclass
class _ListItem:
    marker_indent: int
    content_indent: int
    # The bullet character, or the delimiter of an ordered marker: a new kind starts a new list.
    kind: str


def _list_item(line: str) -> tuple[int, _ListItem | None]:
    """The line's indent, and the list item that the line starts, if any."""
    expanded = line.expandtabs(4)
    indent = len(expanded) - len(expanded.lstrip(" "))
    match = LIST_ITEM.match(expanded)
    if match is None:
        return indent, None
    marker = match.group(2)
    return indent, _ListItem(indent, match.end(), marker[-1])


def _follow_lists(items: list[_ListItem], line: str, after_blank: bool) -> None:
    """Close the list items that the line is not part of, and open the item that it starts.

    A plain line straight after another line continues it, even without an indent."""
    indent, item = _list_item(line)
    if item is None and not after_blank:
        return
    while items and indent < items[-1].content_indent:
        items.pop()
    if item is not None:
        items.append(item)


def _empty_row_indent(items: list[_ListItem], following: str | None) -> int:
    """The indent of an empty row, so it stays in the list item that the next line is part of.

    Before a sibling item, the row goes into the item above it, so the list goes on."""
    if following is None:
        return 0
    indent, item = _list_item(following)
    if item is not None:
        for open_item in items:
            if open_item.marker_indent == indent and open_item.kind == item.kind:
                return open_item.content_indent
    inside = [open_item.content_indent for open_item in items if open_item.content_indent <= indent]
    return inside[-1] if inside else 0


def _stamp(path: Path) -> int | None:
    """The file's modification time in nanoseconds, or None when it is missing."""
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None
    except OSError as error:
        raise IoError(f"{path}: cannot read the note: {error}") from error


def read_note(path: Path) -> tuple[str, int | None]:
    """The note's text and modification time. A missing note is empty, with no time."""
    try:
        stamp = path.stat().st_mtime_ns
        text = path.read_bytes().decode("utf-8")
    except FileNotFoundError:
        return "", None
    except (OSError, UnicodeDecodeError) as error:
        raise IoError(f"{path}: cannot read the note: {error}") from error
    return text, stamp


def write_note(path: Path, text: str, expected: int | None, force: bool = False) -> int:
    """Write the note and return its new modification time.

    `expected` is the time that `read_note` gave. When the file changed since then, NoteChanged
    stops the write, unless `force` is true. The first write creates the folder.
    """
    if not force and _stamp(path) != expected:
        raise NoteChanged(f"{path} changed after it was read")
    temp = None
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        # A save keeps the file's mode. A new note gets the mode that the umask gives, as a board does.
        try:
            mode = path.stat().st_mode & 0o7777
        except FileNotFoundError:
            umask = os.umask(0)
            os.umask(umask)
            mode = 0o666 & ~umask
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.chmod(temp, mode)
        os.replace(temp, path)
        temp = None
        return path.stat().st_mtime_ns
    except OSError as error:
        raise IoError(f"{path}: cannot write the note: {error}") from error
    finally:
        if temp is not None:
            Path(temp).unlink(missing_ok=True)
