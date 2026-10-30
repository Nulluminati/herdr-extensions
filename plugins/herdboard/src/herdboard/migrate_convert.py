"""Convert a free-form `TODO.md` into sections and cards. Pure: no files, no Textual."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field

from .model import BLOCK_ID

# Markdown allows up to three leading spaces before a heading; four spaces or a tab make text.
ATX_HEADING = re.compile(r"^ {0,3}#{1,6}\s+(?P<name>.*?)\s*$")
FENCE_OPEN = re.compile(r"^(?P<indent>[ \t]*)(?P<fence>`{3,}|~{3,})(?P<info>.*)$")
UNDERLINE = re.compile(r"^(?:=+|-{2,})\s*$")
LIST_ITEM = re.compile(
    r"^(?P<indent>[ \t]*)(?:[-*+]|\d+[.)])[ \t]+(?:\[(?P<box>[ xX])\][ \t]+)?(?P<text>\S.*?)\s*$"
)
EMPTY_ITEM = re.compile(r"^[ \t]*(?:[-*+]|\d+[.)])(?:[ \t]+\[[ xX]\])?\s*$")
SLUG_BREAK = re.compile(r"[\W_]+")


def slug(name: str) -> str | None:
    """Lowercase tag name: runs of other characters become one `-`. None when nothing is left."""
    return SLUG_BREAK.sub("-", name.casefold()).strip("-") or None


@dataclass
class Item:
    """A card: a top-level list item with its nested lines (tab indented)."""

    title: str
    tag: str | None
    checked: bool
    body: list[str] = field(default_factory=list)
    # A block ID (` ^id`) that ended the title in the source. It is not kept: the store allocates one.
    dropped_id: str | None = None

    @property
    def first_line(self) -> str:
        mark = "x" if self.checked else " "
        tag = f" #{self.tag}" if self.tag else ""
        return f"- [{mark}] {self.title}{tag}"

    def card_lines(self) -> list[str]:
        return [self.first_line, *self.body]


@dataclass
class Section:
    name: str | None
    tag: str | None
    items: list[Item] = field(default_factory=list)


@dataclass
class NonListLine:
    """A line that needs a look. `action` is `attached` or `card` (a text line that is not a list
    item), or `ambiguous` (a list item whose indent is not a clean level; `parent` is the item
    it was put under, None for a card)."""

    number: int
    text: str
    action: str
    section: str | None
    parent: str | None = None


@dataclass
class Conversion:
    sections: list[Section]
    non_list: list[NonListLine]
    # Smallest leading-space step of the file; None when no list line is indented with spaces.
    indent_unit: int | None

    @property
    def items(self) -> list[Item]:
        return [item for section in self.sections for item in section.items]


def _detect_unit(lines: list[str]) -> int | None:
    """The smallest space indent. An isolated one-space indent is ignored when the other space
    indents are all multiples of a larger step, or when the rest of the file is indented with tabs.
    Prefixes that mix tabs and spaces do not count."""
    steps: set[int] = set()
    has_tab = False
    for line in lines:
        match = LIST_ITEM.match(line)
        if not match:
            continue
        indent = match.group("indent")
        if "\t" in indent:
            has_tab = has_tab or " " not in indent
        elif indent:
            steps.add(len(indent))
    others = steps - {1}
    if 1 in steps:
        if others and math.gcd(*others) > 1:
            steps = others
        elif not others and has_tab:
            return None
    return min(steps) if steps else None


def _is_list_line(line: str) -> bool:
    return bool(LIST_ITEM.match(line) or EMPTY_ITEM.match(line))


def _heading_name(text: str) -> str:
    text = re.sub(r"\s+#+$", "", text.strip())
    return text.rstrip(":").strip()


class _Converter:
    def __init__(self, lines: list[str]):
        self.lines = lines
        self.unit = _detect_unit(lines)
        self.sections: list[Section] = []
        self.non_list: list[NonListLine] = []
        self.section = Section(None, None)
        # Raw indent levels of the open items; its length minus one is the depth.
        self.stack: list[int] = []
        # Titles of the open items, parallel to `stack`.
        self.titles: list[str] = []
        self.card: Item | None = None
        # The open code fence: its character, its length, its indent and the card depth.
        self.fence: tuple[str, int, str, int] | None = None

    def level(self, indent: str) -> tuple[int, bool]:
        """The indent level of a prefix and whether that level is ambiguous.

        Characters count in order. With a space unit, a tab advances to the next multiple of the
        unit. With a tab unit, a tab is one level and any space makes the level ambiguous.
        """
        if not self.unit:
            tabs = indent.count("\t")
            return (tabs or 1, True) if " " in indent else (tabs, False)
        column = 0
        for char in indent:
            column = column + 1 if char == " " else (column // self.unit + 1) * self.unit
        return (max(1, column // self.unit) if column else 0), column % self.unit != 0

    def start_section(self, name: str | None) -> None:
        self.finish_section()
        self.section = Section(name, slug(name) if name else None)

    def finish_section(self) -> None:
        if self.section.items:
            self.sections.append(self.section)
        self.stack = []
        self.titles = []
        self.card = None

    def next_nonblank(self, index: int) -> str | None:
        for line in self.lines[index + 1 :]:
            if line.strip():
                return line
        return None

    def add_card(self, title: str, checked: bool) -> Item:
        dropped = BLOCK_ID.search(title)
        if dropped:
            title = title[: dropped.start()].rstrip()
        item = Item(title, self.section.tag, checked, dropped_id=dropped.group("id") if dropped else None)
        self.section.items.append(item)
        self.card = item
        return item

    def add_item(self, number: int, match: re.Match) -> None:
        raw, ambiguous = self.level(match.group("indent"))
        checked = (match.group("box") or " ") != " "
        while self.stack and self.stack[-1] >= raw:
            self.stack.pop()
            self.titles.pop()
        parent = self.titles[-1] if self.stack and self.card is not None else None
        if ambiguous:
            self.non_list.append(
                NonListLine(number, match.group("text"), "ambiguous", self.section.name, parent)
            )
        if not self.stack or self.card is None:
            self.stack = [raw]
            self.titles = [self.add_card(match.group("text"), checked).title]
            return
        self.stack.append(raw)
        self.titles.append(match.group("text"))
        depth = len(self.stack) - 1
        box = f"[{'x' if checked else ' '}] " if match.group("box") else ""
        self.card.body.append("\t" * depth + f"- {box}{match.group('text')}")

    def add_text(self, number: int, line: str) -> int:
        text = line.strip()
        indent = line[: len(line) - len(line.lstrip())]
        if self.card is None:
            self.titles = [self.add_card(text, False).title]
            self.stack = [0]
            action = "card"
            tabs = 1
        else:
            depth = len(self.stack)
            tabs = max(1, min(self.level(indent)[0], depth))
            self.card.body.append("\t" * tabs + text)
            action = "attached"
        self.non_list.append(NonListLine(number, text, action, self.section.name))
        return tabs

    def open_fence(self, number: int, line: str, match: re.Match) -> None:
        fence = match.group("fence")
        tabs = self.add_text(number, line)
        self.fence = (fence[0], len(fence), match.group("indent"), tabs)

    def fence_line(self, number: int, line: str) -> None:
        """A line inside an open fence: kept as text of the card, whatever its syntax."""
        char, length, indent, tabs = self.fence
        if re.match(rf"^[ \t]*{re.escape(char)}{{{length},}}[ \t]*$", line):
            self.fence = None
        text = (line[len(indent):] if line.startswith(indent) else line.lstrip()).rstrip()
        self.card.body.append("\t" * tabs + text)
        if text:
            self.non_list.append(NonListLine(number, text, "attached", self.section.name))

    def run(self) -> Conversion:
        skip = False
        for index, line in enumerate(self.lines):
            if skip:
                skip = False
                continue
            number = index + 1
            if self.fence:
                self.fence_line(number, line)
                continue
            fence = FENCE_OPEN.match(line)
            if fence and not (fence.group("fence")[0] == "`" and "`" in fence.group("info")):
                self.open_fence(number, line, fence)
                continue
            if not line.strip() or EMPTY_ITEM.match(line):
                continue
            atx = ATX_HEADING.match(line)
            if atx:
                self.start_section(_heading_name(atx.group("name")))
                continue
            item = LIST_ITEM.match(line)
            if item:
                self.add_item(number, item)
                continue
            following = self.next_nonblank(index)
            unindented = line == line.lstrip()
            lead = len(line) - len(line.lstrip(" "))
            heading_text = lead <= 3 and not line[lead:].startswith("\t")
            if heading_text and index + 1 < len(self.lines) and UNDERLINE.match(self.lines[index + 1]):
                self.start_section(_heading_name(line))
                skip = True
            elif unindented and line.rstrip().endswith(":") and following and _is_list_line(following):
                self.start_section(_heading_name(line))
            elif UNDERLINE.match(line):
                continue
            else:
                self.add_text(number, line)
        while self.fence and not self.card.body[-1].strip():
            self.card.body.pop()
        self.finish_section()
        return Conversion(self.sections, self.non_list, self.unit)


def convert(text: str) -> Conversion:
    return _Converter(re.split(r"\r?\n", text)).run()
