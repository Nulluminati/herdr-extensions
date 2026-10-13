"""Convert the lines of a todo card into the parts of a Linear issue. No network."""

from __future__ import annotations

from dataclasses import dataclass, field
import re

from .model import BLOCK_ID, CARD_START, DUE_DATE, PARENT_LINK, TAG, HerdboardError

# One level below the card: a tab, or 4 spaces when the vault does not use tabs.
SUB_ITEM = re.compile(r"^(?:\t| {4})- \[(?P<check>.)\] (?P<title>\S.*)$")
NOTE_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
PROMOTED = re.compile(r"→ (?P<identifier>[A-Za-z][A-Za-z0-9]*-\d+)(?!\S)")


@dataclass
class SubItem:
    title: str
    checked: bool


@dataclass
class Conversion:
    title: str
    due: str | None
    tags: list[str]
    # Names of the `[[note]]` links on the first line, in order.
    links: list[str]
    subs: list[SubItem] = field(default_factory=list)
    # Indented lines that are not sub-items, one level of indentation removed.
    leftover: list[str] = field(default_factory=list)


def promoted_identifier(first_line: str) -> str | None:
    """The Linear identifier after `→` on a card's first line, if the card was promoted."""
    found = PROMOTED.search(BLOCK_ID.sub("", first_line))
    return found.group("identifier") if found else None


def _title(first_line: str) -> str:
    text = BLOCK_ID.sub("", CARD_START.sub("", first_line, count=1))
    text = PARENT_LINK.sub("", TAG.sub("", DUE_DATE.sub("", text)))
    return re.sub(r"\s{2,}", " ", text).strip()


def _dedent(line: str) -> str:
    if line.startswith("\t"):
        line = line[1:]
    elif line.startswith("    "):
        line = line[4:]
    else:
        line = line.lstrip()
    return line.rstrip()


def convert(lines: list[str]) -> Conversion:
    """The title, due date, tags, links, sub-items and leftover text of a card."""
    first = lines[0]
    title = _title(first)
    if not title:
        raise HerdboardError("The todo has no text for a title")
    found = Conversion(
        title=title,
        due=(m.group("date") if (m := DUE_DATE.search(first)) else None),
        tags=[m.group("tag") for m in TAG.finditer(first)],
        links=list(dict.fromkeys(m.group(1).strip() for m in NOTE_LINK.finditer(title))),
    )
    for line in lines[1:]:
        item = SUB_ITEM.match(line)
        if item:
            found.subs.append(SubItem(item.group("title").strip(), item.group("check") != " "))
        else:
            found.leftover.append(_dedent(line))
    while found.leftover and not found.leftover[0]:
        found.leftover.pop(0)
    while found.leftover and not found.leftover[-1]:
        found.leftover.pop()
    return found
