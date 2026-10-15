"""Textual kanban board. Every file change goes through `Store`."""

from __future__ import annotations

import asyncio
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
import threading
import zlib
from typing import Callable

from rich.table import Table
from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container, Horizontal, HorizontalScroll, Vertical, VerticalScroll
from textual.driver import Driver
from textual.drivers.linux_driver import LinuxDriver
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, Markdown, OptionList, Select, Static, TextArea
from textual.worker import Worker, WorkerState

from . import activity, attachments
from .linear import LinearAuthError
from .linear_links import ProjectLink, with_link
from .linear_writes import LinearWriter
from .model import HerdboardError, join_title, parse_tags, plain_tags, split_title
from .store import KEEP, TEMP_REF, BoardMeta, CardView, Store
from . import start as start_module
from .config import THINKING_LEVELS
from . import merge as merge_module
from .merge import Merger
from .agent_review import AgentReviewer
from .plan import Planner, mark_planning
from .review import Reviewer
from .start import Starter, mark_removed, pi_model_choices
from .views import LinearResult

VIEWS = ["repo", "inbox", "all", "mine"]
RELOAD_SECONDS = 2.0
LINEAR_SECONDS = 300.0
WRITE_GROUP = "store-write"
LINEAR_GROUP = "linear-fetch"
START_GROUP = "card-start"
DETAIL_LOG_ENTRIES = 10
PR_STYLES = {"merged": "bold green", "closed": "bold yellow"}
LINEAR_OFFLINE_MESSAGE = "Linear cards are read only offline"
LINEAR_WRITER_MESSAGE = "Linear writes are not available"
LINEAR_MARK = "◆"
# Shown in the top-right corner of a card that has attachments.
ATTACHMENT_MARK = "≡"

HELP_KEYS = [
    ("left, h", "previous column"),
    ("right, l", "next column"),
    ("up, k", "previous card"),
    ("down, j", "next card"),
    ("shift+left, H", "move card to previous column (a Linear card needs a connection)"),
    ("shift+right, L", "move card to next column (a Linear card needs a connection)"),
    ("shift+up, K", "move the todo up in its column"),
    ("shift+down, J", "move the todo down in its column"),
    ("/", "filter: words match title, tags and details; #tag matches a tag, #type/bug a type; enter keeps it"),
    ("escape", "clear the filter"),
    ("enter", "card detail; there, o opens an attachment"),
    ("a", "add a card to the selected column: title, type, tags, due date, details, and the board"),
    ("d", "mark the card done (a Linear card moves to its completed state)"),
    ("e", "edit the todo: title, type, tags, due date and details (ctrl+s saves)"),
    ("b", "move the todo to another board, for example from the inbox to this repository"),
    ("x", "delete the todo, after you confirm"),
    ("A", "archive the todo; in the archive column, move it back to the done column (an archived parent too, after you confirm)"),
    ("D", "archive every card in the done column, after you confirm"),
    ("z", "show or hide the archive column"),
    ("s", "start work with the default agent"),
    ("S", "choose claude or pi (and pi's model and thinking level), then start work"),
    ("P", "plan the card with an agent in a new tab; the plan goes to the work agent's brief"),
    ("g", "go to the started card's Herdr workspace"),
    ("R", "review a started card: in Hunk next to its agent, by another agent, or both"),
    ("p", "set or remove the todo's parent: sub-cards show ↳ parent, a parent shows its done count"),
    ("m", "merge the card branch into its base (local, or a draft PR that an agent describes), after you confirm"),
    ("f", "finish a merged card: remove its worktree and branch, move it to Done, after you confirm"),
    ("v", "cycle view: repo, inbox, all, mine"),
    ("t", "hide or show Linear cards"),
    ("i", "create the board for this repository"),
    ("c", "board config: the merge mode and the linked Linear projects (ctrl+s saves)"),
    ("r", "reload, and fetch Linear again"),
    ("?", "this help"),
    ("escape", "close a screen or cancel input"),
    ("q", "quit"),
]


@dataclass
class ColumnData:
    name: str
    limit: int | None = None
    completed: bool = False
    cards: list[CardView] = field(default_factory=list)
    archive: bool = False

    @property
    def header(self) -> str:
        count = len(self.cards)
        return f"{self.name} {count}" if self.limit is None else f"{self.name} {count}/{self.limit}"


# Tag chip colors: terminal palette names, so ANSI themes use the terminal's own colors.
CHIP_COLORS = ["blue", "magenta", "cyan", "green", "yellow", "bright_blue", "bright_magenta", "bright_cyan"]


def chip_color(tag: str) -> str:
    """The same tag always gets the same color."""
    return CHIP_COLORS[zlib.crc32(tag.casefold().encode()) % len(CHIP_COLORS)]


def due_label(due: str, today: date | None = None) -> Text:
    """`📅 Nov 1`, red when past, yellow within two days."""
    today = today or date.today()
    try:
        day = date.fromisoformat(due)
    except ValueError:
        return Text(f"📅 {due}", style="dim")
    shown = f"{day:%b} {day.day}" + ("" if day.year == today.year else f" {day.year}")
    if day < today:
        style = "bold red"
    elif (day - today).days <= 2:
        style = "yellow"
    else:
        style = "dim"
    return Text(f"📅 {shown}", style=style)


# Type badge colors: terminal palette names. Other types are bold in the default color.
TYPE_STYLES = {"bug": "bold red", "feature": "bold green", "docs": "bold cyan", "refactor": "bold magenta"}


def type_badge(kind: str) -> Text:
    """A card type as bold text, colored for the default types."""
    return Text(kind, style=TYPE_STYLES.get(kind.casefold(), "bold"))


# Nerd Font half circles that round off a tag block.
ROUND_LEFT, ROUND_RIGHT = "\ue0b6", "\ue0b4"


def tag_chip(tag: str, style: str) -> Text:
    """A tag as a colored block, with round ends when `style` is "round"."""
    color = chip_color(tag)
    if style == "round":
        chip = Text(ROUND_LEFT, style=color)
        chip.append(tag, style=f"black on {color}")
        chip.append(ROUND_RIGHT, style=color)
        return chip
    return Text(f" {tag} ", style=f"black on {color}")


def card_text(card: CardView, show_board: bool, tag_style: str = "round") -> Text:
    """A card as the board shows it: the title, then tag chips, the due date and notes."""
    if card.source == "linear":
        title, tags, due = card.title, card.tags, card.due
    else:
        title, tags, due = split_title(card.title)
    text = Text(title or card.title)
    line = Text()
    if card.type:
        line.append_text(type_badge(card.type))
    for tag in plain_tags(tags):
        if line:
            line.append(" ")
        line.append_text(tag_chip(tag, tag_style))
    if due:
        if line:
            line.append(" ")
        line.append_text(due_label(due))
    info = []
    if card.source == "linear":
        info.append(Text(f"{LINEAR_MARK} {card.ref}", style="dim"))
        if card.pr:
            info.append(Text("PR", style="bold"))
    if show_board:
        info.append(Text(card.board, style="dim"))
    if card.body:
        info.append(Text(f"+{len(card.body)}", style="dim"))
    if card.worktree_removed:
        info.append(Text("worktree removed", style="bold yellow"))
    if card.pr_state:
        info.append(Text(f"PR {card.pr_state}", style=PR_STYLES.get(card.pr_state, "dim")))
    if card.worktree_left:
        info.append(Text("worktree still exists", style="bold yellow"))
    if card.agent_review:
        info.append(Text(f"agent review: {card.agent_review}", style="bold"))
    if card.pr_writing:
        info.append(Text("PR: writing", style="bold"))
    if card.planning:
        info.append(Text(f"planning: {card.planning}", style="bold cyan"))
    if card.parent_title:
        info.append(Text(f"↳ {card.parent_title}", style="dim"))
    if card.sub_cards:
        info.append(Text(f"{card.sub_cards[0]}/{card.sub_cards[1]} done", style="dim"))
    if info:
        if line:
            line.append("  ")
        line.append_text(Text(" · ", style="dim").join(info))
    if line:
        text.append("\n")
        text.append_text(line)
    return text


class CardWidget(Static):
    def __init__(self, card: CardView, show_board: bool, tag_style: str = "round"):
        self.card = card
        self.rich_text = card_text(card, show_board, tag_style)
        self.plain = self.rich_text.plain
        content = self.rich_text
        if card.has_attachments:
            content = Table.grid(expand=True)
            content.add_column(ratio=1)
            content.add_column(justify="right", no_wrap=True)
            content.add_row(self.rich_text, Text(ATTACHMENT_MARK))
            self.plain += f" {ATTACHMENT_MARK}"
        super().__init__(content, classes="card")


class ColumnHeader(Static):
    def __init__(self, column: ColumnData):
        self.plain = column.header
        super().__init__(Text(column.header), classes="column-header")


class ModalBase(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss_screen", "Close")]

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)


CHECK_ITEM = re.compile(r"^(?P<indent>\u00a0*)[-*+] \[(?P<mark>.)\] (?P<text>.*)$")
BULLET_ITEM = re.compile(r"^(?P<indent>\u00a0*)[-*+] (?P<text>.*)$")


def _hold_indent(line: str) -> str:
    """Leading spaces and tabs as non-breaking spaces: Textual drops plain leading spaces."""
    stripped = line.lstrip(" \t")
    indent = line[: len(line) - len(stripped)].replace("\t", "    ")
    return "\u00a0" * len(indent) + stripped


def detail_lines(lines: list[str], selected: int | None = None) -> Text:
    """Detail lines with checkboxes and bullets drawn as symbols. Done items are struck through.

    `selected` is the index of the checklist line to highlight.
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
    for entry in files:
        if text:
            text.append("\n")
        text.append(entry["name"].ljust(width + 2))
        text.append(attachments.size_label(entry["size"]), style="dim")
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
                 log: list[dict] | None = None, files: list[dict] | None = None):
        super().__init__()
        self.notes = notes or []
        self.entries = log or []
        self.files = files or []
        self.tag_style = tag_style
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

    def _show_body(self) -> None:
        block = self.query_one("#detail-body", RichStatic)
        block.rich_text = detail_lines(self.body, self._selected())
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

    def _title_parts(self) -> tuple[str, list[str], str | None]:
        card = self.card
        if card.source == "linear":
            return card.title, card.tags, card.due
        title, tags, due = split_title(card.title)
        return title, plain_tags(tags), due

    def _meta(self) -> Text:
        card = self.card
        rows = [("Board", card.board), ("Column", card.column), ("ID", card.ref), ("Type", card.type)]
        if card.source == "linear":
            rows += [("State", card.state), ("Team", card.team), ("Link", card.url)]
            if card.pr:
                rows.append(("Pull request", "yes"))
        text = Text()
        for label, value in rows:
            if value:
                if text:
                    text.append("\n")
                text.append(f"{label:<13}", style="dim")
                text.append(str(value))
        return text

    def compose(self) -> ComposeResult:
        title, tags, due = self._title_parts()
        chips = Text()
        for tag in tags:
            if chips:
                chips.append(" ")
            chips.append_text(tag_chip(tag, self.tag_style))
        if due:
            if chips:
                chips.append("  ")
            chips.append_text(due_label(due))
        body = self.body
        with VerticalScroll(classes="dialog detail", can_focus=False):
            yield RichStatic(Text(title or self.card.title, style="bold"), id="detail-title")
            if chips:
                yield RichStatic(chips, id="detail-chips")
            yield RichStatic(self._meta(), id="detail-meta")
            if any(line.strip() for line in body):
                block = RichStatic(detail_lines(body, self._selected()), id="detail-body", classes="detail-block")
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
                block.border_title = "Attachments"
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
    """The key list. Each description wraps inside its own column, so a long entry stays aligned."""

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog help"):
            with Container(id="help", classes="help-grid"):
                for key, what in HELP_KEYS:
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


class BoardChoiceScreen(ModalBase):
    """Choose the board that a todo moves to."""

    def __init__(self, card: CardView, options: list[tuple[str, Path]]):
        super().__init__()
        self.card = card
        self.options = options
        self.board_labels = [label for label, _ in options]

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label(f"Move {split_title(self.card.title)[0]} to")
            yield OptionList(*self.board_labels, id="board-choice")
            yield Label("enter: move, escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(self.options[event.option_index][1])


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
    """A board's frontmatter settings: the merge mode and the Linear project links.

    Enter on a link marks it for removal, or keeps it again. ctrl+s writes every change.
    """

    BINDINGS = [Binding("ctrl+s", "save", "Save")]
    AUTO_FOCUS = "#config-merge"

    def __init__(self, path: Path, meta: BoardMeta, default_merge: str, can_link: bool):
        super().__init__()
        self.path = path
        self.meta = meta
        self.default_merge = default_merge
        self.can_link = can_link
        self.removed: set[str] = set()

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

    def compose(self) -> ComposeResult:
        modes = [(f"config default ({self.default_merge})", "default"),
                 ("local: merge into the base branch", "local"),
                 ("pr: open a draft pull request", "pr")]
        with VerticalScroll(classes="dialog card-form config", can_focus=False):
            yield RichStatic(Text(f"Config of {self.path.stem}", style="bold"), id="config-heading")
            if self.meta.repo:
                yield Label(f"repo: {self.meta.repo}", classes="dim")
            yield self._titled(Select(modes, value=self.meta.merge or "default", allow_blank=False,
                                      id="config-merge"), "Merge")
            if self.meta.links:
                yield self._titled(OptionList(*[self._link_label(link) for link in self.meta.links],
                                              id="config-links"), "Linear projects")
            else:
                yield Label("No Linear projects are linked.", classes="dim")
            if self.can_link:
                with Horizontal(classes="form-row"):
                    yield self._titled(Input(placeholder="API Platform", id="config-project",
                                             select_on_focus=False), "Link a Linear project")
                    yield self._titled(Input(placeholder="repo:api, bug", id="config-labels",
                                             select_on_focus=False), "Labels")
            yield RichStatic(Text(""), id="config-error", classes="error")
            if self.meta.links:
                yield Label("enter on a project: remove it, or keep it again", classes="dim")
            yield Label("ctrl+s: save · tab: next field · escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        link = self.meta.links[event.option_index]
        self.removed ^= {link.id}
        event.option_list.replace_option_prompt_at_index(event.option_index, self._link_label(link))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in the project field moves to its labels. Enter in the labels field saves."""
        event.stop()
        if event.input.id == "config-project":
            self.query_one("#config-labels", Input).focus()
            return
        self.action_save()

    def show_error(self, message: str) -> None:
        error = self.query_one("#config-error", RichStatic)
        error.rich_text = Text(message)
        error.plain = message
        error.update(error.rich_text)
        self.call_after_refresh(error.scroll_visible, animate=False)

    def action_save(self) -> None:
        merge = self.query_one("#config-merge", Select).value
        project, labels = "", []
        if self.can_link:
            project = self.query_one("#config-project", Input).value.strip()
            labels = [label.strip() for label in self.query_one("#config-labels", Input).value.split(",")]
        self.app.submit_config(self, None if merge == "default" else merge, set(self.removed),
                               project, [label for label in labels if label])


class CellMouseDriver(LinuxDriver):
    """Textual's terminal driver without pixel mouse reports (mode 1016).

    Textual turns pixel reports on when the terminal supports in-band resize.
    Herdr reads mouse positions as cells, so pixel reports stop clicks on its
    own tabs and sidebar while the board is in view.
    """

    def _enable_mouse_pixels(self) -> None:
        return


class HerdboardApp(App):
    TITLE = "herdboard"
    CSS = """
    #title-bar { height: 1; padding: 0 1; background: $primary; color: $text; }
    #columns { height: 1fr; overflow-x: auto; overflow-y: hidden; }
    #columns { scrollbar-size-horizontal: 1; }
    .column { width: 1fr; min-width: 24; height: 100%; border: round $surface-lighten-2; }
    .column.completed { border: round $success; }
    .column-header { text-style: bold; padding: 0 1; background: $surface; }
    .column.completed .column-header { color: $success; }
    .cards { height: 1fr; scrollbar-size-vertical: 1; }
    #filter { dock: bottom; margin-bottom: 1; border: round $primary; }
    .card { padding: 0 1; margin: 0 0 1 0; background: $panel; height: auto; }
    .card.selected { background: $accent; color: $text; }
    .column.current { border: round $accent; }
    .dialog { width: 70; max-width: 90%; height: auto; max-height: 80%; padding: 1 2;
              border: round $accent; background: $surface; }
    /* A Label is as wide as its text by default, so a long line runs past the dialog edge. */
    .dialog Label { width: 100%; }
    .help { width: 90; max-width: 95%; }
    .help-grid { layout: grid; grid-size: 2; grid-columns: 16 1fr; grid-rows: auto;
                 grid-gutter: 0 1; width: 100%; height: auto; }
    .help-key { width: 16; height: auto; }
    .help-what { height: auto; }
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
    #config-merge { border: round $surface-lighten-2; height: auto; }
    #config-merge > SelectCurrent { border: none; }
    #config-merge:focus-within { border: round $primary; }
    .config, #config-links { scrollbar-size-vertical: 1; }
    #config-links { height: auto; max-height: 6; border: round $surface-lighten-2; }
    #config-links:focus { border: round $primary; }
    #config-project { width: 2fr; }
    #config-labels { width: 1fr; }
    /* The ANSI themes paint cards in the terminal background, so a border keeps them apart. */
    App:ansi .card { border: round ansi_bright_black; margin: 0; }
    App:ansi .column { border: round ansi_bright_black; }
    App:ansi .column.completed { border: round $success; }
    .column.archive { border: dashed $surface-lighten-2; }
    App:ansi .column.archive { border: dashed ansi_bright_black; }
    .column.archive.current { border: dashed $accent; }
    App:ansi .column.archive.current { border: dashed ansi_magenta; }
    /* In the ANSI themes the accent and the done color are both green, and the title bar is blue.
       The current column uses magenta and the selected card yellow, so each has its own color.
       Cards outside the current column fade. */
    App:ansi .column .card { color: ansi_bright_black; }
    App:ansi .column.current { border: round ansi_magenta; }
    App:ansi .column.current .column-header { background: ansi_magenta; color: ansi_black; }
    App:ansi .column.current .card { color: ansi_default; }
    App:ansi .card.selected { background: ansi_default; border: heavy ansi_yellow; text-style: bold; }
    App:ansi #title-bar { color: ansi_black; }
    App:ansi .dialog { border: round $primary; }
    App:ansi .detail-block { border: round ansi_bright_black; }
    App:ansi .card-form Input, App:ansi .card-form TextArea { border: round ansi_bright_black; }
    App:ansi .card-form Input:focus, App:ansi .card-form TextArea:focus { border: round $primary; }
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
        Binding("r", "reload", "Reload"),
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
        # Passed on to the background workers, so they read the same boards.
        self.config_file = config_file
        self.starting = False
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

    # Layout

    def compose(self) -> ComposeResult:
        title = Static("", id="title-bar")
        title.plain = ""
        yield title
        yield HorizontalScroll(id="columns", can_focus=False)
        search = Input(placeholder="Filter: words, or #tag · enter keeps it · escape clears it", id="filter")
        search.display = False
        search.disabled = True
        yield search
        yield Footer()

    async def on_mount(self) -> None:
        await self.reload()
        self.set_interval(self.reload_interval, self.check_files)
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
        title = self.base.query_one("#title-bar", Static)
        title.plain = f"herdboard · view: {self.view} · {names} · {self.linear_label()}"
        if self.filter_text:
            shown = sum(len(c.cards) for c in self.columns)
            title.plain += f' · filter: "{self.filter_text}" ({shown} of {sum(len(c.cards) for c in self.loaded)})'
        if self.show_archive:
            title.plain += " · archive shown"
        if self.repo_board is None and self.init_board is not None:
            title.plain += " · no board for this repository: press i to create it"
        title.update(Text(title.plain))
        show_board = len(self.paths) > 1
        container = self.base.query_one("#columns", HorizontalScroll)
        existing = list(container.query(".column"))
        # Repainting every column on each change made the screen flash. Keep the
        # column widgets when the columns are the same, and replace only the cards
        # of columns whose cards changed, in one screen update.
        with self.batch_update():
            if [getattr(w, "column_name", None) for w in existing] == [c.name for c in self.columns]:
                for widget, column in zip(existing, self.columns):
                    await self._update_column(widget, column, show_board)
            else:
                await container.remove_children()
                await container.mount_all([self._column_widget(c, show_board) for c in self.columns])
            self._show_selection()

    def _column_widget(self, column: ColumnData, show_board: bool) -> Vertical:
        classes = "column" + (" completed" if column.completed else "") + (" archive" if column.archive else "")
        cards = VerticalScroll(
            *[CardWidget(card, show_board, self.store.config.tag_style) for card in column.cards], classes="cards", can_focus=False
        )
        widget = Vertical(ColumnHeader(column), cards, classes=classes)
        widget.column_name = column.name
        return widget

    async def _update_column(self, widget: Vertical, column: ColumnData, show_board: bool) -> None:
        widget.set_class(column.completed, "completed")
        widget.set_class(column.archive, "archive")
        header = widget.query_one(ColumnHeader)
        if header.plain != column.header:
            header.plain = column.header
            header.update(Text(column.header))
        holder = widget.query_one(".cards", VerticalScroll)
        old = list(holder.query(CardWidget))
        new = [CardWidget(card, show_board, self.store.config.tag_style) for card in column.cards]
        if [w.plain for w in old] == [w.plain for w in new]:
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
            return [(card.worktree_removed, card.worktree_left, card.pr_state, card.agent_review, card.pr_writing,
                     card.planning, card.has_attachments)
                    for card in cards]

        old = status()
        try:
            await asyncio.to_thread(merge_module.refresh_pr_states)
            await asyncio.to_thread(mark_removed, cards)
            await asyncio.to_thread(mark_planning, cards)
            await asyncio.to_thread(attachments.mark, self.store.config, cards)
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
        if isinstance(self.screen, ModalScreen) and action in BOARD_ACTIONS:
            return False
        return True

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
                    thinking: str | None = None) -> None:
        if self.starting or self.writing:
            self.notify("Busy: a card change is in progress", severity="warning")
            return

        def guarded():
            try:
                if self.start_card:
                    if model or thinking:
                        return self.start_card(ref, agent, model=model, thinking=thinking), None
                    return self.start_card(ref, agent), None
                starter = Starter(self.store, self.directory, writer=self.linear_writer,
                                  ask_state=self.ask_state, repository_view=self.view == "repo",
                                  config_file=self.config_file)
                repo = self.repo_board if self.view == "repo" else None
                return starter.start(ref, agent=agent, repo=repo, model=model, thinking=thinking), None
            except HerdboardError as error:
                return None, error

        self.starting = True
        self.run_worker(guarded, thread=True, group=START_GROUP, exclusive=True, exit_on_error=False)

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

    def _plan(self, ref: str, agent: str, model: str | None, thinking: str | None) -> None:
        options = {"agent": agent, "model": model, "thinking": thinking}

        def run() -> dict:
            if self.plan_card:
                return self.plan_card(ref, **options)
            return self._planner().plan(ref, repo=self.repo_board if self.view == "repo" else None, **options)

        def notice(result: dict) -> str:
            model_text = f" ({result['model']})" if result["model"] else ""
            return f"{result['agent']}{model_text} plans {result['ref']} in a new tab"

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
            return f"Finished {result['ref']}; branch {'deleted' if result['branch_deleted'] else 'kept'}"

        self.push_screen(
            ConfirmScreen(f"Finish {ref}: {card.title}?",
                          "The worktree and the merged branch are removed, and the card moves to Done."),
            lambda answer: self._card_action(run, notice) if answer else None,
        )

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
            DetailScreen(card, notes, self.store.config.tag_style, log, files),
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
        if path is None:
            self.notify("There is no repository board to configure. Press i in a repository to create one.",
                        severity="warning")
            return
        try:
            meta = self.store.board_meta(path)
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        if meta.error:
            self.notify(meta.error, severity="error")
            return
        self.push_screen(ConfigScreen(path, meta, self.store.config.merge, self.resolve_link is not None))

    def submit_config(self, screen: ConfigScreen, merge: str | None, removed: set[str],
                      project: str, labels: list[str]) -> None:
        """Write the dialog's settings. The Linear lookup runs first, so a failed one writes nothing."""
        path, before, resolve = screen.path, screen.meta.merge, self.resolve_link

        def write() -> None:
            link = resolve(project, labels) if project and resolve else None

            def change(links: list[ProjectLink]) -> list[ProjectLink]:
                kept = [l for l in links if l.id not in removed]
                return with_link(kept, link) if link else kept

            self.store.update_config(path, merge=KEEP if merge == before else merge,
                                     change_links=change if removed or link else None)

        self._start_write(write, add=screen, refetch=bool(removed or project))

    async def action_reload(self) -> None:
        await self.reload()
        self.fetch_linear()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())


# The board's own key actions, without parameters. Quit stays available in dialogs.
BOARD_ACTIONS = {
    binding.action.split("(")[0] for binding in HerdboardApp.BINDINGS if binding.action != "quit"
}
