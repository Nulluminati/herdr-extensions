"""Scratchpad notes: one Markdown file per board, and the inbox's note for all boards.

The notes are plain Markdown in a folder of the todos, so Obsidian shows them. A note is the
user's own text: no agent or command writes it.
"""

from __future__ import annotations

import os
from pathlib import Path
import tempfile

from .config import Config
from .model import HerdboardError, IoError

FOLDER = "notes"
# The note of the inbox board is the global note.
GLOBAL = "inbox"


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
        fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
        with os.fdopen(fd, "wb") as handle:
            handle.write(text.encode("utf-8"))
        os.replace(temp, path)
        temp = None
        return path.stat().st_mtime_ns
    except OSError as error:
        raise IoError(f"{path}: cannot write the note: {error}") from error
    finally:
        if temp is not None:
            Path(temp).unlink(missing_ok=True)
