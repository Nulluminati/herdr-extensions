"""Where a card's own data lives: the folder that holds its log and its attachments.

- A todo: its card folder, `boards/<board>/cards/<id>/` or `boards/<board>/archive/<id>/`.
- An automation: `.herdboard/automations/<id>/`.
- A Linear issue: `.herdboard/linear/<IDENTIFIER>/`. Linear cards have no card folder.

A todo's folder moves on archive, unarchive and move to another board. Find it while
the store lock is held, and use it before the lock is let go.
"""

from __future__ import annotations

import re
from pathlib import Path

from .config import Config
from .model import DuplicateIdError, HerdboardError, NotFoundError

# A todo ID (`t3m9`), an automation ID (`a7k2`) or a Linear identifier (`ENG-123`): all are safe file names.
LOG_REF = re.compile(r"^(t[0-9a-z]{3}|a[0-9a-z]{3}|[A-Z][A-Z0-9]*-\d+)$")
TODO_REF = re.compile(r"^t[0-9a-z]{3}$")
AUTOMATION_REF = re.compile(r"^a[0-9a-z]{3}$")


class LogUsageError(HerdboardError):
    code = "usage"


def data_dir(config: Config) -> Path:
    return config.todos_dir / ".herdboard"


def todo_dirs(config: Config, ref: str) -> list[Path]:
    """Every folder of the todo `ref`, on a board or in an archive. More than one is a duplicate ID."""
    boards = config.todos_dir / "boards"
    return sorted(boards.glob(f"*/cards/{ref}/card.md")) + sorted(boards.glob(f"*/archive/{ref}/card.md"))


def card_dir(config: Config, ref: str) -> Path:
    """The folder of the card `ref`. Raises NotFoundError for a todo that has no folder,
    and DuplicateIdError for a todo ID that names two folders."""
    if not LOG_REF.match(ref):
        raise LogUsageError(f"{ref!r} is not a card ID: use a todo ID or a Linear identifier")
    if AUTOMATION_REF.match(ref):
        return data_dir(config) / "automations" / ref
    if not TODO_REF.match(ref):
        return data_dir(config) / "linear" / ref
    found = todo_dirs(config, ref)
    if not found:
        raise NotFoundError(f"No card {ref}")
    if len(found) > 1:
        folders = ", ".join(str(path.parent) for path in found)
        raise DuplicateIdError(f"{ref} names more than one card folder: {folders}. Run `herdboard doctor`")
    return found[0].parent
