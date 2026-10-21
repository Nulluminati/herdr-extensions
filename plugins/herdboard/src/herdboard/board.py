"""Textual kanban board. Every file change goes through `Store`."""

from __future__ import annotations

import asyncio
import functools
import importlib.metadata
import re
import subprocess
import sys
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from pathlib import Path
import threading
import time
import zlib
from typing import Callable

from rich.console import Group
from rich.style import Style
from rich.table import Table
from rich.text import Text
from textual import events
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual._context import NoActiveAppError
from textual.driver import Driver
from textual.errors import NoWidget
from textual.message import Message
from textual.drivers.linux_driver import LinuxDriver
from textual.screen import ModalScreen, Screen
from textual.widgets import Input, Label, Markdown, OptionList, Select, Static, TabbedContent, TabPane, TextArea
from textual.worker import Worker, WorkerState

from . import activity, attachments, notes, pr_reviews
from .linear import LinearAuthError
from .linear_cards import LINEAR_REF
from .linear_links import ProjectLink, with_link
from .linear_pins import IssuePin, add_pin, unpin_issue
from .linear_writes import LinearWriter
from .model import HerdboardError, join_title, parse_tags, plain_tags, split_title
from .store import ARCHIVE_REF, KEEP, TEMP_REF, BoardMeta, CardView, Store
from . import agent_states
from . import start as start_module
from .config import THINKING_LEVELS, check_global_config, save_global_config
from . import merge as merge_module
from .merge import Merger
from .agent_review import AgentReviewer
from .plan import Planner, mark_planning
from .linear_states import StateCancelledError
from .promote import Promoter
from .promote_convert import promoted_identifier
from .review import Reviewer
from .start import Starter, mark_removed, pi_model_choices
from .views import LinearResult

VIEWS = ["repo", "inbox", "all", "mine"]
RELOAD_SECONDS = 2.0
LINEAR_SECONDS = 300.0
WRITE_GROUP = "store-write"
LINEAR_GROUP = "linear-fetch"
START_GROUP = "card-start"
PR_GROUP = "pull-requests"
PR_SCREEN = "pull-requests"
NOTES_SCREEN = "notes"
# How often the pull request list reads GitHub again, the same as the PR state check of the board.
PR_SECONDS = 60.0
# Each pull request state: its Nerd Font icon, its plain icon, and its style. The prefix names where
# the state comes from: `ci:` GitHub checks, `gh:` GitHub reviews, `agent:` the local agent review.
PR_STATES = {
    "open": ("", "●", "green"),
    "draft": ("", "◌", "dim"),
    "ci:passed": ("", "✓", "green"),
    "ci:failed": ("", "✗", "bold red"),
    "ci:pending": ("", "•", "yellow"),
    "ci:skipped": ("", "-", "dim"),
    "gh:approved": ("", "✔", "bold green"),
    "gh:changes requested": ("", "✖", "bold red"),
    "gh:review required": ("", "○", "yellow"),
    "gh:commented": ("", "✎", ""),
    "gh:dismissed": ("", "-", "dim"),
    "gh:pending": ("", "○", "dim"),
    "gh:requested": ("", "◆", "bold yellow"),
    "agent:reviewing": ("", "…", "bold cyan"),
    "agent:reviewed": ("", "✓", "bold cyan"),
    "agent:failed": ("", "✗", "bold red"),
    "agent:new commits": ("", "↑", "bold magenta"),
    "notes": ("", "✎", ""),
    "author": ("", "", ""),
    "branch": ("", "", ""),
    "agent": ("", "", ""),
    "commit": ("", "", ""),
    "workspace": ("", "", ""),
    "issue": ("", "#", ""),
    "github": ("", "", "bold"),
}
# At most this many labels follow a title in the list; the details show them all.
PR_LIST_LABELS = 2
DETAIL_LOG_ENTRIES = 10
PR_STYLES = {"merged": "bold green", "closed": "bold yellow"}
LINEAR_OFFLINE_MESSAGE = "Linear cards are read only offline"
LINEAR_WRITER_MESSAGE = "Linear writes are not available"
LINEAR_MARK = "◆"
# Shown in the top-right corner of a card that has attachments.
ATTACHMENT_MARK = "files"
# Ends a card's title. A click on it opens the card menu, also where Herdr keeps right-click for itself.

# Nerd Font glyphs shared by board cards and detail screens. Labels remain readable without them.
ICONS = {
    "type/bug": "\uf188",
    "type/feature": "\uf0eb",
    "type/chore": "\uf0ad",
    "type/docs": "\uf02d",
    "type/refactor": "\uf021",
    "date": "\uf073",
    "attachment": "\uf0c6",
    "branch": "\ue0a0",
    "pr": "\uf407",
    "sub_cards": "\uf0ae",
    "comments": "\uf27b",
    "phase/planning": "\uf040",
    "phase/planned": "\uf046",
    "phase/reviewing": "\uf002",
    "phase/reviewed": "\uf058",
    "phase/new commits": PR_STATES["agent:new commits"][0],
}
# The plain glyphs of the planning and review phases on a card's status row.
PHASE_ICONS = {"planning": "✎", "planned": "☑", "reviewing": "⌕", "reviewed": "✔", "new commits": "↑"}


# The help screen: a title, then its keys. A screen's keys are in its own section; the keys of
# every screen are in "Everywhere".
HELP_SECTIONS = [
    ("Board", [
        ("left, h", "previous column"),
        ("right, l", "next column"),
        ("up, k", "previous card"),
        ("down, j", "next card"),
        ("/", "filter: words match title, tags and details; #tag matches a tag, #type/bug a type; enter keeps it"),
        ("escape", "clear the filter"),
        ("enter", "card detail"),
        ("v", "cycle view: repo, inbox, all, mine"),
        ("t", "hide or show Linear cards"),
        ("i", "create the board for this repository"),
        ("I", "search Linear (on enter) and pin an issue to this repository's board"),
        ("u", "remove the pin of a pinned Linear issue"),
        ("c", "config: display styles, board merge mode, linked Linear projects and pinned issues (ctrl+s saves)"),
        ("r", "reload, and fetch Linear again"),
        ("G", "go to the next tab: Board, Pull requests, Notes; a click on a tab opens it"),
    ]),
    ("Cards", [
        ("shift+left, H", "move card to previous column (a Linear card needs a connection)"),
        ("shift+right, L", "move card to next column (a Linear card needs a connection)"),
        ("shift+up, K", "move the todo up in its column"),
        ("shift+down, J", "move the todo down in its column"),
        ("a", "add a card to the selected column: title, type, tags, due date, details, and the board"),
        ("d", "mark the card done (a Linear card moves to its completed state)"),
        ("e", "edit the todo: title, type, tags, due date and details (ctrl+s saves)"),
        ("b", "move the todo to another board, for example from the inbox to this repository"),
        ("x", "delete the todo, after you confirm"),
        ("A", "archive the todo; in the archive column, move it back to the done column (an archived parent too, after you confirm)"),
        ("D", "archive every card in the done column, after you confirm"),
        ("T", "promote the todo to a Linear issue: choose its team and project; its checklist and sub-cards "
              "become sub-issues, and the todo moves to Done"),
        ("z", "show or hide the archive column"),
        ("p", "set or remove the todo's parent: sub-cards show ↳ parent, a parent shows its done count"),
    ]),
    ("Agents and branches", [
        ("s", "start work with the default agent"),
        ("S", "choose claude or pi (and pi's model and thinking level), then start work"),
        ("P", "plan the card with an agent in a tab in its worktree, or in its card folder without a repository"),
        ("g", "go to the started card's Herdr workspace"),
        ("R", "review a started card: in Hunk next to its agent, by another agent, or both"),
        ("m", "merge the card branch into its base (local, or a draft PR that an agent describes), after you confirm"),
        ("f", "finish a merged card: remove its worktree and branch, move it to Done, after you confirm"),
    ]),
    ("Card detail", [
        ("up, k", "previous checklist item"),
        ("down, j", "next checklist item"),
        ("space", "check or clear the checklist item"),
        ("e", "edit the todo"),
        ("o", "open an attachment"),
    ]),
    ("Pull requests", [
        ("up, k", "previous pull request"),
        ("down, j", "next pull request"),
        ("enter", "its details, the agent review and the description"),
        ("/", "search GitHub (words, author:NAME, draft:true, review-requested:@me)"),
        ("escape", "close the search box, else clear the search, else go back to the board"),
        ("n", "the next page of 50"),
        ("p", "the previous page of 50"),
        ("R", "choose an agent and its model; it reviews the PR with notes in Hunk"),
        ("g", "go to its review workspace"),
        ("u", "Update: fetch new commits, open Hunk again with the old notes, offer a new agent review"),
        ("f", "finish the review: close the workspace, remove the worktree and pr/N branch"),
        ("o", "open it in the browser"),
        ("r", "read GitHub again"),
        ("G", "go to the Notes tab"),
    ]),
    ("Notes", [
        ("e, enter", "edit the note; it saves 2 seconds after the last change"),
        ("t", "show the global note, or go back to the board's note"),
        ("up, k", "scroll up"),
        ("down, j", "scroll down"),
        ("r", "read the note file again"),
        ("escape", "in the editor, save and show the note; else go back to the board"),
        ("ctrl+s", "in the editor, save now"),
        ("G", "go back to the board"),
    ]),
    ("Pull request detail", [
        ("R", "review the pull request with an agent"),
        ("g", "go to its review workspace"),
        ("u", "fetch new commits"),
        ("f", "finish the review"),
        ("o", "open it in the browser"),
    ]),
    ("Everywhere", [
        ("?", "this help, on the board, on the pull request list and on the notes"),
        ("escape", "close a screen or cancel input"),
        ("q", "quit"),
    ]),
    ("Mouse", [
        ("click", "select a card, or a column on its header"),
        ("double-click", "card detail; there, a click checks a checklist item or opens an attachment"),
        ("right-click", "the card's actions; Herdr sends right-clicks only when its \"send right clicks to pane\" is on"),
        ("drag", "move the card to the bottom of another column"),
    ]),
    ("Icons", [
        ("◐", "an agent works (yellow), as in the Herdr sidebar"),
        ("×", "an agent is blocked and waits for you (red)"),
        ("✓", "an agent is done and you did not look yet (cyan); a card in the completed column (green)"),
        ("○", "an agent is idle (green); a card that is not started"),
        ("·", "Herdr does not know the agent's state (dim)"),
        ("●", "a started card without a live agent (yellow)"),
        ("✎", "a planner runs, then its state and agent"),
        ("☑", "the card has a plan"),
        ("⌕", "an agent review runs, then the reviewer's state and agent"),
        ("✔", "the agent review findings are ready"),
        ("↑", "new commits since the agent review"),
        ("nerd", "with the Nerd Font icon style, the phase icons are Nerd Font glyphs; the agent states stay the same"),
    ]),
]
HELP_KEYS = [entry for _title, entries in HELP_SECTIONS for entry in entries]


@dataclass
class ColumnData:
    name: str
    limit: int | None = None
    completed: bool = False
    cards: list[CardView] = field(default_factory=list)
    archive: bool = False

    @property
    def count_label(self) -> str:
        count = len(self.cards)
        return f"{count}" if self.limit is None else f"{count}/{self.limit}"

    @property
    def header(self) -> str:
        return f"{self.name} {self.count_label}"


# Tag chip colors: terminal palette names, so ANSI themes use the terminal's own colors.
CHIP_COLORS = ["blue", "magenta", "cyan", "green", "yellow", "bright_blue", "bright_magenta", "bright_cyan"]
# The RGB that an outline tag's tint mixes, for each chip color: a terminal color has no RGB of its own.
CHIP_RGB = {
    "blue": (59, 142, 234), "magenta": (188, 63, 188), "cyan": (17, 168, 205), "green": (13, 188, 121),
    "yellow": (229, 229, 16), "bright_blue": (59, 142, 234), "bright_magenta": (214, 112, 214),
    "bright_cyan": (41, 184, 219),
}


def chip_color(tag: str) -> str:
    """The same tag always gets the same color."""
    return CHIP_COLORS[zlib.crc32(tag.casefold().encode()) % len(CHIP_COLORS)]


def chip_tint(color: str, dark: bool = True) -> str:
    """A faint tint of a chip color on a dark or a light background, as a hex color."""
    base = (24, 24, 24) if dark else (250, 250, 250)
    red, green, blue = (round(b + (c - b) * 0.22) for c, b in zip(CHIP_RGB[color], base))
    return f"#{red:02x}{green:02x}{blue:02x}"


# Private Use Area codepoints of the "Herdr Agent Icons Max" font, as the agent-icons plugin uses them.
AGENT_FONT = "Herdr Agent Icons Max"
AGENT_LOGOS = {"claude": "\ue1a0", "pi": "\ue1a9"}


@functools.cache
def agent_font_installed() -> bool:
    """Whether Fontconfig has the agent logo font. fc-match always returns some font, so compare the family."""
    try:
        output = subprocess.run(["fc-match", "--format", "%{family}\n", AGENT_FONT], check=True,
                                capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return any(family.strip() == AGENT_FONT for family in output.replace(",", "\n").splitlines())


def agent_label(agent: str, agent_logos: bool) -> str:
    """The agent's name, after its logo when the logos show and the agent has one."""
    logo = AGENT_LOGOS.get(agent) if agent_logos else None
    return f"{logo} {agent}" if logo else agent


def icon_label(key: str, label: str, icon_style: str) -> str:
    """Add an optional icon without replacing the label or inventing unknown icons."""
    icon = ICONS.get(key) if icon_style == "nerd" else None
    return f"{icon} {label}" if icon else label


def due_label(due: str, today: date | None = None, icon_style: str = "plain") -> Text:
    """`Due Nov 1`, red when past, yellow within two days, with an optional calendar icon."""
    today = today or date.today()
    try:
        day = date.fromisoformat(due)
    except ValueError:
        return Text(icon_label("date", f"Due {due}", icon_style), style="dim")
    shown = f"{day:%b} {day.day}" + ("" if day.year == today.year else f" {day.year}")
    if day < today:
        style = "bold red"
    elif (day - today).days <= 2:
        style = "yellow"
    else:
        style = "dim"
    return Text(icon_label("date", f"Due {shown}", icon_style), style=style)


# Type badge colors: terminal palette names. Other types are bold in the default color.
TYPE_STYLES = {"bug": "bold red", "feature": "bold green", "docs": "bold cyan", "refactor": "bold magenta"}


def type_badge(kind: str, icon_style: str = "plain") -> Text:
    """A card type as bold text, colored for the default types."""
    return Text(icon_label(f"type/{kind.casefold()}", kind, icon_style),
                style=TYPE_STYLES.get(kind.casefold(), "bold"))


# Nerd Font half circles that round off a tag block.
ROUND_LEFT, ROUND_RIGHT = "", ""


def tag_chip(tag: str, style: str, dark: bool = True) -> Text:
    """A tag as a colored block: with round ends when `style` is "round", or as text in its color
    on a faint tint of it when `style` is "outline"."""
    color = chip_color(tag)
    if style == "outline":
        return Text(f" {tag} ", style=f"{color} on {chip_tint(color, dark)}")
    if style == "round":
        chip = Text(ROUND_LEFT, style=color)
        chip.append(tag, style=f"black on {color}")
        chip.append(ROUND_RIGHT, style=color)
        return chip
    return Text(f" {tag} ", style=f"black on {color}")


# The icon before a card's title: not started, started, done. The icon takes the column color.
CARD_ICONS = {"new": "○", "started": "●", "done": "✓"}
# Terminal palette names, so ANSI themes use the terminal's own green and yellow.
CARD_ICON_STYLES = {"new": "", "started": "yellow", "done": "green"}
# The state of an agent pane, with the glyphs and colors of Herdr's `symbols` status indicator set.
# Herdr shows done in teal; the terminal palette has no teal, so cyan takes its place.
AGENT_STATUS_ICONS = {"working": "◐", "blocked": "×", "done": "✓", "idle": "○", "unknown": "·"}
AGENT_STATUS_STYLES = {"working": "yellow", "blocked": "red", "done": "cyan", "idle": "green", "unknown": "dim"}
# The icon before a column's name: the completed column, the start column, any other column.
COLUMN_ICONS = {"done": "✓", "started": "●", "other": "○"}
# How many colors the columns take in turn. The completed column is always green.
COLUMN_COLORS = 5
# Rows of a card's description line, at most.
DESCRIPTION_ROWS = 2


def column_class(index: int, column: ColumnData) -> str:
    """The style class that gives a column its color, from its place on the board."""
    if column.completed:
        return "col-done"
    if column.archive:
        return "col-archive"
    return f"col-{index % COLUMN_COLORS}"


def card_state(card: CardView, done: bool) -> str:
    """`done` in the completed column, `started` while a worktree or an agent works on the card."""
    if done:
        return "done"
    if card.worktree or card.agent_review or card.pr_writing or card.planning:
        return "started"
    return "new"


def agent_glyph(status: str) -> Text:
    """The Herdr glyph of an agent state, in its color. An unknown state shows as `unknown`."""
    key = status if status in AGENT_STATUS_ICONS else "unknown"
    return Text(AGENT_STATUS_ICONS[key], style=AGENT_STATUS_STYLES[key])


def phase_icon(key: str, icon_style: str) -> str:
    """The icon of a planning or review phase: a Nerd Font glyph, or a plain one."""
    return ICONS[f"phase/{key}"] if icon_style == "nerd" else PHASE_ICONS[key]


def phase_item(key: str, icon_style: str, style: str, agent: str | None = None, status: str | None = None,
               agent_logos: bool = False) -> Text:
    """A status row item: the phase icon, then the agent's Herdr glyph and its logo, else its name."""
    item = Text(phase_icon(key, icon_style), style=style)
    if status:
        item.append(" ")
        item.append_text(agent_glyph(status))
    if agent:
        logo = AGENT_LOGOS.get(agent) if agent_logos else None
        item.append(f" {logo or agent}")
    return item


def card_description(card: CardView) -> str:
    """The first detail line that is not a checklist item, without its bullet."""
    for line in card.body:
        line = _hold_indent(line).lstrip(" ")
        if not line.strip() or CHECK_ITEM.match(line):
            continue
        bullet = BULLET_ITEM.match(line)
        return (bullet.group("text") if bullet else line).strip()
    return ""


class ClampedText:
    """Text wrapped to the given width and cut to `rows` rows, with an ellipsis when cut."""

    def __init__(self, text: Text, rows: int):
        self.text = text
        self.rows = rows

    def __rich_console__(self, console, options):
        lines = self.text.wrap(console, options.max_width)
        if len(lines) > self.rows:
            lines = lines[:self.rows]
            last = lines[-1]
            last.rstrip()
            last.truncate(max(options.max_width - 1, 0))
            last.append("…")
        text = Text("\n").join(lines)
        text.no_wrap = True
        yield text


class TitleLine:
    """The title with `corner` at the right of its first row. The rows after the first use the full width,
    and the corner always has a space before it."""

    def __init__(self, title: Text, corner: Text):
        self.title = title
        self.corner = corner

    def __rich_console__(self, console, options):
        width = options.max_width
        room = width - self.corner.cell_len - 1
        if room < 1:
            yield self.title
            yield self.corner
            return
        first = self.title.wrap(console, room)[0]
        rest = self.title[len(first.plain):]
        rest = rest[len(rest.plain) - len(rest.plain.lstrip()):]
        first.rstrip()
        first.append(" " * (width - first.cell_len - self.corner.cell_len))
        first.append_text(self.corner)
        lines = [first, *rest.wrap(console, width)] if rest.plain else [first]
        text = Text("\n").join(lines)
        text.no_wrap = True
        yield text


class ChipRow:
    """Chips in rows that break only between two chips, so no chip splits over two rows."""

    def __init__(self, chips: list[Text]):
        self.chips = chips

    def __rich_console__(self, console, options):
        lines: list[Text] = []
        for chip in self.chips:
            if lines and lines[-1].cell_len + 1 + chip.cell_len <= options.max_width:
                lines[-1].append(" ")
                lines[-1].append_text(chip)
            else:
                # A new row, not a copy of the chip: a copy's style would carry on to the next chips.
                lines.append(Text())
                lines[-1].append_text(chip)
        text = Text("\n").join(lines)
        text.no_wrap = True
        text.overflow = "ellipsis"
        yield text


@dataclass
class CardFace:
    """A card's rows as the board shows them. Empty rows stay off the card. A full card has the state icon
    and the corner in its first row and the title under them; a compact card has them all in one row."""

    icon: Text
    title: Text
    corner: Text
    description: Text
    chips: list[Text]
    count: Text
    due: Text
    status: Text
    compact: bool = False
    # The status row in words, without glyphs, for the card's plain text.
    words: Text = field(default_factory=Text)

    def heading(self) -> Text:
        """The title row of a compact card."""
        heading = self.icon.copy()
        heading.append(" ")
        heading.append_text(self.title)
        return heading

    def lines(self) -> list[Text]:
        first = self.heading() if self.compact else self.icon.copy()
        if self.corner:
            first.append("  ")
            first.append_text(self.corner)
        title = Text() if self.compact else self.title
        footer = self.count.copy()
        if self.count and self.due:
            footer.append("  ")
        footer.append_text(self.due)
        chips = Text(" ").join(self.chips)
        return [line for line in (first, title, self.description, chips, footer, self.status) if line]

    def text(self) -> Text:
        return Text("\n").join(self.lines())


def card_face(card: CardView, show_board: bool, tag_style: str = "outline", icon_style: str = "plain",
              card_style: str = "full", done: bool = False, dark: bool = True,
              agent_logos: bool = False) -> CardFace:
    """A card's rows: the state icon and ID, the title, the description, the type and tags,
    the sub-card count and due date, then herdboard's status of the card."""
    if card.source == "linear":
        title, tags, due = card.title, card.tags, card.due
    else:
        title, tags, due = split_title(card.title)
    muted = "dim strike" if done else ""
    state = card_state(card, done)
    # A live work agent shows its Herdr state; the completed column always shows done.
    if card.agent_status and state != "done":
        icon = agent_glyph(card.agent_status)
    else:
        icon = Text(CARD_ICONS[state], style=CARD_ICON_STYLES[state])
    logo = AGENT_LOGOS.get(card.agent) if agent_logos and card.agent else None
    if logo:
        icon.append(f" {logo}")
    heading = Text(title or card.title, style=muted)
    corner = Text(style="dim")
    if card.has_attachments:
        # Nerd Font cards show the paperclip alone.
        corner.append(ICONS["attachment"] if icon_style == "nerd" else ATTACHMENT_MARK)
    # A card without a block ID has a reference that is only its place on the board.
    if card.source == "linear" or not (TEMP_REF.match(card.ref) or ARCHIVE_REF.match(card.ref)):
        if corner:
            corner.append(" ")
        corner.append(f"{LINEAR_MARK} {card.ref}" if card.source == "linear" else card.ref)
    description = Text()
    if card_style == "full":
        description = Text(card_description(card), style="dim strike" if done else "dim")
    chips = [type_badge(card.type, icon_style)] if card.type else []
    chips += [tag_chip(tag, tag_style, dark) for tag in plain_tags(tags)]
    count = Text()
    if card.sub_cards:
        count = Text(icon_label("sub_cards", f"{card.sub_cards[0]}/{card.sub_cards[1]} done", icon_style),
                     style="dim")
    # Each status item, and its words for the plain text when the item is only glyphs.
    info: list[tuple[Text, str]] = []

    def add(item: Text, words: str | None = None) -> None:
        info.append((item, item.plain if words is None else words))

    if card.source == "linear" and card.pr:
        add(Text(icon_label("pr", "PR", icon_style), style="bold"))
    if show_board:
        add(Text(card.board, style="dim"))
    if card.worktree_removed:
        add(Text(icon_label("branch", "worktree removed", icon_style), style="bold yellow"))
    if card.pr_state:
        add(Text(icon_label("pr", f"PR {card.pr_state}", icon_style), style=PR_STYLES.get(card.pr_state, "dim")))
    if card.worktree_left:
        add(Text(icon_label("branch", "worktree still exists", icon_style), style="bold yellow"))
    if card.agent_review:
        add(phase_item("reviewing", icon_style, "bold", card.agent_review, card.review_status, agent_logos),
            f"agent review: {agent_label(card.agent_review, agent_logos)}")
    elif card.reviewed == "reviewed":
        add(phase_item("reviewed", icon_style, PR_STATES["agent:reviewed"][2]), "reviewed")
    elif card.reviewed == "new commits":
        add(phase_item("new commits", icon_style, PR_STATES["agent:new commits"][2]), "new commits since review")
    if card.pr_writing:
        writing = Text(icon_label("pr", "PR: writing", icon_style), style="bold")
        words = writing.plain
        if card.pr_write_status:
            writing.append(" ")
            writing.append_text(agent_glyph(card.pr_write_status))
        add(writing, words)
    if card.planning:
        add(phase_item("planning", icon_style, "bold cyan", card.planning, card.planning_status, agent_logos),
            f"planning: {agent_label(card.planning, agent_logos)}")
    elif card.planned and not done:
        add(phase_item("planned", icon_style, "bold cyan"), "planned")
    if card.parent_title:
        add(Text(f"↳ {card.parent_title}", style="dim"))
    separator = Text(" · ", style="dim")
    return CardFace(icon, heading, corner, description, chips, count,
                    due_label(due, icon_style=icon_style) if due else Text(),
                    separator.join(item for item, _words in info), card_style == "compact",
                    separator.join(Text(words) for _item, words in info))


def card_text(card: CardView, show_board: bool, tag_style: str = "outline", icon_style: str = "plain",
              card_style: str = "full", done: bool = False) -> Text:
    """A card's rows as one text, as the board shows them."""
    return card_face(card, show_board, tag_style, icon_style, card_style, done).text()


def _two_sided(left, right: Text) -> Table:
    """One row with `left` taking the room and `right` at the right edge."""
    grid = Table.grid(expand=True)
    grid.add_column(ratio=1)
    grid.add_column(justify="right", no_wrap=True)
    grid.add_row(left, right)
    return grid


class CardWidget(Static):
    # A drag moves the card, so it must not also select text.
    ALLOW_SELECT = False

    def __init__(self, card: CardView, show_board: bool, tag_style: str = "outline", icon_style: str = "plain",
                 card_style: str = "full", done: bool = False, dark: bool = True, agent_logos: bool = False):
        self.card = card
        self.agent_logos = agent_logos
        face = card_face(card, show_board, tag_style, icon_style, card_style, done, dark, agent_logos)
        self.rich_text = face.text()
        # The state icon and the agent logo are not card text: `plain` has the title, then the corner.
        first = face.title.plain + (f"  {face.corner.plain}" if face.corner else "")
        rest = [line.plain for line in replace(face, status=face.words).lines()[1 if face.compact else 2:]]
        self.plain = "\n".join([first, *rest])
        # What a redraw compares: the text with its styles and the classes.
        classes = "card" + (" compact" if card_style == "compact" else "") + (" done" if done else "")
        self.key = (self.rich_text.plain, tuple(self.rich_text.spans), classes)
        if face.compact:
            rows = [TitleLine(face.heading(), face.corner) if face.corner else face.heading()]
        else:
            rows = [_two_sided(face.icon, face.corner), face.title]
        if face.description:
            rows.append(ClampedText(face.description, DESCRIPTION_ROWS))
        if face.chips:
            rows.append(ChipRow(face.chips))
        if face.count or face.due:
            rows.append(_two_sided(face.count, face.due))
        if face.status:
            rows.append(face.status)
        super().__init__(Group(*rows), classes=classes)

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1:
            self.app.start_drag(self.card)

    def on_click(self, event: events.Click) -> None:
        event.stop()
        app = self.app
        if app.dropped:
            # The click that ends a drag: the drop has already selected the card.
            app.dropped = False
            return
        if event.button == 3:
            app.open_card_menu(self.card)
        elif event.button == 1 and event.chain == 1:
            app.select_card(self.card)
        elif event.button == 1 and event.chain == 2 and app.select_card(self.card):
            # From the app's own message pump: a reload can replace this widget while the detail is open,
            # and the detail's result goes to the pump that opened it.
            app.call_next(app.action_detail)


class ColumnHeader(Static):
    """The column's icon and name at the left, its card count at the right, in the column color."""

    def __init__(self, column: ColumnData, start_column: str | None = None):
        self.plain = column.header
        super().__init__(self._content(column, start_column), classes="column-header")

    @staticmethod
    def _content(column: ColumnData, start_column: str | None) -> Table:
        if column.completed:
            icon = COLUMN_ICONS["done"]
        elif start_column and column.name.casefold() == start_column.casefold():
            icon = COLUMN_ICONS["started"]
        else:
            icon = COLUMN_ICONS["other"]
        return _two_sided(Text(f"{icon} {column.name}"), Text(column.count_label))

    def show(self, column: ColumnData, start_column: str | None) -> None:
        if self.plain != column.header:
            self.plain = column.header
            self.update(self._content(column, start_column))


class ModalBase(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss_screen", "Close")]

    def on_mount(self) -> None:
        # The dialog's keys at the bottom, over the key bar of the screen below.
        self.mount(KeyBar())

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)


CHECK_ITEM = re.compile(r"^(?P<indent>\u00a0*)[-*+] \[(?P<mark>.)\] (?P<text>.*)$")
BULLET_ITEM = re.compile(r"^(?P<indent>\u00a0*)[-*+] (?P<text>.*)$")


def _hold_indent(line: str) -> str:
    """Leading spaces and tabs as non-breaking spaces: Textual drops plain leading spaces."""
    stripped = line.lstrip(" \t")
    indent = line[: len(line) - len(stripped)].replace("\t", "    ")
    return "\u00a0" * len(indent) + stripped


def detail_lines(lines: list[str], selected: int | None = None, checks: bool = False) -> Text:
    """Detail lines with checkboxes and bullets drawn as symbols. Done items are struck through.

    `selected` is the index of the checklist line to highlight. With `checks`, a click on a
    checklist line checks or clears it.
    """
    text = Text()
    for number, line in enumerate(lines):
        if number:
            text.append("\n")
        line = _hold_indent(line)
        check = CHECK_ITEM.match(line)
        bullet = BULLET_ITEM.match(line)
        if check:
            done = check.group("mark") != " "
            text.append(check.group("indent"))
            start = len(text)
            text.append("☑ " if done else "☐ ", style="green" if done else "")
            text.append(check.group("text"), style="dim strike" if done else "")
            if number == selected:
                text.stylize("reverse", start, len(text))
            if checks:
                text.stylize(Style(meta={"@click": f"screen.toggle_line({number})"}), start, len(text))
        elif bullet:
            text.append(f"{bullet.group('indent')}• {bullet.group('text')}")
        else:
            text.append(line)
    return text


def log_lines(entries: list[dict]) -> Text:
    """Activity log entries, oldest first: the time and author dim, extra lines indented."""
    text = Text()
    for entry in entries:
        first, *rest = entry["message"].split("\n")
        if text:
            text.append("\n")
        text.append(f"{entry['time']} · {entry['author']} · ", style="dim")
        text.append(first)
        for line in rest:
            text.append("\n" + _hold_indent("\t" + line))
    return text


def mark_family(cards: list[CardView]) -> None:
    """Set each sub-card's parent title and each parent's count of done sub-cards.

    A Linear parent can be outside the view, in another project or assigned to someone
    else, so a Linear sub-issue without its parent shows the parent's identifier."""
    by_ref = {card.ref: card for card in cards}
    counts: dict[str, list[int]] = {}
    for card in cards:
        parent = by_ref.get(card.parent) if card.parent else None
        if parent:
            card.parent_title = split_title(parent.title)[0] or parent.title
        else:
            card.parent_title = card.parent if card.source == "linear" else None
        if card.parent:
            count = counts.setdefault(card.parent, [0, 0])
            count[0] += card.checked
            count[1] += 1
    for card in cards:
        count = counts.get(card.ref)
        card.sub_cards = (count[0], count[1]) if count else None


def card_matches(card: CardView, terms: list[str]) -> bool:
    """True when every term matches: `#tag` a tag by prefix, `^id` a card and its sub-cards,
    any other word the card's text."""
    tags = [tag.casefold() for tag in card.tags]
    if card.type:
        tags.append(f"type/{card.type}".casefold())
    text = " ".join([card.title, card.board or "", card.type or "", *card.tags, *card.body]).casefold()
    for term in terms:
        if term.startswith("^") and len(term) > 1:
            if term[1:] not in (card.ref.casefold(), (card.parent or "").casefold()):
                return False
        elif term.startswith("#") and len(term) > 1:
            if not any(tag.startswith(term[1:]) for tag in tags):
                return False
        elif term not in text:
            return False
    return True


class RichStatic(Static):
    """A Static that keeps its Rich text, so tests and callers can read it."""

    def __init__(self, text: Text, **kwargs):
        self.rich_text = text
        self.plain = text.plain
        super().__init__(text, **kwargs)


def attachment_lines(files: list[dict]) -> Text:
    """One line per attachment: its name, then its size in a dim column."""
    width = max((len(entry["name"]) for entry in files), default=0)
    text = Text()
    for index, entry in enumerate(files):
        if text:
            text.append("\n")
        start = len(text)
        text.append(entry["name"].ljust(width + 2))
        text.append(attachments.size_label(entry["size"]), style="dim")
        text.stylize(Style(meta={"@click": f"screen.open_file({index})"}), start, len(text))
    return text


class DetailScreen(ModalBase):
    """A card in full: title, tags and date, where it lives, its details, linked notes and attachments."""

    BINDINGS = [
        Binding("e", "edit", "Edit"),
        Binding("o", "open_attachment", "Open attachment"),
        Binding("space", "toggle", "Check"),
        Binding("down,j", "item(1)", "Next item", show=False),
        Binding("up,k", "item(-1)", "Previous item", show=False),
    ]

    def __init__(self, card: CardView, notes: list[dict] | None, tag_style: str = "round",
                 log: list[dict] | None = None, files: list[dict] | None = None, icon_style: str = "plain"):
        super().__init__()
        self.notes = notes or []
        self.entries = log or []
        self.files = files or []
        self.tag_style = tag_style
        self.icon_style = icon_style
        self.cursor = 0
        self._set_card(card)

    def _set_card(self, card: CardView) -> None:
        """Keep the card and the indexes of its checklist lines. Linear descriptions stay read only."""
        self.card = card
        self.body = card.body if card.source == "linear" else [_unindent(line) for line in card.body]
        self.items = [] if card.source == "linear" else [
            index for index, line in enumerate(self.body) if CHECK_ITEM.match(_hold_indent(line))
        ]
        self.cursor = min(self.cursor, max(len(self.items) - 1, 0))

    def _selected(self) -> int | None:
        return self.items[self.cursor] if self.items else None

    def key_applies(self, action: str) -> bool:
        """False for a key that has nothing to act on: the key bar dims it."""
        if action == "toggle":
            return bool(self.items)
        if action == "open_attachment":
            return bool(self.files)
        if action == "edit":
            return self.card.source != "linear"
        return True

    def _show_body(self) -> None:
        block = self.query_one("#detail-body", RichStatic)
        block.rich_text = detail_lines(self.body, self._selected(), self.card.source != "linear")
        block.plain = block.rich_text.plain
        block.update(block.rich_text)

    def action_item(self, step: int) -> None:
        if self.items:
            self.cursor = max(0, min(self.cursor + step, len(self.items) - 1))
            self._show_body()
        else:
            self.query_one(".detail", VerticalScroll).scroll_relative(y=step, animate=False)

    async def action_toggle(self) -> None:
        index = self._selected()
        if index is None:
            return
        app = self.app
        if app.writing or app.starting:
            self.notify("Busy: a change is still being written", severity="warning")
            return
        try:
            card = await asyncio.to_thread(app.store.toggle_item, self.card.ref, index, self.card)
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        self._set_card(card)
        self._show_body()
        await app.reload(card.ref)

    async def action_toggle_line(self, number: int) -> None:
        """A click on a checklist line: select it, then check or clear it."""
        if number in self.items:
            self.cursor = self.items.index(number)
            await self.action_toggle()

    def action_open_file(self, index: int) -> None:
        """A click on an attachment line opens that attachment."""
        if 0 <= index < len(self.files):
            self.open_attachment(self.files[index])

    def _title_parts(self) -> tuple[str, list[str], str | None]:
        card = self.card
        if card.source == "linear":
            return card.title, card.tags, card.due
        title, tags, due = split_title(card.title)
        return title, plain_tags(tags), due

    def _phase(self, key: str, agent: str, status: str | None) -> Text:
        """A phase icon, the agent's Herdr glyph, and the agent and its state in words."""
        text = Text(f"{phase_icon(key, self.icon_style)} ")
        if status:
            text.append_text(agent_glyph(status))
            text.append(f" {agent}: {status}")
        else:
            text.append(agent)
        return text

    def _meta(self) -> Text:
        card = self.card
        rows = [("Board", card.board), ("Column", card.column), ("ID", card.ref), ("Type", card.type)]
        if card.source == "linear":
            rows += [("State", card.state), ("Team", card.team), ("Link", card.url)]
        if card.pr or card.pr_state or card.pr_writing:
            state = "writing" if card.pr_writing else card.pr_state or "yes"
            rows.append(("Pull request", icon_label("pr", state, self.icon_style)))
        if card.worktree_removed:
            rows.append(("Worktree", icon_label("branch", "removed", self.icon_style)))
        elif card.worktree_left:
            rows.append(("Worktree", icon_label("branch", "still exists", self.icon_style)))
        style = self.icon_style
        if card.agent_status:
            agent = agent_glyph(card.agent_status)
            # An agent that the user started by hand in the card's pane has no recorded kind.
            agent.append(f" {card.agent or 'agent'}: {card.agent_status}")
            rows.append(("Agent", agent))
        if card.planning:
            rows.append(("Planner", self._phase("planning", card.planning, card.planning_status)))
        if card.agent_review:
            rows.append(("Agent review", self._phase("reviewing", card.agent_review, card.review_status)))
        elif card.reviewed == "reviewed":
            rows.append(("Agent review", f"{phase_icon('reviewed', style)} reviewed"))
        elif card.reviewed == "new commits":
            rows.append(("Agent review", f"{phase_icon('new commits', style)} new commits since review"))
        if card.planned:
            rows.append(("Plan", f"{phase_icon('planned', style)} planned"))
        text = Text()
        for label, value in rows:
            if value:
                if text:
                    text.append("\n")
                text.append(f"{label:<13}", style="dim")
                if label == "Type" and self.icon_style == "nerd":
                    text.append_text(type_badge(value, self.icon_style))
                elif isinstance(value, Text):
                    text.append_text(value)
                else:
                    text.append(str(value))
        return text

    def compose(self) -> ComposeResult:
        title, tags, due = self._title_parts()
        chips = Text()
        for tag in tags:
            if chips:
                chips.append(" ")
            chips.append_text(tag_chip(tag, self.tag_style, self.app.current_theme.dark))
        if due:
            if chips:
                chips.append("  ")
            chips.append_text(due_label(due, icon_style=self.icon_style))
        body = self.body
        with VerticalScroll(classes="dialog detail", can_focus=False):
            yield RichStatic(Text(title or self.card.title, style="bold"), id="detail-title")
            if chips:
                yield RichStatic(chips, id="detail-chips")
            yield RichStatic(self._meta(), id="detail-meta")
            if any(line.strip() for line in body):
                block = RichStatic(detail_lines(body, self._selected(), self.card.source != "linear"),
                                   id="detail-body", classes="detail-block")
                # A text drag that ends on a checklist line would also check it.
                block.ALLOW_SELECT = not self.items
                block.border_title = "Details"
                yield block
            for note in self.notes:
                content = note["content"]
                with Vertical(classes="detail-block detail-note") as box:
                    box.border_title = note["name"]
                    if content is None:
                        yield Label("Note not found", classes="dim")
                    else:
                        yield Markdown(content)
            if self.files:
                block = RichStatic(attachment_lines(self.files), id="detail-files", classes="detail-block")
                block.border_title = icon_label("attachment", "Attachments", self.icon_style)
                # A text drag that ends on an attachment line would also open it.
                block.ALLOW_SELECT = False
                yield block
            if self.entries:
                block = RichStatic(log_lines(self.entries), id="detail-log", classes="detail-block")
                block.border_title = "Log"
                yield block
            hint = "j/k: select item · space: check · " if self.items else ""
            if self.files:
                hint += "o: open attachment · "
            yield Label(f"{hint}e: edit · escape: close", classes="dim")

    def action_edit(self) -> None:
        self.dismiss("edit")

    def action_open_attachment(self) -> None:
        if not self.files:
            self.notify("The card has no attachments", severity="warning")
        elif len(self.files) == 1:
            self.open_attachment(self.files[0])
        else:
            self.app.push_screen(AttachmentChoiceScreen(self.files),
                                 lambda entry: self.open_attachment(entry) if entry else None)

    def open_attachment(self, entry: dict) -> None:
        """Show a text attachment in the board. Another file opens in the system's default application."""
        path = Path(entry["path"])
        try:
            text = attachments.view_text(path)
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        if text is not None:
            self.app.push_screen(AttachmentScreen(entry["name"], text))
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            process = subprocess.Popen([opener, str(path)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, start_new_session=True)
        except OSError as error:
            self.notify(f"Cannot open {entry['name']} with {opener}: {error}", severity="error")
            return
        self.notify(f"Opening {entry['name']} with {opener}")
        # A daemon thread, because xdg-open can wait until the viewer closes, and quitting the board must not wait.
        threading.Thread(target=watch_opener, args=(self.app, entry["name"], opener, process), daemon=True).start()


def watch_opener(app: App, name: str, opener: str, process: subprocess.Popen) -> None:
    """Wait for the opener, and show an error notice when it fails, such as for a file type without an application."""
    _, error = process.communicate()
    if process.returncode == 0:
        return
    message = f"Cannot open {name} with {opener}: exit code {process.returncode}"
    detail = error.decode("utf-8", errors="replace").strip()
    if detail:
        message += f": {detail}"
    try:
        app.call_from_thread(app.notify, message, severity="error")
    except RuntimeError:
        # The board has quit.
        pass


class AttachmentChoiceScreen(ModalBase):
    """Choose which of the card's attachments to open."""

    def __init__(self, files: list[dict]):
        super().__init__()
        self.files = files

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Open an attachment")
            yield OptionList(*(entry["name"] for entry in self.files))
            yield Label("enter: open, escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.files[event.option_index])


class AttachmentScreen(ModalBase):
    """A text attachment, read only. A Markdown file is drawn as Markdown."""

    BINDINGS = [
        Binding("down,j", "scroll(1)", "Down", show=False),
        Binding("up,k", "scroll(-1)", "Up", show=False),
    ]

    def __init__(self, name: str, text: str):
        super().__init__()
        self.name_text = name
        self.text = text

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog detail attachment", can_focus=False):
            yield RichStatic(Text(self.name_text, style="bold"), id="attachment-title")
            if self.name_text.lower().endswith(".md"):
                with Vertical(classes="detail-block detail-note"):
                    yield Markdown(self.text)
            else:
                yield RichStatic(Text(self.text), id="attachment-text", classes="detail-block")
            yield Label("j/k: scroll · escape: close", classes="dim")

    def action_scroll(self, step: int) -> None:
        self.query_one(".attachment", VerticalScroll).scroll_relative(y=step, animate=False)


class HelpScreen(ModalBase):
    """The key list in sections. Each description wraps inside its own column, so a long entry stays aligned."""

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog help"):
            with Container(id="help", classes="help-grid"):
                for title, entries in HELP_SECTIONS:
                    section = Static(Text(title, style="bold"), classes="help-section")
                    section.plain = title
                    yield section
                    for key, what in entries:
                        key_label = Static(Text(key), classes="help-key")
                        key_label.plain = key
                        yield key_label
                        what_label = Static(Text(what), classes="help-what")
                        what_label.plain = what
                        yield what_label
            yield Label("escape: close", classes="dim")


class CardFormScreen(ModalBase):
    """The fields of a todo: title, type, tags, due date and details. ctrl+s or enter saves."""

    BINDINGS = [Binding("ctrl+s", "save", "Save")]
    AUTO_FOCUS = "#card-title"
    heading = ""

    def __init__(self, title: str = "", tags: list[str] = (), due: str | None = None, body: list[str] = (),
                 kind: str | None = None, types: list[str] = ()):
        super().__init__()
        self.initial = (title, " ".join(tags), due or "", "\n".join(body))
        self.kind = kind
        # A type that the config no longer lists stays a choice, so an edit keeps it.
        self.types = list(types) + ([kind] if kind and kind not in types else [])

    def destination(self) -> ComposeResult:
        return iter(())

    def compose(self) -> ComposeResult:
        title, tags, due, body = self.initial
        target = Static(Text(self.heading), id="card-target")
        target.plain = self.heading
        error = Static("", id="card-error", classes="error")
        error.plain = ""
        with Vertical(classes="dialog card-form"):
            yield target
            yield from self.destination()
            yield self._titled(Input(value=title, placeholder="What needs doing", id="card-title",
                                     select_on_focus=False), "Title")
            with Horizontal(classes="form-row"):
                yield self._titled(Select([(kind, kind) for kind in self.types], prompt="none",
                                          value=self.kind or Select.NULL, id="card-type"), "Type")
                yield self._titled(Input(value=tags, placeholder="auth, web", id="card-tags",
                                         select_on_focus=False), "Tags")
                yield self._titled(Input(value=due, placeholder="YYYY-MM-DD", id="card-due",
                                         select_on_focus=False), "Due date")
            yield self._titled(
                TextArea(body, id="card-body", placeholder="Notes, links or a checklist such as - [ ] step one"),
                "Details",
            )
            yield error
            yield Label("ctrl+s or enter: save · tab: next field · escape: cancel", classes="dim")

    @staticmethod
    def _titled(widget, title: str):
        widget.border_title = title
        return widget

    def set_heading(self, text: str) -> None:
        target = self.query_one("#card-target", Static)
        target.plain = text
        target.update(Text(text))

    def show_error(self, message: str) -> None:
        error = self.query_one("#card-error", Static)
        error.plain = message
        error.update(Text(message))

    def values(self) -> tuple[str, list[str]]:
        """The first-line text and the detail lines. Raises HerdboardError for bad input."""
        kind = self.query_one("#card-type", Select)
        tags = plain_tags(parse_tags(self.query_one("#card-tags", Input).value))
        line = join_title(
            self.query_one("#card-title", Input).value,
            ([] if kind.is_blank() else [f"type/{kind.value}"]) + tags,
            self.query_one("#card-due", Input).value.strip() or None,
        )
        return line, self.query_one("#card-body", TextArea).text.split("\n")

    def is_empty(self) -> bool:
        fields = [self.query_one(f"#card-{name}", Input).value for name in ("title", "tags", "due")]
        return not any(f.strip() for f in fields) and not self.query_one("#card-body", TextArea).text.strip()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if self.is_empty():
            self.dismiss(None)
            return
        self.action_save()

    def action_save(self) -> None:
        try:
            line, body = self.values()
        except HerdboardError as error:
            self.show_error(str(error))
            return
        self.submit(line, body)

    def submit(self, line: str, body: list[str]) -> None:
        raise NotImplementedError


class AddScreen(CardFormScreen):
    """Add dialog. The column is fixed when the dialog opens; the board is a choice."""

    def __init__(self, options: list[tuple[str, Path]], path: Path, column: str, types: list[str] = ()):
        super().__init__(types=types)
        self.board_options = [(label, str(option)) for label, option in options]
        self.path = path
        self.column = column
        self.heading = self._heading()

    def _heading(self) -> str:
        return f"Add to {self.path.stem} / {self.column}"

    def destination(self) -> ComposeResult:
        if len(self.board_options) > 1:
            yield Select(self.board_options, value=str(self.path), allow_blank=False, id="card-board")

    def on_select_changed(self, event: Select.Changed) -> None:
        event.stop()
        if event.select.id != "card-board":
            return
        self.path = Path(str(event.value))
        self.set_heading(self._heading())

    def submit(self, line: str, body: list[str]) -> None:
        self.app.submit_add(self, line, body)


def _unindent(line: str) -> str:
    """A body line as the edit dialog shows it: one indent level less than in the file."""
    if line.startswith("\t"):
        return line[1:]
    if line.startswith("    "):
        return line[4:]
    return line.lstrip(" ")


class EditScreen(CardFormScreen):
    """Edit a todo's fields. The checkbox and the ID stay."""

    def __init__(self, card: CardView, types: list[str] = ()):
        title, tags, due = split_title(card.title)
        super().__init__(title, plain_tags(tags), due, [_unindent(line) for line in card.body],
                         kind=card.type, types=types)
        self.card = card
        self.heading = f"Edit {card.ref} ({card.board} / {card.column})"

    def submit(self, line: str, body: list[str]) -> None:
        self.app.submit_edit(self, line, body)


class StateScreen(ModalBase):
    """Choose one of several workflow states of a team that match a column."""

    def __init__(self, team_key: str, column: str, found: list[dict]):
        super().__init__()
        self.team_key = team_key
        self.column = column
        self.found = found

    def compose(self) -> ComposeResult:
        text = f"Which state of team {self.team_key} for column {self.column!r}?"
        prompt = Static(Text(text), id="state-prompt")
        prompt.plain = text
        with Vertical(classes="dialog"):
            yield prompt
            yield OptionList(*[state["name"] for state in self.found], id="state-options")
            yield Label("enter: choose, escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.found[event.option_index])


class AgentScreen(ModalBase):
    """Select the agent before starting a card, or the agent that reviews one."""

    def __init__(self, title: str = "Choose an agent"):
        super().__init__()
        self.title_text = title

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text)
            yield OptionList("claude", "pi")
            yield Label("enter: choose, escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(("claude", "pi")[event.option_index])


class ReviewKindScreen(ModalBase):
    """Choose how to review a started card."""

    KINDS = ("human", "agent", "both")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Review")
            yield OptionList("Human: open Hunk next to the card's agent",
                             "Agent: another agent reviews, the card's agent answers",
                             "Both: an agent review first, then Hunk")
            yield Label("enter: choose, escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.KINDS[event.option_index])


class ClaudeModelScreen(ModalBase):
    """Choose the model of a Claude reviewer. It returns `(model,)`, where None is Claude's default."""

    MODELS = (None, "opus", "sonnet", "haiku")

    def __init__(self):
        super().__init__()
        self.labels = ["claude default", *self.MODELS[1:]]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Choose Claude's model")
            yield OptionList(*self.labels)
            yield Label("enter: start, escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss((self.MODELS[event.option_index],))


class TargetScreen(ModalBase):
    """Choose where a card without a repository runs.

    The choices are the repository boards, a new repository at a path, or no repository.
    It returns `(mode, value)`, where mode is `"repo"`, `"new_repo"` or `"repoless"`.
    """

    def __init__(self, ref: str, options: list[tuple[str, Path]], default_path: Path | None = None,
                 linear: bool = False, title: str | None = None):
        super().__init__()
        self.ref = ref
        self.options = options
        self.default_path = default_path
        self.linear = linear
        self.title_text = title or f"Where does {ref} run?"
        self.choices: list[tuple[str, object]] = [("repo", path) for _label, path in options]
        self.labels = [label for label, _ in options]
        if not linear:
            self.choices.append(("new_repo", None))
            self.labels.append("New repository")
        self.choices.append(("repoless", None))
        self.labels.append("No repository: a plain folder, output as attachments")

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.title_text)
            yield OptionList(*self.labels, id="target-choice")
            if not self.linear and self.default_path is not None:
                yield Label("New repository path")
                yield Input(value=str(self.default_path), placeholder="path of the new repository",
                            id="target-path")
            yield Label("enter: choose a repository"
                        + (" · type a path and press enter for a new repository" if not self.linear else "")
                        + " · escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        kind, value = self.choices[event.option_index]
        if kind == "new_repo":
            self.query_one("#target-path", Input).focus()
            return
        self.dismiss((kind, value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        typed = event.value.strip()
        path = Path(typed).expanduser() if typed else self.default_path
        self.dismiss(("new_repo", path))


class BoardChoiceScreen(ModalBase):
    """Choose the board that a todo moves to, or another board for `prompt`."""

    def __init__(self, card: CardView | None, options: list[tuple[str, Path]], prompt: str | None = None):
        super().__init__()
        self.card = card
        self.options = options
        self.board_labels = [label for label, _ in options]
        self.prompt = prompt

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(self.prompt or f"Move {split_title(self.card.title)[0]} to")
            yield OptionList(*self.board_labels, id="board-choice")
            yield Label("enter: choose, escape: cancel" if self.prompt else "enter: move, escape: cancel",
                        classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.options[event.option_index][1])


class IssueSearchScreen(ModalBase):
    """Search Linear and choose an issue. The search runs on enter only, in a thread.

    Linear allows few searches per minute, so typing never searches. The screen returns the
    chosen search result, or None.
    """

    def __init__(self, search: Callable[[str], list[dict]], board_name: str):
        super().__init__()
        self.search = search
        self.board_name = board_name
        self.results: list[dict] = []
        self.searching = False

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Pin a Linear issue to {self.board_name}")
            yield Input(placeholder="words, or an identifier such as ENG-123", id="issue-search")
            yield RichStatic(Text("Open issues of your teams. An identifier finds an issue of any team."),
                             id="issue-status", classes="dim")
            yield OptionList(id="issue-results")
            yield Label("enter: search, or pin the issue · tab: results · escape: cancel", classes="dim")

    @staticmethod
    def _label(issue: dict) -> Text:
        text = Text(f"{issue['identifier']}  ", style="bold")
        text.append(issue["title"])
        details = [issue["state"]["name"], issue["team"]["key"]]
        if issue.get("project"):
            details.append(issue["project"]["name"])
        text.append("  " + " · ".join(details), style="dim")
        return text

    def _status(self, message: str, error: bool = False) -> None:
        status = self.query_one("#issue-status", RichStatic)
        status.rich_text = Text(message)
        status.plain = message
        status.set_class(error, "error")
        status.set_class(not error, "dim")
        status.update(status.rich_text)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        term = event.value.strip()
        if not term or self.searching:
            return
        self.searching = True
        self.results = []
        self.query_one("#issue-results", OptionList).clear_options()
        self._status("Searching…")
        search = self.search

        def guarded() -> tuple[list[dict] | None, HerdboardError | None]:
            try:
                return search(term), None
            except HerdboardError as error:
                return None, error

        self.run_worker(guarded, thread=True, exit_on_error=False)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        self.searching = False
        results, error = None, None
        if event.state == WorkerState.SUCCESS:
            results, error = event.worker.result
        elif event.state == WorkerState.ERROR:
            error = event.worker.error
        if error is not None or results is None:
            self._status(str(error or "The search was cancelled"), error=True)
            return
        self.results = results
        options = self.query_one("#issue-results", OptionList)
        options.add_options([self._label(issue) for issue in results])
        if not results:
            self._status("No open issues match.")
            return
        self._status(f"{len(results)} open issue{'s' if len(results) != 1 else ''}")
        options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.results[event.option_index])


class PromoteScreen(ModalBase):
    """Choose the Linear team and project of a todo's promotion. Nothing starts chosen.

    The teams, then the projects of the chosen team, load in threads. A list that a newer
    team choice made obsolete is dropped. Ctrl+s returns (team, project or None); escape None.
    """

    BINDINGS = [Binding("ctrl+s", "promote", "Promote")]

    def __init__(self, ref: str, title: str, load_teams: Callable[[], list[dict]],
                 load_projects: Callable[[str], list[dict]]):
        super().__init__()
        self.ref = ref
        self.title_text = title
        self.load_teams = load_teams
        self.load_projects = load_projects
        self.teams: list[dict] | None = None
        self.projects: list[dict] | None = None
        self.team: dict | None = None
        self.project: dict | None = None
        # True once a project option, "No project" too, was chosen for the current team.
        self.project_chosen = False
        # True when the last list failed to load.
        self.failed = False
        # Counts the loads. Only the result of the newest load is shown.
        self.sequence = 0

    def compose(self) -> ComposeResult:
        note = (f"This creates Linear issues for {self.title_text}, its checklist and its sub-cards, "
                "then moves the todo to Done.")
        with Vertical(classes="dialog"):
            yield Label(f"Promote {self.title_text} to Linear")
            yield RichStatic(Text(note), id="promote-note", classes="dim")
            yield Label("Team")
            yield OptionList(id="promote-teams")
            yield Label("Project")
            yield OptionList(id="promote-projects", disabled=True)
            yield RichStatic(Text("Loading teams…"), id="promote-status", classes="dim")
            yield Label("enter or click: choose · ctrl+s: promote · escape: cancel", classes="dim")

    def on_mount(self) -> None:
        # Textual also runs ModalBase.on_mount, which adds the key bar.
        self._load(self.load_teams)

    @property
    def ready(self) -> bool:
        return self.team is not None and self.project_chosen

    def key_applies(self, action: str) -> bool:
        return action != "promote" or self.ready

    def _status(self, message: str, error: bool = False) -> None:
        status = self.query_one("#promote-status", RichStatic)
        status.rich_text = Text(message)
        status.plain = message
        status.set_class(error, "error")
        status.set_class(not error, "dim")
        status.update(status.rich_text)

    def _load(self, load: Callable[[], list[dict]]) -> None:
        self.sequence += 1
        sequence = self.sequence

        def guarded() -> tuple[int, list[dict] | None, HerdboardError | None]:
            try:
                return sequence, load(), None
            except HerdboardError as error:
                return sequence, None, error

        self.run_worker(guarded, thread=True, exit_on_error=False)

    def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        if event.state == WorkerState.ERROR:
            self.failed = True
            self._status(str(event.worker.error), error=True)
        if event.state != WorkerState.SUCCESS:
            return
        sequence, found, error = event.worker.result
        if sequence != self.sequence:
            return
        if error is not None:
            self.failed = True
            self._status(str(error), error=True)
            return
        self.failed = False
        if self.team is None:
            self._show_teams(found)
        else:
            self._show_projects(found)

    def _show_teams(self, teams: list[dict]) -> None:
        self.teams = teams
        options = self.query_one("#promote-teams", OptionList)
        options.add_options([f"{team['key']}  {team['name']}" for team in teams])
        if not teams:
            self._status("Your Linear account has no Linear teams.", error=True)
            return
        self._status("Choose a team.")
        options.focus()

    def _show_projects(self, projects: list[dict]) -> None:
        self.projects = projects
        names = [project["name"].casefold() for project in projects]
        labels = ["No project"]
        for project in projects:
            label = project["name"]
            if names.count(project["name"].casefold()) > 1:
                teams = ", ".join(team["key"] for team in project["teams"])
                label += f"  ({teams} · {project['id']})"
            labels.append(label)
        options = self.query_one("#promote-projects", OptionList)
        options.add_options(labels)
        options.disabled = False
        team = self.team["key"]
        self._status(f"Choose a project of {team}, or No project." if projects
                     else f"Team {team} has no projects. Choose No project.")
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_list.id == "promote-teams":
            self._choose_team(self.teams[event.option_index])
            return
        if self.projects is None:
            return
        self.project = None if event.option_index == 0 else self.projects[event.option_index - 1]
        self.project_chosen = True
        name = self.project["name"] if self.project else "no project"
        self._status(f"Team {self.team['key']}, {name}. Press ctrl+s to promote.")
        self.refresh_bindings()

    def _choose_team(self, team: dict) -> None:
        self.team = team
        self.projects = None
        self.project = None
        self.project_chosen = False
        projects = self.query_one("#promote-projects", OptionList)
        projects.clear_options()
        projects.disabled = True
        self._status(f"Loading the projects of {team['key']}…")
        self.refresh_bindings()
        load = self.load_projects
        self._load(lambda: load(team["id"]))

    def action_promote(self) -> None:
        if not self.ready:
            self.notify("Choose a team and a project, or No project.", severity="warning")
            return
        self.dismiss((self.team, self.project))


class ParentScreen(ModalBase):
    """Choose the parent of a todo: another todo on its board, or no parent."""

    def __init__(self, card: CardView, options: list[CardView]):
        super().__init__()
        self.card = card
        self.options = [None, *options]
        self.labels = ["No parent", *(split_title(option.title)[0] or option.title for option in options)]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Parent of {split_title(self.card.title)[0]}")
            yield OptionList(*self.labels, id="parent-choice")
            yield Label("enter: choose, escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        option = self.options[event.option_index]
        self.dismiss(("parent", option.ref if option else None))


# The card menu's entries, in the order of the board's key bindings: (action, key, label).
MENU_ENTRIES = [
    ("detail", "enter", "Detail"),
    ("done", "d", "Done"),
    ("edit", "e", "Edit"),
    ("move_board", "b", "Board"),
    ("delete", "x", "Delete"),
    ("archive", "A", "Archive"),
    ("promote", "T", "Promote to Linear"),
    ("start", "s", "Start"),
    ("choose_agent", "S", "Choose agent"),
    ("go", "g", "Go to workspace"),
    ("review", "R", "Review"),
    ("plan", "P", "Plan"),
    ("parent", "p", "Parent"),
    ("merge", "m", "Merge"),
    ("finish", "f", "Finish"),
]


# The board keys that act on the selected card. Without a card, the key bar dims them.
CARD_KEY_ACTIONS = {action for action, _key, _label in MENU_ENTRIES} | {"move", "shift"}


def promote_refusal(card: CardView) -> str | None:
    """Why the card cannot be promoted to Linear, or None. Sub-cards with a worktree are checked apart."""
    name = split_title(card.title)[0] or card.title
    if card.source == "linear" or card.read_only:
        return "Only a local todo can be promoted to Linear."
    if card.archived:
        return f"{name} is archived. Unarchive it before you promote it."
    if card.parent:
        return f"{name} is a sub-card. Promote its parent: sub-cards become sub-issues."
    done = promoted_identifier(card.title)
    if done:
        return f"{name} was already promoted to {done}."
    if card.worktree:
        return f"{name} has a worktree. Finish the card before you promote it."
    return None


def family_refusal(card: CardView, children: list[CardView]) -> str | None:
    """Why the card cannot be promoted, its sub-cards included: promotion completes them too."""
    refusal = promote_refusal(card)
    if refusal is not None:
        return refusal
    for child in children:
        if child.worktree:
            name = split_title(child.title)[0] or child.title
            return (f"Its sub-card {name} has a worktree. "
                    "Finish the sub-card before you promote the parent.")
    return None


def card_actions(card: CardView) -> list[tuple[str, str]]:
    """The card menu's (label, action) pairs: only the actions that apply to the card.

    The rules use only what the board already knows. An action still makes its own checks.
    """
    todo = card.source != "linear"
    wanted = {"detail"}
    if card.archived and todo:
        wanted |= {"archive", "delete"}
    else:
        if todo:
            wanted |= {"edit", "parent", "move_board", "delete"}
        if not card.checked:
            wanted.add("done")
        if card.worktree:
            # `start` stays: it resumes a start that has steps left.
            wanted |= {"go", "review", "merge", "finish", "start"}
        else:
            wanted |= {"start", "choose_agent", "plan"}
            if todo:
                wanted.add("archive")
        if promote_refusal(card) is None:
            wanted.add("promote")
        if card.checked and card.worktree_left:
            wanted.add("finish")
    return [(f"{key}  {'Unarchive' if action == 'archive' and card.archived else label}", action)
            for action, key, label in MENU_ENTRIES if action in wanted]


class CardMenuScreen(ModalBase):
    """The actions that apply to a card. It returns the chosen action's name, or None."""

    def __init__(self, title: str, actions: list[tuple[str, str]]):
        super().__init__()
        self.title_text = title
        self.actions = actions

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield RichStatic(Text(self.title_text), id="card-menu-title")
            yield OptionList(*(label for label, _ in self.actions), id="card-menu")
            yield Label("enter or click: run, escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.actions[event.option_index][1])


class DeleteScreen(ModalBase):
    """Ask before a todo is deleted."""

    BINDINGS = [
        Binding("y", "answer(True)", "Delete"),
        Binding("n", "answer(False)", "Keep"),
    ]

    def __init__(self, card: CardView):
        super().__init__()
        self.card = card

    def compose(self) -> ComposeResult:
        text = f"Delete {self.card.ref}: {self.card.title}?"
        prompt = Static(Text(text), id="delete-prompt")
        prompt.plain = text
        with Vertical(classes="dialog"):
            yield prompt
            yield Label("y: delete, n or escape: keep", classes="dim")

    def action_answer(self, delete: bool) -> None:
        self.dismiss(delete)


class ConfirmScreen(ModalBase):
    """Ask before a card action that changes Git: y runs it, n or escape does not."""

    BINDINGS = [
        Binding("y", "answer(True)", "Yes"),
        Binding("n", "answer(False)", "No"),
    ]

    def __init__(self, question: str, detail: str):
        super().__init__()
        self.question = question
        self.detail = detail

    def compose(self) -> ComposeResult:
        prompt = Static(Text(self.question), id="confirm-prompt")
        prompt.plain = self.question
        with Vertical(classes="dialog"):
            yield prompt
            yield Label(self.detail, classes="dim")
            yield Label("y: yes, n or escape: no", classes="dim")

    def action_answer(self, answer: bool) -> None:
        self.dismiss(answer)


class ModelScreen(ModalBase):
    """Choose pi's model and thinking level before a start. Enter on a model starts."""

    def __init__(self, default: str | None, models: list[str]):
        super().__init__()
        self.models = [None, *models]
        self.labels = [f"pi default ({default})" if default else "pi default", *models]

    def compose(self) -> ComposeResult:
        levels = [("default", "default"), *((level, level) for level in THINKING_LEVELS)]
        with Vertical(classes="dialog"):
            yield Label("Choose pi's model")
            yield OptionList(*self.labels, id="models")
            thinking = Select(levels, value="default", allow_blank=False, id="thinking")
            thinking.border_title = "Thinking"
            yield thinking
            yield Label("enter: start · tab: thinking level · escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        thinking = self.query_one("#thinking", Select).value
        self.dismiss((self.models[event.option_index], None if thinking == "default" else thinking))


class ConfigScreen(ModalBase):
    """Two tabs: the global settings of all boards, and a board's merge mode, Linear project links
    and pinned issues.

    Enter on a link or a pin marks it for removal, or keeps it again. ctrl+s writes the changes of
    both tabs.
    """

    BINDINGS = [Binding("ctrl+s", "save", "Save")]
    AUTO_FOCUS = "#config-theme"
    # The global settings that the Global tab shows, in the order of its fields.
    GLOBAL_KEYS = ("theme", "start_agent", "start_model", "start_thinking", "merge", "icon_style",
                   "tag_style", "card_style")

    def __init__(self, path: Path | None, meta: BoardMeta, can_link: bool, settings: dict[str, str | None],
                 themes: list[str], pi_choices: tuple[str | None, list[str]]):
        super().__init__()
        self.path = path
        self.meta = meta
        self.can_link = can_link
        # The saved global settings; a save writes only the ones that differ.
        self.settings = dict(settings)
        self.themes = themes
        self.pi_choices = pi_choices
        self.agent = settings["start_agent"]
        self.removed: set[str] = set()
        self.removed_pins: set[str] = set()

    def _pin_label(self, pin: IssuePin) -> Text:
        text = Text(pin.identifier)
        if pin.id in self.removed_pins:
            text.stylize("strike")
            text.append("  remove", style="bold red")
        return text

    def _link_label(self, link: ProjectLink) -> Text:
        text = Text(link.name)
        if link.labels:
            text.append(" · labels: " + ", ".join(label.name for label in link.labels), style="dim")
        if link.id in self.removed:
            text.stylize("strike")
            text.append("  remove", style="bold red")
        return text

    @staticmethod
    def _titled(widget, title: str):
        widget.border_title = title
        return widget

    def _model_options(self, agent: str) -> list[tuple[str, str]]:
        """The models that a start offers for `agent`, after "default" for the agent's own model.

        The saved model stays an option for the saved agent, so a save of other settings keeps it.
        """
        if agent == "pi":
            default, models = self.pi_choices
            first = f"Agent default ({default})" if default else "Agent default"
        else:
            first, models = "Agent default", [model for model in ClaudeModelScreen.MODELS if model]
        options = [(first, "default")]
        for model in models:
            if model not in (value for _, value in options):
                options.append((model, model))
        saved = self.settings["start_model"]
        if agent == self.settings["start_agent"] and saved and saved not in (value for _, value in options):
            options.append((f"{saved} (current configured model)", saved))
        return options

    def _board_merge_options(self, default: str) -> list[tuple[str, str]]:
        return [(f"Global default ({default})", "default"),
                ("local: merge into the base branch", "local"),
                ("pr: open a draft pull request", "pr")]

    def compose(self) -> ComposeResult:
        settings = self.settings
        thinking = [("Agent default", "default"), *((level, level) for level in THINKING_LEVELS)]
        with Vertical(classes="dialog card-form config"):
            yield RichStatic(Text("Herdboard config", style="bold"), id="config-heading")
            with TabbedContent(initial="config-global", id="config-tabs"):
                with TabPane("Global (all boards)", id="config-global"):
                    with VerticalScroll(classes="config-pane", can_focus=False):
                        yield self._titled(Select([(name, name) for name in self.themes], value=settings["theme"],
                                                  allow_blank=False, id="config-theme"), "Theme")
                        yield self._titled(Select([("claude", "claude"), ("pi", "pi")],
                                                  value=settings["start_agent"], allow_blank=False,
                                                  id="config-agent"), "Agent")
                        yield self._titled(Select(self._model_options(self.agent),
                                                  value=settings["start_model"] or "default", allow_blank=False,
                                                  id="config-model"), "Model")
                        yield self._titled(Select(thinking, value=settings["start_thinking"] or "default",
                                                  allow_blank=False, disabled=self.agent != "pi",
                                                  id="config-thinking"), "Thinking (pi only)")
                        yield self._titled(Select([("local: merge into the base branch", "local"),
                                                   ("pr: open a draft pull request", "pr")],
                                                  value=settings["merge"], allow_blank=False,
                                                  id="config-default-merge"), "Merge")
                        yield self._titled(Select([("plain: text labels", "plain"), ("nerd: Nerd Font icons", "nerd")],
                                                  value=settings["icon_style"], allow_blank=False,
                                                  id="config-icon-style"), "Icons")
                        yield self._titled(Select([("outline: text on a tint", "outline"),
                                                   ("round: Nerd Font ends", "round"),
                                                   ("block: square ends", "block")],
                                                  value=settings["tag_style"], allow_blank=False,
                                                  id="config-tag-style"), "Tags")
                        yield self._titled(Select([("full: a border and a description", "full"),
                                                   ("compact: title, tags and status", "compact")],
                                                  value=settings["card_style"], allow_blank=False,
                                                  id="config-card-style"), "Cards")
                        yield Label("Nerd icons and round tags need Nerd Font Mono.", classes="dim")
                with TabPane("Current board", id="config-board"):
                    with VerticalScroll(classes="config-pane", can_focus=False):
                        yield from self._compose_board()
            yield RichStatic(Text(""), id="config-error", classes="error")
            yield Label("ctrl+s: save both tabs · tab: next field · escape: cancel", classes="dim")

    def _compose_board(self) -> ComposeResult:
        if not self.path:
            yield Label("No repository board. Press i in a repository to create one.",
                        id="config-board-hint", classes="dim")
            return
        yield RichStatic(Text(f"Board: {self.path.stem}", style="bold"), id="config-board-name")
        if self.meta.repo:
            yield Label(f"repo: {self.meta.repo}", classes="dim")
        yield self._titled(Select(self._board_merge_options(self.settings["merge"]),
                                  value=self.meta.merge or "default", allow_blank=False,
                                  id="config-merge"), "Merge")
        if self.meta.links:
            yield self._titled(OptionList(*[self._link_label(link) for link in self.meta.links],
                                          id="config-links"), "Linear projects")
        else:
            yield Label("No Linear projects are linked.", classes="dim")
        if self.meta.pins:
            yield self._titled(OptionList(*[self._pin_label(pin) for pin in self.meta.pins],
                                          id="config-pins"), "Pinned Linear issues")
        if self.can_link:
            with Horizontal(classes="form-row"):
                yield self._titled(Input(placeholder="API Platform", id="config-project",
                                         select_on_focus=False), "Link a Linear project")
                yield self._titled(Input(placeholder="repo:api, bug", id="config-labels",
                                         select_on_focus=False), "Labels")
        if self.meta.links or self.meta.pins:
            yield Label("enter on a project or an issue: remove it, or keep it again", classes="dim")

    def on_select_changed(self, event: Select.Changed) -> None:
        if event.select.id == "config-agent" and event.value != self.agent:
            # A new agent starts from its own default model. The thinking level stays for pi.
            self.agent = event.value
            self.query_one("#config-model", Select).set_options(self._model_options(self.agent))
            self.query_one("#config-thinking", Select).disabled = self.agent != "pi"
        elif event.select.id == "config-default-merge" and self.path:
            board = self.query_one("#config-merge", Select)
            value = board.value
            board.set_options(self._board_merge_options(event.value))
            board.value = value

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        if event.option_list.id == "config-pins":
            pin = self.meta.pins[event.option_index]
            self.removed_pins ^= {pin.id}
            event.option_list.replace_option_prompt_at_index(event.option_index, self._pin_label(pin))
            return
        link = self.meta.links[event.option_index]
        self.removed ^= {link.id}
        event.option_list.replace_option_prompt_at_index(event.option_index, self._link_label(link))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the project field moves to its labels. Enter in the labels field saves."""
        event.stop()
        if event.input.id == "config-project":
            self.query_one("#config-labels", Input).focus()
        elif event.input.id == "config-labels":
            self.action_save()

    def show_error(self, message: str) -> None:
        error = self.query_one("#config-error", RichStatic)
        error.rich_text = Text(message)
        error.plain = message
        error.update(error.rich_text)
        self.call_after_refresh(error.scroll_visible, animate=False)

    def global_values(self) -> dict[str, str | None]:
        """The Global tab's values, with None for an agent default."""
        values = {}
        for key, widget in zip(self.GLOBAL_KEYS, ("theme", "agent", "model", "thinking", "default-merge",
                                                  "icon-style", "tag-style", "card-style")):
            value = self.query_one(f"#config-{widget}", Select).value
            values[key] = None if value == "default" else value
        return values

    def action_save(self) -> None:
        merge = self.query_one("#config-merge", Select).value if self.path else "default"
        changed = {key: value for key, value in self.global_values().items() if value != self.settings[key]}
        project, labels = "", []
        if self.can_link:
            project = self.query_one("#config-project", Input).value.strip()
            labels = [label.strip() for label in self.query_one("#config-labels", Input).value.split(",")]
        self.app.submit_config(self, None if merge == "default" else merge, set(self.removed),
                               project, [label for label in labels if label], set(self.removed_pins),
                               settings=changed)


# The tabs under the header: (tab, label). G goes to the next tab, and from the last to the first.
TABS = [("board", "Board"), ("prs", "Pull requests"), ("notes", "Notes")]
TAB_KEY = "G"
# Each tab's icon: its Nerd Font icon and its plain icon.
TAB_ICONS = {"board": ("\uf0db", "▦"), "prs": ("\uf407", "⑂"), "notes": ("\uf249", "✎")}
# The actions that the tabs show, so the key list leaves them out.
TAB_ACTIONS = {"pull_requests", "next_tab"}


def herdboard_version() -> str:
    """The installed herdboard version, or "" without package metadata."""
    try:
        return importlib.metadata.version("herdboard")
    except importlib.metadata.PackageNotFoundError:
        return ""


class HeaderBar(Horizontal):
    """The top row: herdboard and the board or repository at the left, the view and the version at
    the right."""

    def __init__(self, place: str = "", view: str = ""):
        super().__init__(id="header-bar")
        self.version = herdboard_version()
        self.place, self.view = place, view
        self.plain = self._plain()

    def compose(self) -> ComposeResult:
        yield Static("herdboard", id="header-app")
        yield Static("│", id="header-rule")
        yield Static(self.place, id="header-place")
        yield Static(self._right(), id="header-view")

    def _right(self) -> str:
        return "  ".join(part for part in (self.view, f"v{self.version}" if self.version else "") if part)

    def _plain(self) -> str:
        return f"herdboard │ {self.place}" + (f" │ {self._right()}" if self._right() else "")

    def show(self, place: str, view: str) -> None:
        self.place, self.view = place, view
        self.plain = self._plain()
        if self.is_mounted:
            self.query_one("#header-place", Static).update(Text(place, no_wrap=True, overflow="ellipsis"))
            self.query_one("#header-view", Static).update(self._right())


class TabBar(Horizontal):
    """The row under the header: the Board, Pull requests and Notes tabs, each with its icon, and the
    open pull request count once the list has read it. A click on another tab switches to it."""

    def __init__(self, active: str):
        super().__init__(id="tab-bar")
        self.active = active

    def compose(self) -> ComposeResult:
        for tab, label in TABS:
            text = self._tab_text(tab, label)
            widget = Container(Static(text, classes="tab-label"), id=f"tab-{tab}",
                               classes="tab active" if tab == self.active else "tab")
            widget.plain = text.plain
            yield widget

    def _icon(self, tab: str) -> str:
        nerd, plain = TAB_ICONS[tab]
        try:
            style = self.app.store.config.icon_style
        except NoActiveAppError:
            # A tab bar outside a running board.
            style = "plain"
        return nerd if style == "nerd" else plain

    def _count(self, tab: str) -> int | None:
        try:
            return self.app.pr_count if tab == "prs" else None
        except NoActiveAppError:
            return None

    def _tab_text(self, tab: str, label: str) -> Text:
        """The tab's icon, label and count. The next tab shows the switch key; the others keep its
        place blank, so a switch neither resizes a tab nor moves its text. The current tab's bold comes
        from the style sheet."""
        text = Text(f"{self._icon(tab)} {label}")
        count = self._count(tab)
        if count is not None:
            text.append(f" {count}", style="bold")
        text.append(" ")
        if tab == self.next_tab():
            text.append(TAB_KEY, style="bold yellow")
        else:
            text.append(" " * len(TAB_KEY))
        return text

    def next_tab(self) -> str:
        """The tab that the switch key goes to."""
        names = [tab for tab, _label in TABS]
        return names[(names.index(self.active) + 1) % len(names)]

    def show_counts(self) -> None:
        for (tab, label), widget in zip(TABS, self.query(".tab")):
            text = self._tab_text(tab, label)
            if widget.plain != text.plain:
                widget.plain = text.plain
                widget.query_one(".tab-label", Static).update(text)

    def tab_labels(self) -> list[str]:
        return [" ".join(widget.plain.split()) for widget in self.query(".tab")]

    def on_click(self, event: events.Click) -> None:
        widget = event.widget
        while widget is not None and widget is not self and not widget.has_class("tab"):
            widget = widget.parent
        tab = (widget.id or "").removeprefix("tab-") if widget is not None else ""
        if tab in dict(TABS) and tab != self.active:
            event.stop()
            self.app.show_tab(tab)


class KeyBar(Static):
    """The bottom row: the keys of the current screen, in the place of a footer.

    The keys come from the screen's active bindings, as Textual's footer finds them. The row wraps
    between two keys when they do not fit. A click on a key presses it."""

    def __init__(self):
        super().__init__("", id="key-bar")
        self.plain = ""

    def on_mount(self) -> None:
        self.screen.bindings_updated_signal.subscribe(self, self._bindings_changed)
        self.call_after_refresh(self.show_keys)

    def on_unmount(self) -> None:
        self.screen.bindings_updated_signal.unsubscribe(self)

    def _bindings_changed(self, screen) -> None:
        if self.is_attached and screen is self.screen:
            self.show_keys()

    def key_labels(self) -> list[Text]:
        """Each key of the current screen once, in its binding order: the key as a keycap in reverse
        colors, then its description. No-break spaces keep a key and its description together.
        A disabled key, or one that its node's `key_applies` rejects, has a dim keycap."""
        seen = set()
        labels = []
        for node, binding, enabled, _tooltip in self.screen.active_bindings.values():
            if not binding.show or binding.action in seen or binding.action in TAB_ACTIONS:
                continue
            seen.add(binding.action)
            # A key that has nothing to act on is dimmed, but still runs and says why.
            applies = getattr(node, "key_applies", None)
            enabled = enabled and (applies is None or applies(binding.action))
            click = Style(meta={"@click": f"app.simulate_key({binding.key!r})"})
            key = self.app.get_key_display(binding).replace(" ", "\u00a0")
            text = Text()
            text.append(f"\u00a0{key}\u00a0", style=Style(bold=True, reverse=True, dim=not enabled) + click)
            text.append(f"\u00a0{binding.description.replace(' ', chr(0xa0))}", style=click)
            labels.append(text)
        return labels

    def keys_text(self) -> Text:
        return Text("   ").join(self.key_labels())

    def _wrapped_keys(self, width: int) -> Text:
        """The keys in lines of at most `width` cells. Rich breaks lines at any space, a no-break
        space too, so the lines break only between two keys."""
        lines: list[Text] = []
        for label in self.key_labels():
            if lines and lines[-1].cell_len + 3 + label.cell_len <= width:
                lines[-1].append("   ")
                lines[-1].append_text(label)
            else:
                lines.append(label)
        text = Text("\n").join(lines)
        text.no_wrap = True
        text.overflow = "ellipsis"
        return text

    def show_keys(self) -> None:
        text = self.keys_text()
        self.plain = text.plain
        width = self.content_size.width
        self.update(self._wrapped_keys(width) if width else text)

    def on_resize(self, event: events.Resize) -> None:
        self.call_after_refresh(self.show_keys)


class PullRequestService:
    """The pull request list's calls into `pr_reviews`, for the main checkout of the board's directory."""

    def __init__(self, directory: Path):
        self.directory = Path(directory)
        self._target: tuple[Path, str] | None = None

    def target(self) -> tuple[Path, str]:
        if self._target is None:
            checkout = start_module._main_checkout(self.directory)
            if checkout is None:
                raise HerdboardError(f"{self.directory} is not in a Git repository")
            self._target = (checkout, pr_reviews.base_repository(checkout))
        return self._target

    def key(self, number: int) -> str:
        return pr_reviews.review_key(self.target()[1], number)

    def page(self, text: str, after: str | None) -> tuple[str, pr_reviews.Page]:
        checkout, repo = self.target()
        page = pr_reviews.list_page(checkout, repo, text, after=after)
        pr_reviews.annotate(page.prs, repo, pr_reviews.records())
        return repo, page

    def details(self, number: int) -> dict:
        checkout, repo = self.target()
        return pr_reviews.pr_details(checkout, repo, number)

    def review(self, number: int, kind: str, model: str | None, thinking: str | None) -> dict:
        checkout, repo = self.target()
        return pr_reviews.request(checkout, repo, number, kind, model, thinking)

    def refresh(self, number: int) -> dict:
        return pr_reviews.refresh(self.key(number))

    def go(self, number: int) -> dict:
        return pr_reviews.go(self.key(number))

    def finish(self, number: int, force: bool) -> dict:
        return pr_reviews.finish(self.key(number), force=force)


def pr_icon(state: str, icon_style: str) -> str:
    nerd, plain, _ = PR_STATES[state]
    return nerd if icon_style == "nerd" else plain


def pr_label(state: str, label: str, icon_style: str) -> Text:
    """A state label with its icon, in the state's style."""
    icon = pr_icon(state, icon_style)
    return Text(f"{icon} {label}" if icon else label, style=PR_STATES[state][2])


def label_chip(label: dict) -> Text:
    """A GitHub label in its own color, with black or white text, whichever reads better."""
    color = label.get("color") or ""
    if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        return Text(f" {label['name']} ", style="reverse")
    red, green, blue = (int(color[i:i + 2], 16) for i in (0, 2, 4))
    text = "black" if 0.299 * red + 0.587 * green + 0.114 * blue > 150 else "white"
    return Text(f" {label['name']} ", style=f"{text} on #{color}")


def pr_ci_cell(pr: pr_reviews.PullRequest, icon_style: str) -> Text:
    return pr_label(f"ci:{pr.ci}", pr.ci, icon_style) if pr.ci else Text("")


def pr_github_cell(pr: pr_reviews.PullRequest, icon_style: str) -> Text:
    """GitHub's side of the review: a request for your review, and GitHub's review decision."""
    parts = []
    if pr.requested:
        parts.append(pr_label("gh:requested", "your review", icon_style))
    # A request for your review already says that a review is required.
    if pr.decision and not (pr.requested and pr.decision == "review required"):
        parts.append(pr_label(f"gh:{pr.decision}", pr.decision, icon_style))
    return Text("  ").join(parts)


def pr_agent_cell(pr: pr_reviews.PullRequest, icon_style: str, agent_logos: bool = False) -> Text:
    """The local agent review that herdboard ran, after the agent's logo, and new commits since then."""
    parts = []
    logo = AGENT_LOGOS.get((pr.review or {}).get("agent")) if agent_logos and pr.status else None
    if logo:
        parts.append(Text(logo))
    if pr.status:
        parts.append(pr_label(f"agent:{pr.status}", pr.status, icon_style))
    if pr.new_commits:
        parts.append(pr_label("agent:new commits", "new commits", icon_style))
    return Text(" ").join(parts[:1] + [Text("  ").join(parts[1:])]) if logo else Text("  ").join(parts)


def pr_size_cell(pr: pr_reviews.PullRequest) -> Text:
    text = Text(justify="right")
    text.append(f"+{pr.additions}", style="green")
    text.append(" ")
    text.append(f"−{pr.deletions}", style="red")
    return text


def pr_title_line(pr: pr_reviews.PullRequest) -> Text:
    """The title, then its first labels. The row cuts the line to fit."""
    text = Text(pr.title, style="dim" if pr.draft else "bold")
    for label in pr.labels[:PR_LIST_LABELS]:
        text.append(" ")
        text.append_text(label_chip(label))
    if len(pr.labels) > PR_LIST_LABELS:
        text.append(f" +{len(pr.labels) - PR_LIST_LABELS}", style="dim")
    return text


def ago(stamp: str, now: datetime | None = None) -> str:
    """How long ago a GitHub time was: `5 min ago`, `3 hours ago`, `2 days ago`; "" when unknown."""
    try:
        then = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    except ValueError:
        return ""
    now = now or datetime.now(then.tzinfo)
    seconds = max(0, int((now - then).total_seconds()))
    for size, unit in ((7 * 86400, "week"), (86400, "day"), (3600, "hour"), (60, "min")):
        if seconds >= size:
            count = seconds // size
            return f"{count} {unit}{'' if count == 1 or unit == 'min' else 's'} ago"
    return "just now"


# The parts of a pull request row, in the order that `PullRequestsScreen.rows` gives them.
PR_FIELDS = ["", "PR", "Title", "Author", "Size", "CI", "GitHub review", "Agent review (local)", "Notes",
             "Comments", "Branch", "Updated"]


def pr_fields(pr: pr_reviews.PullRequest, icon_style: str, now: datetime | None = None,
              agent_logos: bool = False) -> list[Text]:
    """Each part of a pull request row, as `PR_FIELDS` names them."""
    state = "draft" if pr.draft else "open"
    notes = Text(f"{pr.notes} {pr_icon('notes', icon_style)}".rstrip()) if pr.notes else Text("")
    comments = Text(icon_label("comments", f"{pr.comments} comments", icon_style), style="dim")
    branch = Text(icon_label("branch", pr.head_branch, icon_style), style="cyan")
    if pr.base_branch:
        branch.append(f" → {pr.base_branch}", style="dim")
    updated = ago(pr.updated, now)
    return [Text(pr_icon(state, icon_style), style=PR_STATES[state][2]),
            Text(f"#{pr.number}", style="bold"),
            pr_title_line(pr),
            Text(pr.author, style="bold cyan" if pr.mine else ""),
            pr_size_cell(pr),
            pr_ci_cell(pr, icon_style),
            pr_github_cell(pr, icon_style),
            pr_agent_cell(pr, icon_style, agent_logos),
            notes,
            comments,
            branch,
            Text(f"updated {updated}" if updated else "", style="dim")]


# The narrowest row that keeps the fixed columns of the wide layout and a readable title.
PR_ROW_WIDE = 110


class PullRequestRowContent:
    """A pull request in three rows: the number and title, with the author, size and branch under it.
    A wide row has fixed columns at the right: CI, the reviews, the comments and notes, and when it was
    updated. A narrow row puts the reviews under the title, and CI, the comments and the time at the right."""

    def __init__(self, fields: list[Text]):
        self.fields = fields

    def __rich_console__(self, console, options):
        icon, number, title, author, size, ci, github, agent, notes, comments, branch, updated = self.fields
        who = author.copy()
        if size:
            who.append("  ")
            who.append_text(size)
        grid = Table.grid(expand=True, padding=(0, 2))
        # The state icon and the number, up to six digits.
        grid.add_column(no_wrap=True, width=9)
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        if options.max_width >= PR_ROW_WIDE:
            # Fixed widths, so the parts line up from row to row.
            grid.add_column(no_wrap=True, width=10, overflow="ellipsis")
            grid.add_column(no_wrap=True, width=26, overflow="ellipsis")
            grid.add_column(no_wrap=True, width=20, overflow="ellipsis")
            grid.add_row(Text.assemble(icon, " ", number), title, ci, github, comments)
            grid.add_row(Text(""), who, Text(""), agent, notes)
            grid.add_row(Text(""), branch, Text(""), Text(""), updated)
        else:
            grid.add_column(no_wrap=True, overflow="ellipsis", justify="right")
            reviews = Text("  ").join(part for part in (who, github, agent) if part)
            talk = Text("  ").join(part for part in (notes, comments) if part)
            grid.add_row(Text.assemble(icon, " ", number), title, ci)
            grid.add_row(Text(""), reviews, talk)
            grid.add_row(Text(""), branch, updated)
        yield grid


def pr_row_content(fields: list[Text]) -> PullRequestRowContent:
    return PullRequestRowContent(fields)


class PullRequestRow(Static):
    """One pull request: a bordered box in the color of its CI state."""

    # A click opens the details, so it must not also select text.
    ALLOW_SELECT = False

    def __init__(self, index: int, pr: pr_reviews.PullRequest, fields: list[Text]):
        super().__init__(pr_row_content(fields), classes=f"pr-row ci-{pr.ci or 'none'}")
        self.index = index
        self.fields = fields

    def show(self, pr: pr_reviews.PullRequest, fields: list[Text]) -> None:
        for name in [name for name in self.classes if name.startswith("ci-")]:
            self.remove_class(name)
        self.add_class(f"ci-{pr.ci or 'none'}")
        self.fields = fields
        self.update(pr_row_content(fields))

    def on_click(self, event: events.Click) -> None:
        event.stop()
        self.parent.select(self.index, open_details=True)


class PullRequestList(VerticalScroll, can_focus=True):
    """The pull request rows, with one selected row. One click on a row selects it and, as enter does,
    opens its details."""

    # The arrows move the selected row, not only the scroll.
    BINDINGS = [
        Binding("down", "screen.cursor(1)", "Next", show=False),
        Binding("up", "screen.cursor(-1)", "Previous", show=False),
    ]

    class Selected(Message):
        """A click chose a row."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.cursor_row = 0

    @property
    def row_count(self) -> int:
        return len(self.query(PullRequestRow))

    async def show(self, prs: list[pr_reviews.PullRequest], rows: list[list[Text]]) -> None:
        """Show the rows. Rows for the same pull requests are updated in place, so the scroll stays."""
        widgets = list(self.query(PullRequestRow))
        if len(widgets) == len(prs):
            for widget, pr, fields in zip(widgets, prs, rows):
                widget.show(pr, fields)
        else:
            await self.remove_children()
            await self.mount_all([PullRequestRow(index, pr, fields)
                                  for index, (pr, fields) in enumerate(zip(prs, rows))])

    def move_cursor(self, row: int, scroll: bool = True) -> None:
        widgets = list(self.query(PullRequestRow))
        self.cursor_row = max(0, min(row, len(widgets) - 1)) if widgets else 0
        for index, widget in enumerate(widgets):
            widget.set_class(index == self.cursor_row, "selected")
        if scroll and widgets:
            self.call_after_refresh(widgets[self.cursor_row].scroll_visible, animate=False)
        # The selected pull request sets which keys the key bar dims.
        self.screen.refresh_bindings()

    def select(self, index: int, open_details: bool = False) -> None:
        self.focus()
        self.move_cursor(index)
        if open_details:
            self.post_message(self.Selected())


def check_order(check: dict) -> int:
    """Failed checks first, then running, passed and skipped ones."""
    return ["failed", "pending", "passed", "skipped"].index(check["state"]) \
        if check["state"] in ("failed", "pending", "passed", "skipped") else 0


def pr_key_applies(pr: pr_reviews.PullRequest | None, action: str) -> bool:
    """Whether a pull request key has something to act on. Go, refresh and finish need a local review."""
    if action in ("detail", "review", "open"):
        return pr is not None
    if action in ("go", "refresh", "finish"):
        return pr is not None and pr.review is not None
    return True


class PullRequestDetailScreen(ModalBase):
    """One pull request: where it comes from, its size and labels, its checks, the issues it closes,
    the GitHub review, the local agent review and the description.

    The list does not hold the checks, issues, GitHub reviews and description; they are read when the
    screen opens. The action keys close the screen and return the action, which the list then runs."""

    BINDINGS = [
        Binding("R", "act('review')", "Review"),
        Binding("g", "act('go')", "Go to workspace"),
        Binding("u", "act('refresh')", "New commits"),
        Binding("f", "act('finish')", "Finish"),
        Binding("o", "act('open')", "Open in browser"),
    ]

    def __init__(self, pr: pr_reviews.PullRequest, repo: str, icon_style: str = "plain",
                 load_details: Callable[[], dict] | None = None):
        super().__init__()
        self.pr = pr
        self.repo = repo
        self.icon_style = icon_style
        self.load_details = load_details
        self.details: dict | None = None

    def _static(self, text: Text, **options) -> Static:
        widget = Static(text, **options)
        widget.plain = text.plain
        return widget

    def _set(self, selector: str, text: Text) -> None:
        widget = self.query_one(selector, Static)
        widget.plain = text.plain
        widget.update(text)

    def _line(self, key: str, text: str | Text) -> Text:
        """A line with the icon of `key` in the Nerd Font style."""
        icon = PR_STATES[key][0] if self.icon_style == "nerd" else ""
        line = Text(f"{icon} " if icon else "")
        line.append_text(text if isinstance(text, Text) else Text(text))
        return line

    def _agent_text(self) -> Text:
        pr, style = self.pr, self.icon_style
        review = pr.review or {}
        # Each line has its own style: a style of the whole Text would carry over to the lines after it.
        text = Text()
        text.append("herdboard's reviewer agent, in a workspace on this machine. Its notes are in Hunk.\n",
                    style="dim")
        if not pr.status:
            text.append("No agent review yet. Press s to start one.", style="dim")
            return text
        text.append_text(pr_label(f"agent:{pr.status}", pr.status, style))
        if pr.new_commits:
            text.append("  ")
            text.append_text(pr_label("agent:new commits", "new commits since the review", style))
        agent = " ".join(part for part in (review.get("agent"), review.get("model"), review.get("thinking")) if part)
        lines = [("agent", agent or "unknown"),
                 ("notes", f"{pr.notes} notes" if pr.notes is not None else "notes not counted yet")]
        if review.get("reviewed_sha"):
            lines.append(("commit", f"reviewed at {review['reviewed_sha'][:8]}"))
        if review.get("workspace_id"):
            lines.append(("workspace", f"workspace {review['workspace_id']}"))
        for key, line in lines:
            text.append("\n")
            text.append_text(self._line(key, line))
        if review.get("error"):
            text.append("\n" + review["error"], style="red")
        return text

    def _github_text(self) -> Text:
        pr, style = self.pr, self.icon_style
        text = Text()
        text.append("Reviews on GitHub. You submit yours with S in Hunk.\n", style="dim")
        head = [part for part in (pr_github_cell(pr, style),) if part.plain]
        text.append_text(head[0] if head else Text("No review decision: the repository requires none.",
                                                   style="dim"))
        if self.details is None:
            text.append("\nReading the reviews…", style="dim")
            return text
        reviews = self.details.get("reviews") or []
        if not reviews:
            text.append("\nNo reviews yet.", style="dim")
        for review in reviews:
            state = review["state"]
            key = f"gh:{state}" if f"gh:{state}" in PR_STATES else "gh:commented"
            line = Text("\n")
            line.append_text(pr_label(key, f"{review['author']} {state}", style))
            if review.get("at"):
                line.append(f"  {review['at'][:16].replace('T', ' ')}", style="dim")
            text.append_text(line)
        return text

    def _checks_text(self) -> Text:
        pr, style = self.pr, self.icon_style
        if self.details is None:
            return Text("Reading the checks…", style="dim")
        checks = sorted(self.details.get("checks") or [], key=check_order)
        if not checks:
            return Text("No checks on the last commit.", style="dim")
        counts = {}
        for check in checks:
            counts[check["state"]] = counts.get(check["state"], 0) + 1
        summary = ", ".join(f"{counts[state]} {state}" for state in ("failed", "pending", "passed", "skipped")
                            if state in counts)
        text = Text()
        text.append_text(pr_label(f"ci:{pr.ci}", summary, style) if pr.ci else Text(summary))
        for check in checks:
            text.append("\n")
            text.append_text(pr_label(f"ci:{check['state']}", check["name"], style))
        return text

    def _issues_text(self) -> Text:
        if self.details is None:
            return Text("Reading the linked issues…", style="dim")
        issues = self.details.get("issues") or []
        if not issues:
            return Text("This pull request closes no issues.", style="dim")
        text = Text()
        for index, issue in enumerate(issues):
            if index:
                text.append("\n")
            text.append_text(self._line("issue", Text(f"#{issue['number']} ", style="bold")))
            text.append(issue.get("title") or "")
            state = (issue.get("state") or "").lower()
            if state:
                text.append(f"  {state}", style="green" if state == "open" else "dim")
        return text

    def compose(self) -> ComposeResult:
        pr, style = self.pr, self.icon_style
        state = "draft" if pr.draft else "open"
        title = Text()
        title.append(f"{pr_icon(state, style)} ", style=PR_STATES[state][2])
        title.append(f"#{pr.number} ", style="bold dim")
        title.append(pr.title, style="bold")
        meta = Text()
        author_icon = PR_STATES["author"][0] if style == "nerd" else ""
        meta.append(f"{author_icon} " if author_icon else "by ")
        meta.append(pr.author, style="bold cyan" if pr.mine else "bold")
        branch_icon = PR_STATES["branch"][0] if style == "nerd" else ""
        meta.append(f"    {branch_icon} " if branch_icon else "    ")
        meta.append(pr.head_branch)
        if pr.base_branch:
            meta.append(" → ", style="dim")
            meta.append(pr.base_branch)
        meta.append("\n")
        meta.append_text(pr_size_cell(pr))
        meta.append(f"  in {pr.files} file{'' if pr.files == 1 else 's'}")
        if pr.updated:
            meta.append(f" · updated {pr.updated[:16].replace('T', ' ')}", style="dim")
        if pr.draft:
            meta.append("  ")
            meta.append_text(pr_label("draft", "draft", style))
        if pr.labels:
            meta.append("\n")
            meta.append_text(Text(" ").join(label_chip(label) for label in pr.labels))
        dialog = VerticalScroll(classes="dialog detail pr-detail")
        # In the border, so the keys stay in view when the description scrolls.
        dialog.border_subtitle = "R review · g go · u new commits · f finish · o browser · esc close"
        with dialog:
            yield self._static(title, id="pr-detail-title")
            yield self._static(meta, id="pr-detail-meta")
            blocks = [("pr-detail-checks", "CI checks", self._checks_text()),
                      ("pr-detail-issues", "Closes issues", self._issues_text()),
                      ("pr-detail-github", "GitHub review", self._github_text()),
                      ("pr-detail-agent", "Agent review (local)", self._agent_text())]
            for widget_id, name, text in blocks:
                block = self._static(text, id=widget_id, classes="detail-block")
                block.border_title = name
                yield block
            body = Markdown("*Reading the description…*" if self.load_details else pr.body.strip()
                            or "*No description.*", id="pr-detail-body", classes="detail-block")
            body.border_title = "Description"
            yield body

    def on_mount(self) -> None:
        if self.load_details is None:
            return
        load = self.load_details

        def read() -> None:
            try:
                details, error = load(), None
            except HerdboardError as failure:
                details, error = None, failure
            try:
                self.app.call_from_thread(self._show_details, details, error)
            except RuntimeError:
                pass  # The board quit.

        self.run_worker(read, thread=True, group=PR_GROUP, exit_on_error=False)

    def _show_details(self, details: dict | None, error: HerdboardError | None) -> None:
        if not self.is_attached:
            return  # The screen closed meanwhile.
        if error is not None:
            message = Text(f"Cannot read GitHub: {error}", style="red")
            for widget_id in ("#pr-detail-checks", "#pr-detail-issues", "#pr-detail-github"):
                self._set(widget_id, message)
            self.query_one("#pr-detail-body", Markdown).update(f"*Cannot read the description: {error}*")
            return
        self.details = details
        self.pr.body = details.get("body") or ""
        self._set("#pr-detail-checks", self._checks_text())
        self._set("#pr-detail-issues", self._issues_text())
        self._set("#pr-detail-github", self._github_text())
        self.query_one("#pr-detail-body", Markdown).update(self.pr.body.strip() or "*No description.*")

    def text(self) -> str:
        """The screen's text, with the block names, for tests."""
        parts = []
        for widget in self.query(Static):
            if getattr(widget, "border_title", None):
                parts.append(str(widget.border_title))
            if hasattr(widget, "plain"):
                parts.append(widget.plain)
        return "\n".join(parts + [self.pr.body])

    def key_applies(self, action: str) -> bool:
        """False for a key that has nothing to act on: the key bar dims it."""
        return pr_key_applies(self.pr, action.removeprefix("act('").removesuffix("')"))

    def action_act(self, action: str) -> None:
        self.dismiss(action)


class PullRequestsScreen(Screen):
    """The open pull requests of the board's repository. An agent reviews one in its own workspace."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("G", "next_tab", "Notes", show=False),
        Binding("enter", "detail", "Details"),
        Binding("slash", "search", "Search"),
        Binding("n", "page(1)", "Next"),
        Binding("p", "page(-1)", "Prev"),
        Binding("R", "review", "Review"),
        Binding("g", "go", "Go"),
        Binding("u", "refresh", "Update"),
        Binding("f", "finish", "Finish"),
        Binding("o", "open", "Browser"),
        Binding("r", "load", "Reload"),
        Binding("down,j", "cursor(1)", "Next", show=False),
        Binding("up,k", "cursor(-1)", "Previous", show=False),
        Binding("question_mark", "help", "Help"),
    ]

    def __init__(self, service):
        super().__init__()
        self.service = service
        self.repo = ""
        self.prs: list[pr_reviews.PullRequest] = []
        # One review, refresh or finish runs at a time.
        self.busy = False
        # The GitHub search text, and the cursor that starts each page seen so far: page 1 starts at None.
        self.search = ""
        self.cursors: list[str | None] = [None]
        self.page_index = 0
        self.page: pr_reviews.Page | None = None
        # True while a read of GitHub runs. Not `loading`: that Textual property covers the screen
        # with a spinner, and the reads run in the background.
        self.reading = False
        # When the list last read GitHub, as time.monotonic(); 0 before the first read.
        self.read_at = 0.0
        # The cells of the rows on screen, to see whether a read changed anything.
        self.shown_rows: list[list[Text]] = []

    def compose(self) -> ComposeResult:
        yield HeaderBar("Pull requests", self.app.view)
        yield TabBar("prs")
        repo = Static("", id="pr-repo")
        repo.display = False
        yield repo
        status = Static("Reading GitHub…", id="pr-status")
        yield status
        yield PullRequestList(id="pr-list")
        search = Input(placeholder="GitHub search: words, author:NAME, draft:true, review-requested:@me · "
                                   "enter searches · escape cancels", id="pr-search")
        search.display = False
        with Vertical(classes="bottom-dock"):
            yield search
            yield KeyBar()

    def on_mount(self) -> None:
        self.action_load()
        self.timer = self.set_interval(PR_SECONDS, self.action_load)
        self.query_one("#pr-list", PullRequestList).focus()

    def on_screen_suspend(self) -> None:
        # The board shows: no GitHub reads in the background.
        self.timer.pause()

    def on_screen_resume(self) -> None:
        self.timer.resume()
        if not self.reading and time.monotonic() - self.read_at >= PR_SECONDS:
            self.action_load()

    @property
    def icon_style(self) -> str:
        return self.app.store.config.icon_style

    def rows(self) -> list[tuple[str, ...]]:
        """Each row's parts (`PR_FIELDS`) as plain text, for tests."""
        return [tuple(field.plain for field in row.fields) for row in self.query(PullRequestRow)]

    def current(self) -> pr_reviews.PullRequest | None:
        index = self.query_one("#pr-list", PullRequestList).cursor_row
        return self.prs[index] if self.prs and 0 <= index < len(self.prs) else None

    def key_applies(self, action: str) -> bool:
        """False for a key that has nothing to act on: the key bar dims it."""
        if action == "page(1)":
            return self.page is not None and self.page.next_cursor is not None
        if action == "page(-1)":
            return self.page_index > 0
        return pr_key_applies(self.current(), action)

    def status_text(self) -> str:
        return self.query_one("#pr-status", Static).plain

    def _status(self, text: str | Text) -> None:
        status = self.query_one("#pr-status", Static)
        status.plain = text if isinstance(text, str) else text.plain
        status.update(text)

    def _summary(self, repo: str, prs: list[pr_reviews.PullRequest]) -> Text:
        """Where the list is (the total, the page, the search), then what the shown PRs need."""
        if not prs:
            if self.search:
                return Text(f"No open pull requests in {repo} match {self.search!r}. Escape clears the search.",
                            style="dim")
            return Text(f"No open pull requests in {repo}", style="dim")
        style = self.icon_style
        page = self.page
        total = page.total if page is not None else len(prs)
        paged = page is not None and (page.total > len(prs) or self.page_index > 0)
        text = Text(f"{total} open" + (" that match" if self.search else ""), style="bold")
        if paged:
            pages = max(1, -(-page.total // pr_reviews.PAGE_SIZE))
            text.append(f" · page {self.page_index + 1} of {pages}", style="bold")
        if self.search:
            text.append(f" · search: {self.search}", style="bold")
        # What needs the user, by where it comes from: GitHub, its checks, and the local agent.
        groups = [("GitHub", [("gh:requested", sum(pr.requested for pr in prs), "your review"),
                              ("gh:changes requested", sum(pr.decision == "changes requested" for pr in prs),
                               "changes requested")]),
                  ("CI", [("ci:failed", sum(pr.ci == "failed" for pr in prs), "failed")]),
                  ("agent", [("agent:reviewing", sum(pr.status == "reviewing" for pr in prs), "reviewing"),
                             ("agent:reviewed", sum(pr.status == "reviewed" for pr in prs), "reviewed"),
                             ("agent:failed", sum(pr.status == "failed" for pr in prs), "failed"),
                             ("agent:new commits", sum(pr.new_commits for pr in prs), "new commits")])]
        shown = [(name, [entry for entry in entries if entry[1]]) for name, entries in groups]
        shown = [(name, entries) for name, entries in shown if entries]
        if shown and paged:
            text.append(" │ on this page", style="dim")
        for name, entries in shown:
            text.append(f" │ {name}: ", style="dim")
            for index, (state, count, label) in enumerate(entries):
                if index:
                    text.append("  ")
                text.append_text(pr_label(state, f"{count} {label}", style))
        text.append(f" │ read at {datetime.now().strftime('%H:%M')}", style="dim")
        return text

    async def _show(self, repo: str, prs: list[pr_reviews.PullRequest], page: pr_reviews.Page | None = None,
                    searched: bool = False) -> None:
        """Show a page. A search page leaves the tab's open count as it is."""
        selected = self.current()
        self.repo, self.prs = repo, prs
        if page is not None:
            self.page = page
        self.query_one(HeaderBar).show(repo, self.app.view)
        if page is not None and not searched:
            self.app.pr_count = page.total
            for screen in self.app.screen_stack + [self]:
                for bar in screen.query(TabBar):
                    bar.show_counts()
        heading = self.query_one("#pr-repo", Static)
        heading.display = True
        heading.update(pr_label("github", f"github.com/{repo}", self.icon_style))
        listing = self.query_one("#pr-list", PullRequestList)
        rows = [pr_fields(pr, self.icon_style, agent_logos=self.app.agent_logos) for pr in prs]
        # A read that changed nothing leaves the rows alone, so the cursor and the scroll stay.
        if rows != self.shown_rows:
            self.shown_rows = rows
            await listing.show(prs, rows)
            numbers = [pr.number for pr in prs]
            listing.move_cursor(numbers.index(selected.number) if selected and selected.number in numbers else 0,
                                scroll=selected is None)
        self._status(self._summary(repo, prs))
        self.refresh_bindings()

    def _call(self, function, *args, **options) -> None:
        """Run a function on the event loop from a worker thread. Nothing runs once the board quits."""
        try:
            self.app.call_from_thread(function, *args, **options)
        except RuntimeError:
            pass

    def action_load(self) -> None:
        text, after = self.search, self.cursors[self.page_index]
        self.reading = True

        async def done(repo: str | None, page: pr_reviews.Page | None) -> None:
            self.reading = False
            if page is not None:
                self.read_at = time.monotonic()
                await self._show(repo, page.prs, page, searched=bool(text))

        def load() -> None:
            try:
                repo, page = self.service.page(text, after)
            except HerdboardError as error:
                self._call(done, None, None)
                self._call(self._status, f"Cannot read the pull requests: {error}")
                self._call(self.app.notify, str(error), severity="error")
                return
            self._call(done, repo, page)

        self.run_worker(load, thread=True, group=PR_GROUP, exit_on_error=False)

    def action_page(self, step: int) -> None:
        if self.reading or self.page is None:
            return
        if step > 0:
            if self.page.next_cursor is None:
                self.app.notify("This is the last page")
                return
            del self.cursors[self.page_index + 1:]
            self.cursors.append(self.page.next_cursor)
            self.page_index += 1
        else:
            if self.page_index == 0:
                self.app.notify("This is the first page")
                return
            self.page_index -= 1
        self._status(f"Reading page {self.page_index + 1}…")
        self.action_load()

    def _set_search(self, text: str) -> None:
        """Search again from page 1."""
        self.search = text
        self.cursors, self.page_index = [None], 0
        self._status("Searching GitHub…" if text else "Reading GitHub…")
        self.action_load()

    def action_search(self) -> None:
        search = self.query_one("#pr-search", Input)
        search.value = self.search
        search.display = True
        search.focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "pr-search":
            return
        event.stop()
        event.input.display = False
        self.query_one("#pr-list", PullRequestList).focus()
        self._set_search(event.value.strip())

    def action_back(self) -> None:
        """Escape: close the search box, else clear the search, else go back to the board."""
        search = self.query_one("#pr-search", Input)
        if search.display:
            search.display = False
            self.query_one("#pr-list", PullRequestList).focus()
        elif self.search:
            self._set_search("")
        else:
            self.action_close()

    def _run(self, work: Callable[[], dict], notice: Callable[[dict], str],
             then: Callable[[dict], None] | None = None,
             failed: Callable[[HerdboardError], bool] | None = None) -> None:
        """Run one PR action in a thread. `failed` handles an error itself when it returns True."""
        if self.busy:
            self.app.notify("Busy: a pull request action is in progress", severity="warning")
            return
        self.busy = True

        def done(result: dict | None, error: HerdboardError | None) -> None:
            self.busy = False
            if error is not None:
                if not (failed and failed(error)):
                    self.app.notify(str(error), severity="error")
            else:
                self.app.notify(notice(result))
                if then:
                    then(result)
            self.action_load()

        def guarded() -> None:
            try:
                result, error = work(), None
            except HerdboardError as error_:
                result, error = None, error_
            self._call(done, result, error)

        self.run_worker(guarded, thread=True, group=PR_GROUP, exit_on_error=False)

    def action_close(self) -> None:
        self.app.show_tab("board")

    def action_next_tab(self) -> None:
        self.app.show_tab("notes")

    def action_cursor(self, step: int) -> None:
        listing = self.query_one("#pr-list", PullRequestList)
        if self.prs:
            listing.move_cursor(listing.cursor_row + step)

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())

    def on_pull_request_list_selected(self, event: PullRequestList.Selected) -> None:
        event.stop()
        self.action_detail()

    def action_detail(self) -> None:
        pr = self.current()
        if pr is None:
            return

        def chosen(action: str | None) -> None:
            if action:
                getattr(self, f"action_{action}")()

        self.app.push_screen(PullRequestDetailScreen(pr, self.repo, self.icon_style,
                                                     load_details=lambda: self.service.details(pr.number)), chosen)

    def action_review(self) -> None:
        pr = self.current()
        if pr is None:
            return

        def chosen(kind: str | None) -> None:
            if kind == "claude":
                self.app.push_screen(ClaudeModelScreen(),
                                     lambda choice: choice and self._review(pr.number, kind, choice[0], None))
            elif kind == "pi":
                self.app.push_screen(ModelScreen(*pi_model_choices(self.app.store.config)),
                                     lambda choice: choice and self._review(pr.number, kind, *choice))

        self.app.push_screen(AgentScreen(f"Choose the reviewer of PR #{pr.number}"), chosen)

    def _review(self, number: int, kind: str, model: str | None, thinking: str | None) -> None:
        def notice(result: dict) -> str:
            model_text = f" ({result['model']})" if result.get("model") else ""
            verb = "has the new request" if result.get("prompted") else "reviews"
            return f"PR #{number}: {result['agent']}{model_text} {verb} in workspace {result['workspace_id']}"

        def new_commits(error: HerdboardError) -> bool:
            if getattr(error, "code", None) != "new_commits":
                return False
            self.app.push_screen(
                ConfirmScreen(f"PR #{number} has new commits since its worktree was made. Fetch them first?",
                              "Then herdboard offers the review again."),
                lambda answer: answer and self._refresh(number))
            return True

        self._run(lambda: self.service.review(number, kind, model, thinking), notice, failed=new_commits)

    def action_go(self) -> None:
        pr = self.current()
        if pr is not None:
            self._run(lambda: self.service.go(pr.number),
                      lambda result: f"Went to the review workspace {result['workspace_id']} of PR #{pr.number}")

    def action_refresh(self) -> None:
        pr = self.current()
        if pr is not None:
            self._refresh(pr.number)

    def _refresh(self, number: int) -> None:

        def notice(result: dict) -> str:
            text = f"PR #{number}: Hunk shows the new commits; {result['kept']} notes kept"
            if result["dropped"]:
                text += ". On no diff line any more: " + "; ".join(result["dropped"])
            return text

        def offer(result: dict) -> None:
            kind = result.get("agent")
            if kind not in ("claude", "pi"):
                return
            model = f" ({result['model']})" if result.get("model") else ""
            self.app.push_screen(
                ConfirmScreen(f"Start a new agent review of PR #{number}?", f"The reviewer: {kind}{model}."),
                lambda answer: answer and self._review(number, kind, result.get("model"), result.get("thinking")))

        self._run(lambda: self.service.refresh(number), notice, then=offer)

    def action_finish(self) -> None:
        pr = self.current()
        if pr is None:
            return
        number = pr.number

        def finish(force: bool) -> None:
            def unsent(error: HerdboardError) -> bool:
                if getattr(error, "code", None) != "unsent_notes":
                    return False
                self.app.push_screen(ConfirmScreen(f"Finish PR #{number} anyway?", f"{error} Their notes are lost."),
                                     lambda answer: answer and finish(True))
                return True

            self._run(lambda: self.service.finish(number, force),
                      lambda result: f"PR #{number}: review finished; worktree removed"
                                     + ("; branch deleted" if result.get("branch_deleted") else ""),
                      failed=None if force else unsent)

        self.app.push_screen(
            ConfirmScreen(f"Finish the review of PR #{number}?",
                          f"Closes its workspace, removes the worktree and deletes the branch pr/{number}."),
            lambda answer: answer and finish(False))

    def action_open(self) -> None:
        pr = self.current()
        if pr is None or not pr.url:
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            process = subprocess.Popen([opener, pr.url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.PIPE, start_new_session=True)
        except OSError as error:
            self.app.notify(f"Cannot open {pr.url} with {opener}: {error}", severity="error")
            return
        threading.Thread(target=watch_opener, args=(self.app, pr.url, opener, process), daemon=True).start()


class NoteChangedScreen(ModalBase):
    """The note changed on disk during an edit: o writes the edit over it, r reads it again and drops
    the edit, escape keeps editing."""

    BINDINGS = [
        Binding("o", "answer('overwrite')", "Overwrite"),
        Binding("r", "answer('reload')", "Reload"),
    ]

    def __init__(self, path: Path):
        super().__init__()
        self.path = path

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("The note changed on disk while you edited it.")
            yield Label(str(self.path), classes="dim")
            yield Label("o: overwrite the file with your edit · r: reload the file and drop your edit · "
                        "escape: keep editing", classes="dim")

    def action_answer(self, answer: str) -> None:
        self.dismiss(answer)


class NotesScreen(Screen):
    """The scratchpad of a board, or the global note: Markdown, shown as a preview and edited in place.

    An edit saves itself a moment after the last change, on escape, on a tab switch and when the
    board quits. The preview reads the file again when it changes on disk; the editor does not."""

    BINDINGS = [
        Binding("escape", "back", "Back"),
        Binding("escape", "done", "Done"),
        Binding("G", "next_tab", "Board", show=False),
        Binding("e", "edit", "Edit"),
        Binding("enter", "edit", "Edit", show=False),
        Binding("t", "toggle", "Global note"),
        Binding("r", "load", "Reload"),
        Binding("ctrl+s", "save", "Save"),
        Binding("down,j", "scroll(1)", "Down", show=False),
        Binding("up,k", "scroll(-1)", "Up", show=False),
        Binding("question_mark", "help", "Help"),
    ]
    # The keys of the preview. In the editor these keys type text.
    PREVIEW_ACTIONS = {"back", "next_tab", "edit", "toggle", "load", "scroll", "help"}
    EDIT_ACTIONS = {"done", "save"}
    EMPTY = "Empty note. e: edit"
    AUTOSAVE_SECONDS = 2.0
    # The preview has no focus, so its keys reach the screen; the hidden editor takes none.
    AUTO_FOCUS = ""

    def __init__(self, board: str = notes.GLOBAL):
        super().__init__()
        # The note on screen, and the board note that `t` goes back to from the global note.
        self.board = self.home = board
        self.path: Path | None = None
        # The note's text as read or last saved.
        self.saved = ""
        # The file's modification time as read, so a save sees an outside edit.
        self.stamp: int | None = None
        self.editing = False
        # True while the outside edit question shows.
        self.asking = False
        self.autosave_seconds = self.AUTOSAVE_SECONDS
        self.autosave = None

    def compose(self) -> ComposeResult:
        yield HeaderBar("Notes", self.app.view)
        yield TabBar("notes")
        title = Static("", id="notes-title")
        title.plain = ""
        yield title
        status = Static("", id="notes-status", classes="error")
        status.plain = ""
        status.display = False
        yield status
        with VerticalScroll(id="notes-preview", can_focus=False):
            empty = Static(Text(self.EMPTY), id="notes-empty", classes="dim")
            yield empty
            yield Markdown("", id="notes-markdown")
        editor = TextArea(id="notes-editor", soft_wrap=True)
        if "markdown" in editor.available_languages:
            editor.language = "markdown"
        editor.display = False
        yield editor
        with Vertical(classes="bottom-dock"):
            yield KeyBar()

    def on_mount(self) -> None:
        self.load()
        self.timer = self.set_interval(self.app.reload_interval, self.check_file)

    def on_screen_suspend(self) -> None:
        self.timer.pause()

    def on_screen_resume(self) -> None:
        self.timer.resume()
        self.check_file()

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        if action in self.PREVIEW_ACTIONS:
            return not self.editing
        if action in self.EDIT_ACTIONS:
            return self.editing
        return True

    def text(self) -> str:
        """The screen's text, for tests."""
        title = self.query_one("#notes-title", Static).plain
        return "\n".join([title, self.status_text(), self.saved or self.EMPTY])

    def status_text(self) -> str:
        return self.query_one("#notes-status", Static).plain

    def editor_text(self) -> str:
        return self.query_one("#notes-editor", TextArea).text

    def _status(self, message: str) -> None:
        status = self.query_one("#notes-status", Static)
        status.plain = message
        status.update(Text(message))
        status.display = bool(message)

    def _show(self) -> None:
        """Draw the title and the mode: the preview of the note as read, or the editor."""
        name = "Global note" if self.board == notes.GLOBAL else f"Note: {self.board}"
        title = Text(name, style="bold")
        title.append(f"  {self.path}", style="dim")
        widget = self.query_one("#notes-title", Static)
        widget.plain = title.plain
        widget.update(title)
        self.query_one(HeaderBar).show(name, self.app.view)
        self.query_one("#notes-preview").display = not self.editing
        self.query_one("#notes-editor").display = self.editing
        self.query_one("#notes-empty").display = not self.saved.strip()
        markdown = self.query_one("#notes-markdown", Markdown)
        markdown.display = bool(self.saved.strip())
        markdown.update(self.saved)
        self.refresh_bindings()

    def load(self) -> None:
        """Read the note from disk. An edit in progress is dropped."""
        self.editing = False
        self._stop_autosave()
        self.path = notes.note_path(self.app.store.config, self.board)
        try:
            self.saved, self.stamp = notes.read_note(self.path)
            self._status("")
        except HerdboardError as error:
            self.saved = ""
            self._status(str(error))
        self._show()

    def check_file(self) -> None:
        """In the preview, read the note again when it changed on disk."""
        if self.editing or self.path is None:
            return
        try:
            _text, stamp = notes.read_note(self.path)
        except HerdboardError:
            stamp = -1
        if stamp != self.stamp:
            self.load()

    def set_board(self, board: str, home: bool = True) -> bool:
        """Show the note of `board`. An edit saves first; when it cannot, the note stays."""
        if not self.save():
            return False
        if home:
            self.home = board
        self.board = board
        if self.is_mounted:
            self.load()
        return True

    def leave(self) -> bool:
        """Save the edit and go back to the preview, before another tab shows. False keeps the tab."""
        if not self.save():
            return False
        if self.editing:
            self.editing = False
            self._show()
        return True

    def _stop_autosave(self) -> None:
        if self.autosave is not None:
            self.autosave.stop()
            self.autosave = None

    def save(self, force: bool = False) -> bool:
        """Write the edit when it changed. False when it is not saved: the status line or a
        question says why, and the edit stays."""
        self._stop_autosave()
        if not self.editing:
            return True
        if self.asking:
            return False
        text = self.editor_text()
        if text == self.saved and not force:
            return True
        try:
            self.stamp = notes.write_note(self.path, text, self.stamp, force=force)
        except notes.NoteChanged:
            self._ask_changed()
            return False
        except HerdboardError as error:
            self._status(str(error))
            return False
        self.saved = text
        self._status("")
        return True

    def _ask_changed(self) -> None:
        self.asking = True

        def answered(answer: str | None) -> None:
            self.asking = False
            if answer == "overwrite":
                self.save(force=True)
            elif answer == "reload":
                self.load()
            if self.editing:
                self.query_one("#notes-editor", TextArea).focus()

        self.app.push_screen(NoteChangedScreen(self.path), answered)

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        if not self.editing:
            return
        self._stop_autosave()
        self.autosave = self.set_timer(self.autosave_seconds, self.save)

    def action_edit(self) -> None:
        editor = self.query_one("#notes-editor", TextArea)
        editor.load_text(self.saved)
        self.editing = True
        self._show()
        editor.focus()
        editor.move_cursor(editor.document.end)

    def action_done(self) -> None:
        if self.leave():
            self.set_focus(None)

    def action_save(self) -> None:
        self.save()

    def action_back(self) -> None:
        self.app.show_tab("board")

    def action_next_tab(self) -> None:
        self.app.show_tab("board")

    def action_toggle(self) -> None:
        target = notes.GLOBAL if self.board != notes.GLOBAL else self.home
        if target == self.board:
            self.app.notify("This board's note is the global note")
            return
        self.set_board(target, home=False)

    def action_load(self) -> None:
        self.load()

    def action_scroll(self, step: int) -> None:
        self.query_one("#notes-preview", VerticalScroll).scroll_relative(y=step, animate=False)

    def action_help(self) -> None:
        self.app.push_screen(HelpScreen())


# The column colors in turn, as (color, ANSI color), and the completed column's green.
COLUMN_PALETTE = [("#4ea8de", "ansi_blue"), ("#d670b8", "ansi_magenta"), ("#e3b341", "ansi_yellow"),
                  ("#3fb8c9", "ansi_cyan"), ("#7bc96f", "ansi_green")]
DONE_COLORS = ("$success", "ansi_green")


# The border of a pull request row by its CI state, as (color, ANSI color).
PR_ROW_COLORS = {"passed": ("$success", "ansi_green"), "failed": ("$error", "ansi_red"),
                 "pending": ("$warning", "ansi_yellow"), "skipped": ("$surface-lighten-2", "ansi_bright_black"),
                 "none": ("$surface-lighten-2", "ansi_bright_black")}


def pr_row_css() -> str:
    """Each CI state's color on the row border. The selected row has a heavy border."""
    rules = []
    for state, (color, ansi) in PR_ROW_COLORS.items():
        for prefix, value in (("", color), ("App:ansi ", ansi)):
            rules += [f"{prefix}.pr-row.ci-{state} {{ border: round {value}; }}",
                      f"{prefix}.pr-row.ci-{state}.selected {{ border: heavy {value}; }}"]
    # Without a CI color, the selected row's border takes the text color.
    rules += [".pr-row.ci-none.selected, .pr-row.ci-skipped.selected { border: heavy $foreground; }",
              "App:ansi .pr-row.ci-none.selected, App:ansi .pr-row.ci-skipped.selected "
              "{ border: heavy ansi_default; }"]
    return "\n".join(rules)


def column_css() -> str:
    """Each column color on the column border, its header and its cards. The current column has a
    heavy border and an underlined header."""
    rules = []
    named = [(f"col-{index}", colors) for index, colors in enumerate(COLUMN_PALETTE[:COLUMN_COLORS])]
    for name, (color, ansi) in named + [("col-done", DONE_COLORS)]:
        for prefix, value in (("", color), ("App:ansi ", ansi)):
            rules += [f"{prefix}.{name} {{ border: round {value}; }}",
                      f"{prefix}.{name} .column-header {{ color: {value}; }}",
                      f"{prefix}.{name} .card {{ border: round {value}; }}",
                      f"{prefix}.{name}.current {{ border: heavy {value}; }}",
                      f"{prefix}.{name}.current .column-header {{ text-style: bold underline; }}",
                      f"{prefix}.{name} .cards {{ scrollbar-color: {value}; scrollbar-color-hover: {value}; "
                      f"scrollbar-color-active: {value}; }}"]
    rules += ["App:ansi .col-archive .card { border: round ansi_bright_black; }",
              ".col-archive .column-header { color: $text-muted; }"]
    return "\n".join(rules)


class CellMouseDriver(LinuxDriver):
    """Textual's terminal driver without pixel mouse reports (mode 1016).

    Textual turns pixel reports on when the terminal supports in-band resize.
    Herdr reads mouse positions as cells, so pixel reports stop clicks on its
    own tabs and sidebar while the board is in view.

    It does not ask for in-band resize reports (mode 2048) either. After one,
    Textual's parser reads every mouse position as pixels, so Herdr's cell
    positions land near the top-left corner. Resizes then come by SIGWINCH.
    """

    def _enable_mouse_pixels(self) -> None:
        return

    def _query_in_band_window_resize(self) -> None:
        return


class HerdboardApp(App):
    TITLE = "herdboard"
    CSS = """
    /* The header row of the board and the pull requests: herdboard | board or repository, view, version. */
    #header-bar { height: 2; padding: 0 1; border-bottom: solid $surface-lighten-2; }
    #header-app { width: auto; color: $accent; text-style: bold; }
    #header-rule { width: auto; padding: 0 1; color: $text-muted; }
    #header-place { width: 1fr; height: 1; color: $text-muted; }
    #header-view { width: auto; padding-left: 2; color: $accent; }
    App:ansi #header-bar { border-bottom: solid ansi_bright_black; }
    App:ansi #header-app, App:ansi #header-view { color: ansi_magenta; }
    App:ansi #header-rule, App:ansi #header-place { color: ansi_default; }
    #columns { height: 1fr; overflow-x: auto; overflow-y: hidden; }
    #columns { scrollbar-size-horizontal: 1; }
    .column { width: 1fr; min-width: 24; height: 100%; border: round $surface-lighten-2; }
    .column-header { text-style: bold; padding: 0 1; }
    .cards { height: 1fr; scrollbar-size-vertical: 1; }
    #filter { border: round $primary; }
    .bottom-dock { dock: bottom; height: auto; }
    /* Each tab is a box with its icon and label. The current tab has an accent border and bold text. */
    #tab-bar { height: 3; background: $background; }
    #tab-bar .tab { width: auto; height: 3; padding: 0 1; margin-right: 1; background: transparent;
                    border: round $surface-lighten-2; }
    #tab-bar .tab-label { width: auto; height: 1; color: $text-muted; }
    #tab-bar .tab.active { border: round $accent; }
    #tab-bar .tab.active .tab-label { color: $accent; text-style: bold; }
    #key-bar { height: auto; padding: 0 1; color: $text-muted;
                link-style: none; link-color: $text-muted; link-background: transparent;
                link-style-hover: bold; link-color-hover: $text; link-background-hover: $primary 30%; }
    App:ansi #tab-bar .tab { border: round ansi_bright_black; }
    App:ansi #tab-bar .tab-label { color: ansi_default; }
    App:ansi #tab-bar .tab.active { border: round ansi_magenta; }
    App:ansi #tab-bar .tab.active .tab-label { color: ansi_magenta; text-style: bold; }
    .card { padding: 0 1; height: auto; border: round $surface-lighten-2; }
    .dialog { width: 70; max-width: 90%; height: auto; max-height: 80%; padding: 1 2;
              border: round $accent; background: $surface; }
    /* A Label is as wide as its text by default, so a long line runs past the dialog edge. */
    .dialog Label { width: 100%; }
    .help { width: 90; max-width: 95%; }
    .help-grid { layout: grid; grid-size: 2; grid-columns: 16 1fr; grid-rows: auto;
                 grid-gutter: 0 1; width: 100%; height: auto; }
    .help-key { width: 16; height: auto; }
    .help-what { height: auto; }
    ModalBase #key-bar { dock: bottom; background: $background; }
    .help-section { column-span: 2; height: auto; padding-top: 1; color: $accent; }
    .help-section:first-child { padding-top: 0; }
    .dim { color: $text-muted; }
    .error { color: $error; }
    ModalBase { align: center middle; }
    #card-body { height: 7; }
    .detail { width: 90; scrollbar-size-vertical: 1; }
    .detail-note MarkdownH1, .detail-note MarkdownH2, .detail-note MarkdownH3 {
        margin: 0; padding: 0; content-align: left middle; text-align: left;
    }
    #detail-title { margin-bottom: 1; }
    #detail-chips { margin-bottom: 1; }
    #detail-meta { margin-bottom: 1; }
    #attachment-title { margin-bottom: 1; }
    .detail-block { height: auto; border: round $surface-lighten-2; padding: 0 1; margin-bottom: 1; }
    .detail-note Markdown { margin: 0; padding: 0; }
    .card-form { max-height: 95%; }
    .card-form Input, .card-form TextArea { border: round $surface-lighten-2; }
    .card-form Input:focus, .card-form TextArea:focus { border: round $primary; }
    .form-row { height: auto; }
    #card-type { width: 1fr; border: round $surface-lighten-2; height: auto; }
    #card-type > SelectCurrent { border: none; }
    #card-type:focus-within { border: round $primary; }
    #card-tags { width: 2fr; }
    #card-due { width: 1fr; }
    .config Select { border: round $surface-lighten-2; height: auto; }
    .config Select > SelectCurrent { border: none; }
    .config Select:focus-within { border: round $primary; }
    /* A fixed height keeps the dialog still when the tabs switch; the panes scroll in a small terminal. */
    .config { height: 40; }
    .config TabbedContent, .config ContentSwitcher, .config TabPane, .config-pane { height: 1fr; }
    .config TabPane { padding: 0; }
    .config-pane, #config-links, #config-pins { scrollbar-size-vertical: 1; }
    #config-links, #config-pins { height: auto; max-height: 6; border: round $surface-lighten-2; }
    #config-links:focus, #config-pins:focus { border: round $primary; }
    #issue-search { border: round $surface-lighten-2; }
    #issue-search:focus { border: round $primary; }
    #issue-results { height: auto; max-height: 12; border: round $surface-lighten-2; }
    #issue-results:focus { border: round $primary; }
    #config-project { width: 2fr; }
    #notes-title { height: 1; padding: 0 1; }
    #notes-status { height: auto; padding: 0 1; }
    #notes-preview { height: 1fr; padding: 0 1; scrollbar-size-vertical: 1; }
    #notes-preview Markdown { margin: 0; padding: 0; }
    #notes-preview MarkdownH1, #notes-preview MarkdownH2, #notes-preview MarkdownH3 {
        margin: 0; padding: 0; content-align: left middle; text-align: left;
    }
    #notes-editor { height: 1fr; border: round $primary; }
    App:ansi #notes-editor { border: round ansi_magenta; }
    App:ansi #notes-title { color: ansi_default; }
    #pr-repo { height: 1; padding: 0 1; color: $accent; }
    #pr-status { height: 1; padding: 0 1; color: $text-muted; }
    #pr-list { height: 1fr; scrollbar-size-vertical: 1; }
    .pr-row { height: auto; padding: 0 1; }
    """ + pr_row_css() + """
    .pr-row.selected { background: $accent 20%; }
    App:ansi #pr-repo { color: ansi_magenta; }
    App:ansi #pr-status { color: ansi_default; }
    App:ansi .pr-row { color: ansi_bright_black; }
    App:ansi .pr-row.selected { background: ansi_default; color: ansi_default; text-style: bold; }
    #pr-search { border: round $primary; }
    .pr-detail { width: 100; }
    #pr-detail-title, #pr-detail-meta, #pr-detail-marks { margin-bottom: 1; }
    #pr-detail-body { height: auto; margin: 0; }
    #pr-detail-body MarkdownH1, #pr-detail-body MarkdownH2, #pr-detail-body MarkdownH3 {
        margin: 0; padding: 0; content-align: left middle; text-align: left; color: $text; text-style: bold;
    }
    #config-labels { width: 1fr; }
    App:ansi .column { border: round ansi_bright_black; }
    /* Cards outside the current column fade in the ANSI themes, so the eye goes to one column. */
    App:ansi .column .card { color: ansi_bright_black; border: round ansi_bright_black; }
    App:ansi .column.current .card { color: ansi_default; }
    """ + column_css() + """
    .column.archive { border: dashed $surface-lighten-2; }
    App:ansi .column.archive { border: dashed ansi_bright_black; }
    .column.archive.current { border: dashed $accent; }
    App:ansi .column.archive.current { border: dashed ansi_magenta; }
    /* A compact card has no border of its own. The ANSI themes paint cards in the terminal
       background, so there a border keeps them apart. */
    .column .card.compact { border: none; margin: 0 0 1 0; background: $panel; }
    App:ansi .column .card.compact { border: round ansi_bright_black; margin: 0; }
    .column .card.done { color: $text-muted; }
    /* Every column has its color, so the selected card has a heavy border in the text color. */
    .column .card.selected { border: heavy $foreground; background: $accent 20%; }
    .column .card.compact.selected { border: none; background: $accent; color: $text; }
    App:ansi .column .card.selected { background: ansi_default; border: heavy ansi_default; text-style: bold; }
    App:ansi .dialog { border: round $primary; }
    App:ansi .detail-block { border: round ansi_bright_black; }
    App:ansi .card-form Input, App:ansi .card-form TextArea { border: round ansi_bright_black; }
    App:ansi .card-form Input:focus, App:ansi .card-form TextArea:focus { border: round $primary; }
    /* The column under a dragged card. */
    .column.drop-target { border: heavy $accent; }
    App:ansi .column.drop-target { border: heavy ansi_yellow; }
    """
    BINDINGS = [
        Binding("left,h", "column(-1)", "Column", show=False),
        Binding("right,l", "column(1)", "Column", show=False),
        Binding("up,k", "card(-1)", "Card", show=False),
        Binding("down,j", "card(1)", "Card", show=False),
        Binding("shift+left,H", "move(-1)", "Move left"),
        Binding("shift+right,L", "move(1)", "Move right"),
        Binding("shift+up,K", "shift(-1)", "Up", show=False),
        Binding("shift+down,J", "shift(1)", "Down", show=False),
        Binding("slash", "filter", "Filter"),
        Binding("escape", "clear_filter", "Clear filter", show=False),
        Binding("enter", "detail", "Detail"),
        Binding("a", "add", "Add"),
        Binding("d", "done", "Done"),
        Binding("e", "edit", "Edit"),
        Binding("b", "move_board", "Board"),
        Binding("x", "delete", "Delete"),
        Binding("A", "archive", "Archive"),
        Binding("D", "archive_done", "Archive done", show=False),
        Binding("z", "toggle_archive", "Show archive", show=False),
        Binding("T", "promote", "Promote", show=False),
        Binding("s", "start", "Start"),
        Binding("S", "choose_agent", "Choose agent", show=False),
        Binding("g", "go", "Go to workspace"),
        Binding("R", "review", "Review"),
        Binding("P", "plan", "Plan"),
        Binding("p", "parent", "Parent", show=False),
        Binding("m", "merge", "Merge"),
        Binding("f", "finish", "Finish"),
        Binding("v", "cycle_view", "View"),
        Binding("t", "toggle_linear", "Linear", show=False),
        Binding("i", "init", "Init", show=False),
        Binding("c", "config", "Config", show=False),
        Binding("I", "import_issue", "Pin issue", show=False),
        Binding("u", "unpin", "Unpin", show=False),
        Binding("r", "reload", "Reload"),
        Binding("G", "pull_requests", "PRs"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def get_driver_class(self) -> type[Driver]:
        driver_class = super().get_driver_class()
        return CellMouseDriver if driver_class is LinuxDriver else driver_class

    def __init__(
        self,
        store: Store,
        view: str,
        paths_for: Callable[[str], list[Path]],
        repo_board: Path | None = None,
        reload_interval: float = RELOAD_SECONDS,
        linear_for: Callable[[str], LinearResult] | None = None,
        linear_active: bool = False,
        available: Callable[[str], bool] | None = None,
        linear_interval: float = LINEAR_SECONDS,
        linear_writer: LinearWriter | None = None,
        start_card: Callable[[str, str | None], dict] | None = None,
        review_card: Callable[[str], dict] | None = None,
        agent_review_card: Callable[..., dict] | None = None,
        plan_card: Callable[..., dict] | None = None,
        merge_card: Callable[[str], dict] | None = None,
        finish_card: Callable[[str], dict] | None = None,
        go_card: Callable[[str], dict] | None = None,
        directory: Path | None = None,
        init_board: Callable[[], Path] | None = None,
        config_file: Path | None = None,
        resolve_link: Callable[[str, list[str]], ProjectLink] | None = None,
        search_issues: Callable[[str], list[dict]] | None = None,
        pr_service=None,
        open_prs: bool = False,
        agent_logos: bool = False,
    ):
        super().__init__()
        theme = store.config.theme
        if theme not in self.available_themes:
            names = ", ".join(sorted(self.available_themes))
            raise HerdboardError(f"Unknown theme {theme!r} in the config. Choose one of: {names}")
        self.theme = theme
        self.store = store
        self.view = view
        self.paths_for = paths_for
        self.repo_board = repo_board
        self.init_board = init_board
        # Finds a Linear project and its labels by name for the config dialog; None without Linear.
        self.resolve_link = resolve_link
        # Searches Linear for the `I` dialog; None without a Linear connection.
        self.search_issues = search_issues
        self.hide_linear = False
        self.show_archive = False
        self.reload_interval = reload_interval
        self.linear_for = linear_for
        self.linear_active = linear_active and linear_for is not None
        self.available = available or (
            lambda view: view != "mine" and (view != "repo" or repo_board is not None)
        )
        self.linear_interval = linear_interval
        self.linear_writer = linear_writer
        self.start_card = start_card
        self.review_card = review_card
        self.agent_review_card = agent_review_card
        self.plan_card = plan_card
        self.merge_card = merge_card
        self.finish_card = finish_card
        self.go_card = go_card
        self.directory = directory or Path.cwd()
        # The pull request list's calls into `pr_reviews`; `open_prs` opens the board on that list.
        self.pr_service = pr_service or PullRequestService(self.directory)
        self.open_prs = open_prs
        # The agent logo font is installed, so agent names show their logos.
        self.agent_logos = agent_logos
        # The open pull request count of the last read, for the tab; None before the first read.
        self.pr_count: int | None = None
        # Passed on to the background workers, so they read the same boards.
        self.config_file = config_file
        self.starting = False
        # The last start or plan arguments, for the question that asks where a card without a
        # repository runs. A dict, so a target chosen later adds one option.
        self.target_request: dict = {}
        self.linear_results: dict[str, LinearResult] = {}
        self.linear_state = "loading" if self.linear_active else "off"
        # One Linear fetch runs at a time. A refresh asked meanwhile waits as one queued fetch.
        self.linear_fetching = False
        self.linear_queued = False
        self.linear_sequence = 0
        self.linear_applied: dict[str, int] = {}
        self.columns: list[ColumnData] = []
        # Columns as read, before the filter. `columns` is what the board shows.
        self.loaded: list[ColumnData] = []
        self.filter_text = ""
        self.paths: list[Path] = []
        self.col = 0
        self.row = 0
        self.signature: tuple = ()
        self.writing = False
        self.add_screen: AddScreen | None = None
        # True while a Linear write runs: Linear is fetched again when it ends.
        self.refetch = False
        # The card that the mouse holds, and the index of its column, from mouse down to mouse up.
        self.drag: tuple[CardView, int] | None = None
        # True after a drop, so the click that ends the drag does not count.
        self.dropped = False

    # Layout

    def compose(self) -> ComposeResult:
        yield HeaderBar(view=self.view)
        yield TabBar("board")
        yield HorizontalScroll(id="columns", can_focus=False)
        search = Input(placeholder="Filter: words, or #tag · enter keeps it · escape clears it", id="filter")
        search.display = False
        search.disabled = True
        # Docks on one edge overlap, so the filter and the keys share one bottom dock.
        with Vertical(classes="bottom-dock"):
            yield search
            yield KeyBar()

    async def on_mount(self) -> None:
        await self.reload()
        self.set_interval(self.reload_interval, self.check_files)
        if self.open_prs:
            self.action_pull_requests()
        if self.linear_active:
            self.fetch_linear()
            self.set_interval(self.linear_interval, self.fetch_linear)

    # Loading

    def _signature(self, paths: list[Path]) -> tuple:
        sig = []
        for path in paths:
            try:
                stat = path.stat()
                sig.append((str(path), stat.st_mtime_ns, stat.st_size))
            except OSError:
                sig.append((str(path), None, None))
        return tuple(sig)

    def _load(self, paths: list[Path]) -> list[ColumnData]:
        by_key: dict[str, ColumnData] = {}
        archive = ColumnData(self.store.config.archive_titles[0], archive=True)
        for path in paths:
            snapshot = self.store.snapshot(path)
            if snapshot.archive is not None:
                archive.cards.extend(snapshot.archive.cards)
            for column in snapshot.columns:
                data = by_key.setdefault(column.title.casefold(), ColumnData(column.title))
                if data.limit is None:
                    data.limit = column.limit
                data.completed = data.completed or column.completed
                data.cards.extend(column.cards)
        linear = None if self.hide_linear else self.linear_results.get(self.view)
        for card in linear.cards if linear else []:
            data = by_key.setdefault(card.column.casefold(), ColumnData(card.column))
            data.cards.append(card)
        mark_removed([card for data in by_key.values() for card in data.cards])
        mark_family([card for data in by_key.values() for card in data.cards])
        if len(paths) == 1 and not linear:
            ordered = list(by_key.values())
        else:
            wanted = [name.casefold() for name in self.store.config.columns]
            ordered = [by_key[key] for key in wanted if key in by_key]
            ordered += [data for key, data in by_key.items() if key not in wanted]
        # The archive column is last, apart from a column with the same name.
        if self.show_archive:
            ordered.append(archive)
        mark_planning([card for data in ordered for card in data.cards])
        attachments.mark(self.store.config, [card for data in ordered for card in data.cards])
        agent_states.mark([card for data in ordered for card in data.cards])
        return ordered

    def current_card(self) -> CardView | None:
        if not self.columns:
            return None
        cards = self.columns[self.col].cards
        return cards[self.row] if self.row < len(cards) else None

    @staticmethod
    def _identity(card: CardView) -> tuple:
        """A block ID names a card. Without one, board and identical lines do."""
        if not TEMP_REF.match(card.ref):
            return ("id", card.ref)
        return ("lines", card.path, card.checked, card.title, tuple(card.body))

    def _select(self, identity: tuple | None, column: str | None = None) -> None:
        """Select the card with `identity`.

        When it is gone, select the first card of `column`; without a column keep
        the position within range.
        """
        if identity is not None:
            for col, data in enumerate(self.columns):
                for row, card in enumerate(data.cards):
                    if self._identity(card) == identity:
                        self.col, self.row = col, row
                        return
            names = [data.name.casefold() for data in self.columns]
            if column is not None and column.casefold() in names:
                self.col, self.row = names.index(column.casefold()), 0
                return
        self.col = min(self.col, max(len(self.columns) - 1, 0))
        count = len(self.columns[self.col].cards) if self.columns else 0
        self.row = min(self.row, max(count - 1, 0))

    @property
    def base(self):
        """The board screen, also while a dialog is open."""
        return self.screen_stack[0]

    def _ensure_inbox(self) -> None:
        if self.view != "repo" and not self.store.inbox_path.exists():
            self.store.ensure_inbox()

    async def reload(self, select: str | None = None) -> None:
        """Read the view again. `select` names the card to select, else the current one."""
        if select is None:
            card = self.current_card()
            identity = self._identity(card) if card else None
            column = self.columns[self.col].name if card else None
        else:
            identity, column = ("id", select), None
        try:
            self._ensure_inbox()
            paths = self.paths_for(self.view)
            signature = self._signature(paths)
            columns = await asyncio.to_thread(self._load, paths)
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        self.paths, self.signature, self.loaded = paths, signature, columns
        self.columns = self._filtered(columns)
        self._select(identity, column)
        await self._rebuild()

    def _filtered(self, columns: list[ColumnData]) -> list[ColumnData]:
        """The columns with only the cards that match every filter term."""
        terms = self.filter_text.casefold().split()
        if not terms:
            return columns
        return [
            ColumnData(c.name, c.limit, c.completed, [card for card in c.cards if card_matches(card, terms)],
                       c.archive)
            for c in columns
        ]

    async def _apply_filter(self, text: str) -> None:
        card = self.current_card()
        identity = self._identity(card) if card else None
        column = self.columns[self.col].name if self.columns else None
        self.filter_text = text.strip()
        self.columns = self._filtered(self.loaded)
        self._select(identity, column)
        await self._rebuild()

    def action_filter(self) -> None:
        search = self.base.query_one("#filter", Input)
        search.disabled = False
        search.display = True
        search.focus()

    async def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "filter":
            await self._apply_filter(event.value)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "filter":
            event.input.display = False
            event.input.disabled = True
            self.set_focus(None)

    async def action_clear_filter(self) -> None:
        self._end_drag()
        search = self.base.query_one("#filter", Input)
        search.display = False
        search.disabled = True
        self.set_focus(None)
        if search.value or self.filter_text:
            search.value = ""
            await self._apply_filter("")

    def linear_label(self) -> str:
        """The Linear part of the title bar."""
        if not self.linear_active:
            return "Linear: off"
        if self.hide_linear:
            return "Linear: hidden"
        if self.linear_state == "auth":
            return "Linear: authentication failed"
        result = self.linear_results.get(self.view)
        if result is None:
            return "Linear: error" if self.linear_state == "error" else "Linear: loading"
        if result.fetched_at is None:
            return "Linear: no links"
        stamp = result.fetched_at.astimezone().strftime("%H:%M")
        return f"Linear: stale since {stamp}" if result.stale else f"Linear: {stamp}"

    async def _rebuild(self) -> None:
        names = ", ".join(self.store.board_name(path) for path in self.paths) or "no boards"
        place = f"{names} · {self.linear_label()}"
        if self.filter_text:
            shown = sum(len(c.cards) for c in self.columns)
            place += f' · filter: "{self.filter_text}" ({shown} of {sum(len(c.cards) for c in self.loaded)})'
        if self.show_archive:
            place += " · archive shown"
        if self.repo_board is None and self.init_board is not None:
            place += " · no board for this repository: press i to create it"
        self.base.query_one(HeaderBar).show(place, self.view)
        show_board = len(self.paths) > 1
        container = self.base.query_one("#columns", HorizontalScroll)
        existing = list(container.query(".column"))
        # Repainting every column on each change made the screen flash. Keep the
        # column widgets when the columns are the same, and replace only the cards
        # of columns whose cards changed, in one screen update.
        with self.batch_update():
            if [getattr(w, "column_name", None) for w in existing] == [c.name for c in self.columns]:
                for index, (widget, column) in enumerate(zip(existing, self.columns)):
                    await self._update_column(widget, index, column, show_board)
            else:
                await container.remove_children()
                await container.mount_all([self._column_widget(index, c, show_board)
                                           for index, c in enumerate(self.columns)])
            self._show_selection()

    def _card_widgets(self, column: ColumnData, show_board: bool) -> list[CardWidget]:
        config = self.store.config
        dark = self.current_theme.dark
        return [CardWidget(card, show_board, config.tag_style, config.icon_style, config.card_style,
                           column.completed, dark, self.agent_logos) for card in column.cards]

    def _column_widget(self, index: int, column: ColumnData, show_board: bool) -> Vertical:
        classes = ("column " + column_class(index, column) + (" completed" if column.completed else "")
                   + (" archive" if column.archive else ""))
        cards = VerticalScroll(*self._card_widgets(column, show_board), classes="cards", can_focus=False)
        widget = Vertical(ColumnHeader(column, self.store.config.start_column), cards, classes=classes)
        widget.column_name = column.name
        return widget

    async def _update_column(self, widget: Vertical, index: int, column: ColumnData, show_board: bool) -> None:
        widget.set_class(column.completed, "completed")
        widget.set_class(column.archive, "archive")
        for name in [name for name in widget.classes if name.startswith("col-")]:
            widget.remove_class(name)
        widget.add_class(column_class(index, column))
        widget.query_one(ColumnHeader).show(column, self.store.config.start_column)
        holder = widget.query_one(".cards", VerticalScroll)
        old = list(holder.query(CardWidget))
        new = self._card_widgets(column, show_board)
        if [w.key for w in old] == [w.key for w in new]:
            for widget_, card in zip(old, column.cards):
                widget_.card = card
            return
        await holder.remove_children()
        await holder.mount_all(new)

    def _show_selection(self) -> None:
        for index, widget in enumerate(self.base.query(".column")):
            widget.set_class(index == self.col, "current")
        for widget in self.base.query(".card"):
            widget.set_class(widget.card is self.current_card(), "selected")
        self.call_after_refresh(self._reveal_selection)
        # The selected card sets which keys the key bar dims.
        self.refresh_bindings()

    def _reveal_selection(self) -> None:
        """Scroll the selected column and card into view, after layout."""
        for widget in self.base.query(".card.selected"):
            widget.scroll_visible(animate=False)
        columns = list(self.base.query(".column"))
        if self.col < len(columns):
            columns[self.col].scroll_visible(animate=False)

    async def check_files(self) -> None:
        cards = [card for column in self.loaded for card in column.cards]

        def status():
            return [(card.worktree_removed, card.worktree, card.worktree_left, card.pr_state, card.agent_review,
                     card.pr_writing, card.planning, card.has_attachments, card.agent, card.planned, card.reviewed,
                     card.agent_status, card.planning_status, card.review_status, card.pr_write_status)
                    for card in cards]

        old = status()
        try:
            await asyncio.to_thread(merge_module.refresh_pr_states)
            await asyncio.to_thread(mark_removed, cards)
            await asyncio.to_thread(mark_planning, cards)
            await asyncio.to_thread(attachments.mark, self.store.config, cards)
            await asyncio.to_thread(agent_states.mark, cards)
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        if self._signature(self.paths_for_safe()) != self.signature or old != status():
            await self.reload()

    def paths_for_safe(self) -> list[Path]:
        try:
            return self.paths_for(self.view)
        except HerdboardError:
            return self.paths

    # Actions

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Modal screens handle their own keys: the board's own key actions stop
        # behind a dialog. Textual's actions, such as tab to the next field, stay.
        # The pull request list is a full screen with its own keys, so the board's stop there too.
        if isinstance(self.screen, (ModalScreen, PullRequestsScreen, NotesScreen)) and action in BOARD_ACTIONS:
            return False
        return True

    def key_applies(self, action: str) -> bool:
        """False for a card key that does not apply to the selected card, by the card menu's rules.
        The key bar dims it."""
        name = action.split("(")[0]
        if name not in CARD_KEY_ACTIONS:
            return True
        card = self.current_card()
        if card is None:
            return False
        if name in ("move", "shift"):
            return not card.archived and (name == "move" or card.source != "linear")
        return name in {menu_action for _label, menu_action in card_actions(card)}

    def action_column(self, step: int) -> None:
        if self.columns:
            self.col = max(0, min(self.col + step, len(self.columns) - 1))
            count = len(self.columns[self.col].cards)
            self.row = min(self.row, max(count - 1, 0))
            self._show_selection()

    def action_card(self, step: int) -> None:
        if self.columns:
            count = len(self.columns[self.col].cards)
            self.row = max(0, min(self.row + step, max(count - 1, 0)))
            self._show_selection()

    # Mouse

    def select_card(self, card: CardView) -> bool:
        """Select a card that the mouse points at. False when a dialog is open or the card is gone."""
        if isinstance(self.screen, ModalScreen):
            return False
        identity = self._identity(card)
        for col, data in enumerate(self.columns):
            for row, shown in enumerate(data.cards):
                if shown is card or self._identity(shown) == identity:
                    self.col, self.row = col, row
                    self._show_selection()
                    return True
        return False

    def select_column(self, index: int) -> None:
        if isinstance(self.screen, ModalScreen) or not 0 <= index < len(self.columns):
            return
        self.action_column(index - self.col)

    def open_card_menu(self, card: CardView) -> None:
        if not self.select_card(card):
            return

        async def chosen(action: str | None) -> None:
            if action is None:
                return
            # A reload while the menu was open can remove the card: then the action must not run on another one.
            if not self.select_card(card):
                self.notify("The card is no longer on the board.", severity="warning")
                return
            await self.run_action(action)

        title = split_title(card.title)[0] if card.source != "linear" else card.title
        # From the app's own message pump, so the choice still runs when a reload replaces the card widget.
        self.call_next(self.push_screen, CardMenuScreen(title or card.title, card_actions(card)), chosen)

    def _column_at(self, x: int, y: int) -> int | None:
        """The index of the board column at a screen position, or None."""
        try:
            widget, _ = self.get_widget_at(x, y)
        except NoWidget:
            return None
        columns = list(self.base.query(".column"))
        while widget is not None:
            if widget in columns:
                return columns.index(widget)
            widget = widget.parent
        return None

    def start_drag(self, card: CardView) -> None:
        self._end_drag()
        if self.select_card(card):
            self.drag = (card, self.col)

    def _end_drag(self) -> None:
        self.drag = None
        for widget in self.base.query(".column.drop-target"):
            widget.remove_class("drop-target")

    def on_mouse_down(self, event: events.MouseDown) -> None:
        # A new press: a click from now on is not the end of a drop.
        self.dropped = False

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self.drag is None:
            return
        target = self._column_at(event.screen_x, event.screen_y)
        for index, widget in enumerate(self.base.query(".column")):
            widget.set_class(index == target and target != self.drag[1], "drop-target")

    def on_mouse_up(self, event: events.MouseUp) -> None:
        drag = self.drag
        self._end_drag()
        if drag is None or event.button != 1 or isinstance(self.screen, ModalScreen):
            return
        card = drag[0]
        target = self._column_at(event.screen_x, event.screen_y)
        # The card can be gone after a reload: then nothing moves. Its column is read again,
        # because a reload during the drag can change the order of the columns.
        if target is None or not self.select_card(card) or target == self.col:
            return
        self.dropped = True
        self.action_move(target - self.col)

    def on_click(self, event: events.Click) -> None:
        """A click on a column outside its cards, such as on its header, selects the column."""
        if isinstance(self.screen, ModalScreen):
            return
        if self.dropped:
            self.dropped = False
            return
        index = self._column_at(event.screen_x, event.screen_y)
        if index is not None:
            self.select_column(index)

    def _start_write(
        self,
        change: Callable[[], CardView | None],
        add: AddScreen | None = None,
        refetch: bool = False,
        removed: bool = False,
    ) -> bool:
        """Run a store or Linear change in a thread worker, one at a time. False when busy."""
        if self.writing or self.starting:
            self.notify("Busy: a change is still being written", severity="warning")
            return False

        def guarded() -> tuple[CardView | None, HerdboardError | None]:
            try:
                card = change()
            except HerdboardError as error:
                return None, error
            if card is not None:
                # Still in the worker thread: the sidebar update may call Herdr.
                start_module.card_changed(card, removed=removed)
            return card, None

        self.writing = True
        self.add_screen = add
        self.refetch = refetch
        self.run_worker(guarded, thread=True, group=WRITE_GROUP, exclusive=True, exit_on_error=False)
        return True

    async def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Apply the result of a store change on the event loop."""
        worker = event.worker
        if worker.group == START_GROUP:
            if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
                return
            self.starting = False
            result, error = (None, None)
            if event.state == WorkerState.SUCCESS:
                result, error = worker.result
            elif event.state == WorkerState.ERROR:
                error = worker.error
            if error:
                if not self._ask_target(error):
                    self.notify(str(error), severity="error")
            elif result:
                self.notify(result.get("notice") or
                            f"Started {result['ref']} in {result['workspace_id']} ({result['worktree_path']})")
            await self.reload(result["ref"] if result else None)
            self.fetch_linear()
            return
        if worker.group == LINEAR_GROUP:
            await self._linear_done(event)
            return
        if worker.group != WRITE_GROUP or event.state not in (
            WorkerState.SUCCESS,
            WorkerState.ERROR,
            WorkerState.CANCELLED,
        ):
            return
        self.writing = False
        screen, self.add_screen = self.add_screen, None
        refetch, self.refetch = self.refetch, False
        card, error = None, None
        if event.state == WorkerState.SUCCESS:
            card, error = worker.result
        elif event.state == WorkerState.ERROR:
            error = worker.error
        if error is not None:
            self.notify(str(error), severity="error")
            if screen is not None and screen in self.screen_stack:
                screen.show_error(str(error))
        elif screen is not None and screen in self.screen_stack:
            screen.dismiss(None)
        await self.reload(card.ref if card else None)
        if refetch:
            self.fetch_linear()

    # Linear

    def fetch_linear(self) -> None:
        """Fetch the Linear cards of the current view in a thread, one fetch at a time."""
        if not self.linear_active:
            return
        if self.linear_fetching:
            self.linear_queued = True
            return
        self.linear_fetching = True
        self.linear_sequence += 1
        view, fetch, sequence = self.view, self.linear_for, self.linear_sequence

        def guarded() -> tuple[str, int, LinearResult | None, HerdboardError | None]:
            try:
                return view, sequence, fetch(view), None
            except HerdboardError as error:
                return view, sequence, None, error

        self.run_worker(guarded, thread=True, group=LINEAR_GROUP, exit_on_error=False)

    async def _linear_done(self, event: Worker.StateChanged) -> None:
        if event.state not in (WorkerState.SUCCESS, WorkerState.ERROR, WorkerState.CANCELLED):
            return
        self.linear_fetching = False
        try:
            await self._apply_linear(event)
        finally:
            if self.linear_queued:
                self.linear_queued = False
                self.fetch_linear()

    async def _apply_linear(self, event: Worker.StateChanged) -> None:
        if event.state == WorkerState.ERROR:
            self.notify(str(event.worker.error), severity="error")
            return
        if event.state != WorkerState.SUCCESS:
            return
        view, sequence, result, error = event.worker.result
        if view != self.view or sequence < self.linear_applied.get(view, 0):
            return
        self.linear_applied[view] = sequence
        if isinstance(error, LinearAuthError):
            # Cached cards would look current although the key no longer works.
            self.linear_state = "auth"
            self.linear_results.clear()
            self.notify(str(error), severity="error")
            await self.reload()
            return
        if error is not None:
            self.linear_state = "error"
            self.notify(str(error), severity="error")
            await self.reload()
            return
        self.linear_state = "ok"
        self.linear_results[view] = result
        for message in result.errors:
            self.notify(message, severity="warning")
        await self.reload()

    def ask_state(self, team_key: str, column: str, found: list[dict]) -> dict | None:
        """Ask in a dialog which state to use. Runs in the write worker; None when cancelled."""
        answered = threading.Event()
        answer: list[dict | None] = []

        def done(choice: dict | None) -> None:
            answer.append(choice)
            answered.set()

        self.call_from_thread(self.push_screen, StateScreen(team_key, column, found), done)
        while not answered.wait(0.2):
            if not self.is_running:
                return None
        return answer[0]

    def _linear_write(self, card: CardView, change: Callable[[LinearWriter], object]) -> None:
        """Write to Linear in the write worker, then fetch Linear again."""
        if self.linear_writer is None:
            self.notify(LINEAR_WRITER_MESSAGE, severity="warning")
            return
        if card.stale:
            self.notify(LINEAR_OFFLINE_MESSAGE, severity="warning")
            return
        writer = self.linear_writer

        def write() -> CardView:
            change(writer)
            return card

        self._start_write(write, refetch=True)

    def action_move(self, step: int) -> None:
        card = self.current_card()
        target = self.col + step
        if card is None or not 0 <= target < len(self.columns):
            return
        if card.archived:
            self.notify("Press A to move the card back to the done column.", severity="warning")
            return
        if self.columns[target].archive:
            self.action_archive()
            return
        name = self.columns[target].name
        if card.source == "linear":
            self._linear_write(card, lambda writer: writer.move(card.ref, name, None, self.ask_state))
            return
        self._start_write(lambda: self.store.move(card.ref, name))

    def action_shift(self, step: int) -> None:
        card = self.current_card()
        if card is None:
            return
        if card.source == "linear":
            self.notify("Change the order of Linear issues in Linear.", severity="warning")
            return
        ref = card.ref
        self._start_write(lambda: self.store.shift(ref, step))

    def action_done(self) -> None:
        card = self.current_card()
        if card is None:
            return
        if card.source == "linear":
            self._linear_write(card, lambda writer: writer.done(card.ref, self.ask_state))
            return
        self._start_write(lambda: self.store.done(card.ref))

    def action_edit(self) -> None:
        card = self.current_card()
        if card is None:
            return
        if card.source == "linear":
            self.notify("Edit Linear issues in Linear.", severity="warning")
            return
        self.push_screen(EditScreen(card, self.store.config.types))

    def submit_edit(self, screen: EditScreen, title: str, body: list[str]) -> None:
        """Save the dialog's text, if the card still matches what the dialog showed."""
        ref, before = screen.card.ref, screen.card
        self._start_write(lambda: self.store.edit(ref, title=title, body=body, expected=before), add=screen)

    def action_parent(self) -> None:
        card = self.current_card()
        if card is None:
            return
        if card.source == "linear":
            self.notify("A Linear issue's sub-issues are set in Linear.", severity="warning")
            return
        options = [other for other in self.store.list_cards([card.path])
                   if other.ref != card.ref and other.parent is None and not TEMP_REF.match(other.ref)]
        ref = card.ref

        def chosen(answer: tuple[str, str | None] | None) -> None:
            if answer is not None:
                self._start_write(lambda: self.store.set_parent(ref, answer[1]))

        self.push_screen(ParentScreen(card, options), chosen)

    def action_promote(self) -> None:
        card = self.current_card()
        if card is None:
            return
        children = [other for data in self.loaded for other in data.cards
                    if other.parent == card.ref and other.source != "linear"]
        refusal = family_refusal(card, children)
        if refusal is not None:
            self.notify(refusal, severity="warning")
            return
        if self.linear_writer is None:
            self.notify(LINEAR_WRITER_MESSAGE, severity="warning")
            return
        writer = self.linear_writer

        def load_teams() -> list[dict]:
            with writer.online() as client:
                return client.teams()

        def load_projects(team_id: str) -> list[dict]:
            with writer.online() as client:
                return client.projects_for_team(team_id)

        def chosen(choice: tuple[dict, dict | None] | None) -> None:
            if choice is not None:
                self.submit_promote(card.ref, choice)

        title = split_title(card.title)[0] or card.title
        self.push_screen(PromoteScreen(card.ref, title, load_teams, load_projects), chosen)

    def submit_promote(self, ref: str, choice: tuple[dict, dict | None]) -> None:
        """Promote the todo in the write worker with the form's team and project."""
        team, project = choice
        writer = self.linear_writer
        if writer is None:
            self.notify(LINEAR_WRITER_MESSAGE, severity="warning")
            return

        def target(client, _view) -> tuple[dict | None, list[str]]:
            if project is None:
                return None, []
            found = client.project_for_write(project["id"])
            if found is None or team["id"] not in {t["id"] for t in found["teams"]}:
                raise HerdboardError(f"Linear project {project['name']} is not a project of team "
                                     f"{team['key']}. Press T and choose the project again")
            return found, []

        def change() -> CardView | None:
            # The card can change while the form is open: check it again, with its start records.
            view = self.store.show(ref)
            children = self.store.children(view.ref)
            mark_removed([view, *children])
            refusal = family_refusal(view, children)
            if refusal is not None:
                raise HerdboardError(refusal)
            try:
                promotion = Promoter(self.store.config, self.store, writer).promote(
                    ref, team=team["key"], target=target, ask=self.ask_state,
                    # An empty name tells the service that "No project" was chosen.
                    project=project["name"] if project else "",
                    again="Press T on the todo again to finish",
                )
            except StateCancelledError:
                self.call_from_thread(self.notify, "Promotion cancelled: no Linear issue was created.",
                                      severity="warning")
                return None
            count = len(promotion.sub_issues)
            subs = f" with {count} sub-issue{'s' if count != 1 else ''}" if count else ""
            self.call_from_thread(self.notify, f"Promoted to {promotion.issue['identifier']}{subs}")
            for warning in promotion.warnings:
                # The service words its warnings for the command line's options.
                self.call_from_thread(self.notify, warning.removeprefix("--"), severity="warning")
            return promotion.todo

        self._start_write(change, refetch=True)

    def action_move_board(self) -> None:
        card = self.current_card()
        if card is None:
            return
        if card.source == "linear":
            self.notify("Linear issues stay in Linear; only todos change board.", severity="warning")
            return
        options = [(label, path) for label, path in self._board_options() if path != card.path]
        if not options:
            self.notify("There is no other board.", severity="warning")
            return
        ref, inbox = card.ref, self.store.inbox_path

        def move(path: Path | None) -> None:
            if path is None:
                return

            def write() -> CardView:
                if path == inbox:
                    self.store.ensure_inbox()
                return self.store.move_to_board(ref, path)

            self._start_write(write)

        self.push_screen(BoardChoiceScreen(card, options), move)

    def _board_options(self) -> list[tuple[str, Path]]:
        """Every board: this repository's first, then the inbox, then the others by name."""
        options = []
        if self.repo_board is not None:
            options.append((f"This repository ({self.store.board_name(self.repo_board)})", self.repo_board))
        options.append(("Inbox", self.store.inbox_path))
        known = {path for _, path in options}
        others = [path for path in self.store.board_paths() if path not in known]
        options += [(self.store.board_name(path), path) for path in sorted(others, key=self.store.board_name)]
        return options

    def action_delete(self) -> None:
        card = self.current_card()
        if card is None:
            return
        if card.source == "linear":
            self.notify("Delete Linear issues in Linear.", severity="warning")
            return
        ref = card.ref
        self.push_screen(
            DeleteScreen(card),
            lambda delete: self._start_write(lambda: self.store.delete(ref), removed=True) if delete else None,
        )

    def action_archive(self) -> None:
        """Archive the todo, or move an archived todo back to the done column."""
        card = self.current_card()
        if card is None:
            return
        if card.source == "linear":
            self.notify("herdboard archives only todos.", severity="warning")
            return
        ref = card.ref

        def archive() -> CardView:
            changed = self.store.archive(ref, blocked=start_module.archive_blocker)
            activity.append(self.store, changed.ref, "Archived.", activity.author())
            return changed

        def unarchive(parent: CardView | None = None) -> CardView:
            changed = self.store.unarchive(ref, with_parent=parent is not None)
            if parent is not None:
                activity.append(self.store, parent.ref, "Unarchived.", activity.author())
            activity.append(self.store, changed.ref, "Unarchived.", activity.author())
            return changed

        if not card.archived:
            self._start_write(archive)
            return
        try:
            parent = self.store.archived_parent(ref)
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        if parent is None:
            self._start_write(unarchive)
            return
        self.push_screen(
            ConfirmScreen(f"Unarchive {parent.ref}: {parent.title} too?",
                          f"The parent of {ref} is archived. Both move back to the done column."),
            lambda answer: self._start_write(lambda: unarchive(parent)) if answer else None,
        )

    def action_archive_done(self) -> None:
        """Archive every card in the done column of each board in the view, after a confirmation."""
        count = 0
        paths = []
        for path in self.paths:
            try:
                cards = self.store.done_cards(path)
            except HerdboardError:
                # A board without a done column has nothing to archive.
                continue
            count += len(cards)
            paths.append(path)
        if not count:
            self.notify("The done column has no cards.")
            return

        def write() -> None:
            kept = []
            total = 0
            for path in paths:
                archived, skipped = self.store.archive_done(path, blocked=start_module.archive_blocker)
                for card in archived:
                    activity.append(self.store, card.ref, "Archived.", activity.author())
                total += len(archived)
                kept += [f"{card.ref}: {reason}" for card, reason in skipped]
            notice = f"Archived {total} card{'' if total == 1 else 's'}."
            if kept:
                notice += " Kept " + "; ".join(kept)
            self.call_from_thread(self.notify, notice)

        names = ", ".join(self.store.board_name(path) for path in paths)
        self.push_screen(
            ConfirmScreen(f"Archive {count} card{'' if count == 1 else 's'} from the done column?",
                          f"Boards: {names}. Press z to show the archive."),
            lambda answer: self._start_write(write) if answer else None,
        )

    async def action_toggle_archive(self) -> None:
        self.show_archive = not self.show_archive
        await self.reload()

    def _start_card(self, ref: str, agent: str | None, model: str | None = None,
                    thinking: str | None = None, repo: Path | None = None, no_repo: bool = False,
                    new_repo: Path | None = None) -> None:
        """Start a card in a thread. `repo` is a board that names the card's repository."""
        if self.starting or self.writing:
            self.notify("Busy: a card change is in progress", severity="warning")
            return
        self.target_request = {"kind": "start", "ref": ref, "agent": agent, "model": model,
                               "thinking": thinking, "repo": repo, "no_repo": no_repo,
                               "new_repo": new_repo}

        def guarded():
            try:
                if self.start_card:
                    options = {"model": model, "thinking": thinking} if model or thinking else {}
                    if repo is not None:
                        options["repo"] = repo
                    if no_repo:
                        options["no_repo"] = True
                    if new_repo is not None:
                        options["new_repo"] = new_repo
                    return self.start_card(ref, agent, **options), None
                starter = Starter(self.store, self.directory, writer=self.linear_writer,
                                  ask_state=self.ask_state, repository_view=self.view == "repo",
                                  config_file=self.config_file)
                board = repo or (self.repo_board if self.view == "repo" else None)
                return starter.start(ref, agent=agent, repo=board, model=model, thinking=thinking,
                                     no_repo=no_repo, new_repo=new_repo), None
            except HerdboardError as error:
                return None, error

        self.starting = True
        self.run_worker(guarded, thread=True, group=START_GROUP, exclusive=True, exit_on_error=False)

    def _ask_target(self, error: HerdboardError) -> bool:
        """After a start or a plan failed for want of a repository, ask where the card runs.

        The action then runs again with the chosen repository, a new repository, or no repository.
        False when there is nothing to ask: another error, or a choice was made already.
        """
        request = self.target_request
        if getattr(error, "code", None) != "repo_required" or not request:
            return False
        if request["repo"] is not None or request["no_repo"] or request["new_repo"] is not None:
            return False
        ref = request["ref"]
        linear = bool(LINEAR_REF.match(ref))
        try:
            card = None if linear else self.store.show(ref)
            boards = [path for path in self.store.board_paths() if self.store.board_meta(path).repo]
        except HerdboardError:
            return False
        options = [(self.store.board_name(path), path) for path in sorted(boards, key=self.store.board_name)]
        default = None
        if card is not None:
            default = Path(self.store.config.projects_dir) / start_module.repo_slug(card)

        def chosen(answer) -> None:
            if answer is None:
                return
            mode, value = answer
            arguments = {"agent": request["agent"], "model": request["model"],
                         "thinking": request["thinking"]}
            if mode == "repo":
                arguments["repo"] = value
            elif mode == "new_repo":
                arguments["new_repo"] = value
            else:
                arguments["no_repo"] = True
            if request["kind"] == "plan":
                self._plan(ref, **arguments)
            else:
                self._start_card(ref, **arguments)

        self.push_screen(TargetScreen(ref, options, default, linear=linear), chosen)
        return True

    def _card_action(self, run: Callable[[], dict], notice: Callable[[dict], str]) -> None:
        """Run a review, merge or finish in a thread worker, like a start: one at a time."""
        if self.starting or self.writing:
            self.notify("Busy: a card change is in progress", severity="warning")
            return

        def guarded():
            try:
                result = run()
            except HerdboardError as error:
                return None, error
            return {**result, "notice": notice(result)}, None

        self.starting = True
        self.run_worker(guarded, thread=True, group=START_GROUP, exclusive=True, exit_on_error=False)

    def action_review(self) -> None:
        card = self.current_card()
        if card is None:
            return
        ref = card.ref
        if card.repoless:
            # A repoless card has no branch for Hunk, so only an agent review applies.
            self.push_screen(AgentScreen("Choose the reviewer"),
                             lambda reviewer: self._chosen_reviewer(ref, reviewer, False))
            return

        def chosen(kind: str | None) -> None:
            if kind == "human":
                self._human_review(ref)
            elif kind is not None:
                self.push_screen(AgentScreen("Choose the reviewer"),
                                 lambda reviewer: self._chosen_reviewer(ref, reviewer, kind == "both"))

        self.push_screen(ReviewKindScreen(), chosen)

    def _human_review(self, ref: str) -> None:
        run = (lambda: self.review_card(ref)) if self.review_card else (
            lambda: Reviewer(self.store, self.directory, writer=self.linear_writer,
                             config_file=self.config_file).review(ref))
        self._card_action(run, lambda result: f"Review of {result['ref']} opened in {result['workspace_id']}")

    def _chosen_reviewer(self, ref: str, reviewer: str | None, then_hunk: bool) -> None:
        if reviewer is None:
            return
        if reviewer == "claude":
            self.push_screen(ClaudeModelScreen(), lambda choice: choice and
                             self._agent_review(ref, reviewer, choice[0], None, then_hunk))
            return

        def chosen(choice: tuple[str | None, str | None] | None) -> None:
            if choice is not None:
                self._agent_review(ref, reviewer, *choice, then_hunk)

        self.push_screen(ModelScreen(*pi_model_choices(self.store.config)), chosen)

    def _agent_review(self, ref: str, reviewer: str, model: str | None, thinking: str | None,
                      then_hunk: bool) -> None:
        options = {"reviewer": reviewer, "model": model, "thinking": thinking, "then_hunk": then_hunk}
        run = (lambda: self.agent_review_card(ref, **options)) if self.agent_review_card else (
            lambda: self._agent_reviewer().request(ref, **options))

        def notice(result: dict) -> str:
            model_text = f" ({result['model']})" if result["model"] else ""
            return f"Agent review of {result['ref']} by {result['reviewer']}{model_text} started in {result['pane_id']}"

        self._card_action(run, notice)

    def action_plan(self) -> None:
        card = self.current_card()
        if card is None:
            return
        ref = card.ref

        def chosen_agent(agent: str | None) -> None:
            if agent == "claude":
                self.push_screen(ClaudeModelScreen(), lambda choice: choice and self._plan(ref, agent, choice[0], None))
            elif agent == "pi":
                self.push_screen(ModelScreen(*pi_model_choices(self.store.config)),
                                 lambda choice: choice and self._plan(ref, agent, *choice))

        self.push_screen(AgentScreen("Choose the planner"), chosen_agent)

    def _plan(self, ref: str, agent: str, model: str | None, thinking: str | None, repo: Path | None = None,
              no_repo: bool = False, new_repo: Path | None = None) -> None:
        options = {"agent": agent, "model": model, "thinking": thinking}
        self.target_request = {"kind": "plan", "ref": ref, "agent": agent, "model": model,
                               "thinking": thinking, "repo": repo, "no_repo": no_repo,
                               "new_repo": new_repo}
        if self.plan_card:
            for name, value in (("repo", repo), ("no_repo", no_repo), ("new_repo", new_repo)):
                if value:
                    options[name] = value

        def run() -> dict:
            if self.plan_card:
                return self.plan_card(ref, **options)
            board = repo or (self.repo_board if self.view == "repo" else None)
            return self._planner().plan(ref, repo=board, no_repo=no_repo, new_repo=new_repo, **options)

        def notice(result: dict) -> str:
            model_text = f" ({result['model']})" if result["model"] else ""
            where = ("its work folder" if result.get("mode") in ("repoless", "new_repo")
                     else "a tab in its worktree")
            return f"{result['agent']}{model_text} plans {result['ref']} in {where}"

        self._card_action(run, notice)

    def _agent_reviewer(self) -> AgentReviewer:
        return AgentReviewer(self.store, self.directory, writer=self.linear_writer, config_file=self.config_file)

    def _planner(self) -> Planner:
        return Planner(self.store, self.directory, writer=self.linear_writer, repository_view=self.view == "repo",
                       config_file=self.config_file)

    def _merger(self) -> Merger:
        return Merger(self.store, self.directory, writer=self.linear_writer, ask_state=self.ask_state,
                      config_file=self.config_file)

    def action_merge(self) -> None:
        card = self.current_card()
        if card is None:
            return
        ref = card.ref
        pr_open = card.pr_state == "open"

        def run_merge(**options) -> dict:
            return self.merge_card(ref, **options) if self.merge_card else self._merger().merge(ref, **options)

        def notice(result: dict) -> str:
            if result["mode"] == "pr" and result["pr_action"] == "pushed":
                return f"Pushed {result['ref']} to its pull request {result['pr_url']}"
            if result["mode"] == "pr":
                writer = result["writer"][0].upper() + result["writer"][1:]
                return f"{writer} writes the pull request description of {result['ref']}"
            return f"Merged {result['ref']} into {result['base']} at {result['commit'][:7]}, then finished it"

        if card.repoless:
            # There is no branch to merge: the merge refuses, and the board shows why.
            self._card_action(run_merge, notice)
            return

        def rewrite(answer: bool) -> None:
            self._card_action(lambda: run_merge(rewrite=bool(answer)), notice)

        def confirmed(answer: bool) -> None:
            if not answer:
                return
            if not pr_open:
                self._card_action(run_merge, notice)
                return
            self.push_screen(
                ConfirmScreen("A pull request is open. Rewrite its description?",
                              "Yes: an agent writes it again. This replaces any edits made on GitHub. "
                              "No: only push the branch."),
                rewrite,
            )

        self.push_screen(
            ConfirmScreen(f"Merge {ref}: {card.title} into its base?",
                          "Local: rebase onto the base, move the base forward, remove the worktree. "
                          "PR: an agent writes the description, then a draft pull request opens. "
                          "The board or config sets the mode."),
            confirmed,
        )

    def action_finish(self) -> None:
        card = self.current_card()
        if card is None:
            return
        ref = card.ref
        run = (lambda: self.finish_card(ref)) if self.finish_card else (lambda: self._merger().finish(ref))

        def notice(result: dict) -> str:
            if result.get("mode") in ("repoless", "new_repo"):
                return f"Finished {result['ref']}; card folder removed"
            return f"Finished {result['ref']}; branch {'deleted' if result['branch_deleted'] else 'kept'}"

        detail = ("The card folder is deleted, with everything in it, and the card moves to Done."
                  if card.repoless else
                  "The worktree and the merged branch are removed, and the card moves to Done.")
        self.push_screen(ConfirmScreen(f"Finish {ref}: {card.title}?", detail),
                         lambda answer: self._card_action(run, notice) if answer else None)

    def action_go(self) -> None:
        card = self.current_card()
        if card is None:
            return
        ref = card.ref
        run = (lambda: self.go_card(ref)) if self.go_card else (lambda: start_module.go_to_workspace(ref))
        self._card_action(run, lambda result: f"Went to the workspace {result['workspace_id']} of {result['ref']}")

    def action_start(self) -> None:
        card = self.current_card()
        if card:
            self._start_card(card.ref, None)

    def action_choose_agent(self) -> None:
        card = self.current_card()
        if card:
            ref = card.ref
            self.push_screen(AgentScreen(), lambda agent: self._chosen_agent(ref, agent))

    def _chosen_agent(self, ref: str, agent: str | None) -> None:
        if agent is None:
            return
        if agent != "pi":
            self._start_card(ref, agent)
            return

        def chosen(choice: tuple[str | None, str | None] | None) -> None:
            if choice is not None:
                self._start_card(ref, "pi", *choice)

        self.push_screen(ModelScreen(*pi_model_choices(self.store.config)), chosen)

    def action_detail(self) -> None:
        card = self.current_card()
        if card is None:
            return
        notes = None
        if card.source != "linear":
            try:
                notes = self.store.notes_for(card)
            except HerdboardError as error:
                self.notify(str(error), severity="error")
        log = None
        files = None
        if activity.LOG_REF.match(card.ref):
            try:
                log = activity.read(self.store.config, card.ref, DETAIL_LOG_ENTRIES)
            except HerdboardError as error:
                self.notify(str(error), severity="error")
            try:
                files = attachments.listing(self.store.config, card.ref)
            except HerdboardError as error:
                self.notify(str(error), severity="error")
        self.push_screen(
            DetailScreen(card, notes, self.store.config.tag_style, log, files, self.store.config.icon_style),
            lambda result: self.action_edit() if result == "edit" else None,
        )

    def _add_path(self) -> Path:
        if self.view == "repo" and self.repo_board is not None:
            return self.repo_board
        if self.view == "all":
            card = self.current_card()
            if card is not None and card.path is not None:
                return card.path
        return self.store.inbox_path

    def action_add(self) -> None:
        if not self.columns:
            self.notify("The view has no columns", severity="error")
            return
        if self.columns[self.col].archive:
            self.notify("Cards cannot be added to the archive. Add the card to a column, then archive it.",
                        severity="warning")
            return
        path = self._add_path()
        column = self.columns[self.col].name
        self.push_screen(AddScreen(self._add_options(path), path, column, self.store.config.types))

    def _add_options(self, path: Path) -> list[tuple[str, Path]]:
        """Boards the add dialog offers: this repository's board, the inbox, and `path`."""
        options = []
        if self.repo_board is not None:
            options.append((f"This repository ({self.store.board_name(self.repo_board)})", self.repo_board))
        options.append(("Inbox", self.store.inbox_path))
        if all(option != path for _, option in options):
            options.append((self.store.board_name(path), path))
        return options

    def submit_add(self, screen: AddScreen, text: str, body: list[str]) -> None:
        """Add the card to the board the dialog shows and the column captured when it opened."""
        path, column, inbox = screen.path, screen.column, self.store.inbox_path

        def add() -> CardView:
            if path == inbox:
                self.store.ensure_inbox()
            return self.store.add(path, text, column, body=body)

        self._start_write(add, add=screen)

    async def action_cycle_view(self) -> None:
        views = [v for v in VIEWS if self.available(v) and (v != "mine" or self.linear_active)]
        if self.view in views:
            self.view = views[(views.index(self.view) + 1) % len(views)]
        else:
            self.view = views[0]
        self.col = self.row = 0
        self.linear_state = "loading"
        await self.reload(select="")
        self.fetch_linear()

    async def action_toggle_linear(self) -> None:
        if not self.linear_active:
            self.notify("Linear is off, so there are no Linear cards to hide.")
            return
        self.hide_linear = not self.hide_linear
        await self.reload()

    async def action_init(self) -> None:
        if self.repo_board is not None:
            self.notify("This repository already has a board.")
            return
        if self.init_board is None:
            self.notify("Not in a Git repository.")
            return
        try:
            board = await asyncio.to_thread(self.init_board)
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        self.repo_board = board
        self.view = "repo"
        self.col = self.row = 0
        self.linear_state = "loading"
        await self.reload(select="")
        self.fetch_linear()

    def action_config(self) -> None:
        path = self.repo_board
        try:
            meta = self.store.board_meta(path) if path else BoardMeta(None, [])
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        if meta.error:
            self.notify(meta.error, severity="error")
            return
        config = self.store.config
        settings = {key: getattr(config, key) for key in ConfigScreen.GLOBAL_KEYS}
        self.push_screen(ConfigScreen(path, meta, path is not None and self.resolve_link is not None, settings,
                                      sorted(self.available_themes), pi_model_choices(config)))

    def submit_config(self, screen: ConfigScreen, merge: str | None, removed: set[str],
                      project: str, labels: list[str], removed_pins: set[str] = frozenset(),
                      settings: dict[str, str | None] | None = None) -> None:
        """Write the dialog's settings. The Linear lookup runs first, so a failed one writes nothing.

        `settings` holds the changed global settings. The board file and the config file are
        separate, so a failed config save after a board save leaves the board change saved.
        """
        path, before, resolve = screen.path, screen.meta.merge, self.resolve_link
        settings = dict(settings or {})

        def apply_settings() -> None:
            for key, value in settings.items():
                setattr(self.store.config, key, value)
            screen.settings.update(settings)
            if "theme" in settings:
                self.theme = settings["theme"]

        def write() -> None:
            # A global value that cannot save must not leave a board change behind.
            check_global_config(**settings)
            link = resolve(project, labels) if project and resolve else None

            def change(links: list[ProjectLink]) -> list[ProjectLink]:
                kept = [l for l in links if l.id not in removed]
                return with_link(kept, link) if link else kept

            board_changed = path is not None and (merge != before or removed or link or removed_pins)
            if board_changed:
                self.store.update_config(
                    path, merge=KEEP if merge == before else merge,
                    change_links=change if removed or link else None,
                    change_pins=(lambda pins: [p for p in pins if p.id not in removed_pins]) if removed_pins else None)
                if merge != before:
                    # A global-save failure leaves the dialog open; retries compare against the saved merge mode.
                    self.call_from_thread(setattr, screen.meta, "merge", merge)
            if settings:
                try:
                    save_global_config(self.config_file, **settings)
                except HerdboardError as error:
                    if board_changed:
                        raise HerdboardError(f"Board settings saved; global settings not saved: {error}") from error
                    raise
                self.call_from_thread(apply_settings)

        self._start_write(write, add=screen, refetch=bool(removed or project or removed_pins))

    def action_import_issue(self) -> None:
        """Search Linear and pin the chosen issue to this repository's board, or a chosen one."""
        if self.search_issues is None:
            self.notify("Pinning a Linear issue needs Linear and a connection.", severity="warning")
            return
        if self.repo_board is not None:
            self._search_and_pin(self.repo_board)
            return
        try:
            boards = [path for path in self.store.board_paths() if self.store.board_meta(path).repo]
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        if not boards:
            self.notify("There is no repository board to pin to. Press i in a repository to create one.",
                        severity="warning")
            return
        options = [(self.store.board_name(path), path) for path in sorted(boards, key=self.store.board_name)]

        def chosen(path: Path | None) -> None:
            if path is not None:
                self._search_and_pin(path)

        self.push_screen(BoardChoiceScreen(None, options, prompt="Pin a Linear issue to"), chosen)

    def _search_and_pin(self, board: Path) -> None:
        name = self.store.board_name(board)

        def pin(issue: dict | None) -> None:
            if issue is None:
                return
            identifier = issue["identifier"]

            def write() -> None:
                _, added = add_pin(self.store, board, issue)
                self.call_from_thread(self.notify, f"Pinned {identifier} to {name}" if added
                                      else f"{identifier} is already pinned to {name}")

            self._start_write(write, refetch=True)

        self.push_screen(IssueSearchScreen(self.search_issues, name), pin)

    def action_unpin(self) -> None:
        card = self.current_card()
        if card is None:
            return
        if card.source != "linear":
            self.notify("Only a pinned Linear issue can be unpinned.", severity="warning")
            return
        board = self.repo_board if self.view == "repo" else None
        if board is None:
            self.notify("Unpin works in the repo view, where the board's pinned issues show.", severity="warning")
            return
        try:
            pins = self.store.board_meta(board).pins
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        # The pin may keep an older identifier, from before the issue moved team: match the ID.
        ref, issue_id = card.ref, card.issue_id
        if not any(pin.id == issue_id for pin in pins):
            self.notify(f"{ref} is not pinned to this board: a linked Linear project shows it.",
                        severity="warning")
            return

        def write() -> None:
            unpin_issue(self.store, board, issue_id)
            self.call_from_thread(self.notify, f"Unpinned {ref}")

        self._start_write(write, refetch=True)

    async def action_reload(self) -> None:
        await self.reload()
        self.fetch_linear()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())

    def action_pull_requests(self) -> None:
        self.show_tab("prs")

    def current_tab(self) -> str:
        if isinstance(self.screen, PullRequestsScreen):
            return "prs"
        if isinstance(self.screen, NotesScreen):
            return "notes"
        return "board"

    def show_tab(self, tab: str) -> None:
        """Show a tab. The tab screens are installed once, so the pull request list keeps its page,
        search and selection, and at most one of them sits on the board."""
        current = self.current_tab()
        if tab == current:
            return
        if current == "notes" and not self.get_screen(NOTES_SCREEN).leave():
            return
        if tab == "board":
            self.pop_screen()
            return
        if tab == "prs":
            name = PR_SCREEN
            if not self.is_screen_installed(PR_SCREEN):
                self.install_screen(PullRequestsScreen(self.pr_service), name=PR_SCREEN)
        else:
            name = NOTES_SCREEN
            if not self.is_screen_installed(NOTES_SCREEN):
                self.install_screen(NotesScreen(self.notes_board()), name=NOTES_SCREEN)
            elif not self.get_screen(NOTES_SCREEN).set_board(self.notes_board()):
                return
        if current == "board":
            self.push_screen(name)
        else:
            self.switch_screen(name)

    def notes_board(self) -> str:
        """The board whose note the Notes tab shows: the repository board, else the board of the
        selected todo; the inbox, whose note is the global note, without one."""
        if self.view == "repo" and self.repo_board is not None:
            return self.store.board_name(self.repo_board)
        card = self.current_card()
        if self.view != "inbox" and card is not None and card.source != "linear" and card.path is not None:
            return self.store.board_name(card.path)
        return notes.GLOBAL

    async def action_quit(self) -> None:
        """Save a note edit first. When it cannot be saved, ask before the edit is lost."""
        if self.is_screen_installed(NOTES_SCREEN):
            screen = self.get_screen(NOTES_SCREEN)
            if not screen.save():
                if not screen.asking:
                    self.push_screen(ConfirmScreen("Quit without saving the note?", screen.status_text()),
                                     lambda answer: answer and self.exit())
                return
        self.exit()


# The board's own key actions, without parameters. Quit stays available in dialogs.
BOARD_ACTIONS = {
    binding.action.split("(")[0] for binding in HerdboardApp.BINDINGS if binding.action != "quit"
}
