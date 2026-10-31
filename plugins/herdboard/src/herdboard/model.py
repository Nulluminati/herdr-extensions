"""Shared errors and small helpers for herdboard boards and cards."""

from __future__ import annotations

import re

# The column that a new report lands in, before it is sorted: created on first use, on any board.
TRIAGE_COLUMN = "Triage"
# A Linear label such as `type/bug` names a card type, not a tag.
TYPE_TAG_PREFIX = "type/"


class HerdboardError(Exception):
    """A failure that the CLI explains without a traceback."""

    code = "error"
    exit_code = 1


class FormatError(HerdboardError):
    code = "format"


class OldFormatError(HerdboardError):
    """The todos folder still has boards in the old Markdown format."""

    code = "old_format"


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


def is_triage(title: str) -> bool:
    """True for the title of the triage column, in any case."""
    return title.casefold() == TRIAGE_COLUMN.casefold()


# Starts the title of a card that an automation spawned.
AUTOMATION_PREFIX = "Automation: "


def is_automation_title(text: str) -> bool:
    """Whether a card's title text starts with the automation prefix, in any case."""
    return text.lower().startswith(AUTOMATION_PREFIX.lower())


def automation_title(text: str) -> str:
    """The title text with the automation prefix in front, once."""
    return text if is_automation_title(text) else f"{AUTOMATION_PREFIX}{text}"


# Card types whose conventional commit type has another name. Other types are their own commit type.
COMMIT_TYPES = {"bug": "fix", "feature": "feat"}


def commit_type(kind: str) -> str:
    """The conventional commit type of a card type, such as `fix` for `bug`."""
    kind = kind.casefold()
    return COMMIT_TYPES.get(kind, kind)


def plain_tags(tags: list[str]) -> list[str]:
    """The tags without the `type/<name>` tags."""
    return [tag for tag in tags if not tag.startswith(TYPE_TAG_PREFIX)]


def parse_tags(text: str) -> list[str]:
    """Tags typed in a form: separated by spaces or commas, with or without `#`. Duplicates drop."""
    tags: list[str] = []
    for part in re.split(r"[\s,]+", text):
        tag = part.strip().lstrip("#")
        if tag and tag not in tags:
            tags.append(tag)
    return tags
