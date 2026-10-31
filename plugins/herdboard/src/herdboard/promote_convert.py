"""Convert a todo card into the parts of a Linear issue. No network."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .model import HerdboardError

# A checklist item at the top level of the card body.
SUB_ITEM = re.compile(r"^- \[(?P<check>.)\] (?P<title>\S.*)$")


@dataclass
class SubItem:
    title: str
    checked: bool


@dataclass
class Conversion:
    title: str
    due: str | None
    # The card's type first, then its tags: each can become a Linear label.
    tags: list[str]
    subs: list[SubItem] = field(default_factory=list)
    # Body lines that are not sub-items.
    leftover: list[str] = field(default_factory=list)


def promoted_identifier(card) -> str | None:
    """The Linear identifier that a todo was promoted to, or None."""
    return card.promoted


def convert(card) -> Conversion:
    """The title, due date, tags, sub-items and leftover text of a todo card."""
    title = " ".join(card.title.split())
    if not title:
        raise HerdboardError("The todo has no text for a title")
    found = Conversion(title=title, due=card.due, tags=([card.type] if card.type else []) + list(card.tags))
    for line in card.body:
        item = SUB_ITEM.match(line)
        if item:
            found.subs.append(SubItem(item.group("title").strip(), item.group("check") != " "))
        else:
            found.leftover.append(line.rstrip())
    while found.leftover and not found.leftover[0]:
        found.leftover.pop(0)
    while found.leftover and not found.leftover[-1]:
        found.leftover.pop()
    return found
