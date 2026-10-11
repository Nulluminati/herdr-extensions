"""Textual kanban board. Every file change goes through `Store`."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, Static
from textual.worker import Worker, WorkerState

from .model import HerdboardError
from .store import TEMP_REF, CardView, Store

VIEWS = ["repo", "global", "all"]
RELOAD_SECONDS = 2.0
WRITE_GROUP = "store-write"

HELP_KEYS = [
    ("left, h", "previous column"),
    ("right, l", "next column"),
    ("up, k", "previous card"),
    ("down, j", "next card"),
    ("shift+left, H", "move card to previous column"),
    ("shift+right, L", "move card to next column"),
    ("enter", "card detail"),
    ("a", "add a card to the selected column"),
    ("d", "mark the card done"),
    ("v", "cycle view: repo, global, all"),
    ("r", "reload"),
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

    @property
    def header(self) -> str:
        count = len(self.cards)
        return f"{self.name} {count}" if self.limit is None else f"{self.name} {count}/{self.limit}"


class CardWidget(Static):
    def __init__(self, card: CardView, show_board: bool):
        self.card = card
        text = Text(card.title)
        meta = Text(card.ref, style="dim")
        if show_board:
            meta.append(f" · {card.board}", style="dim")
        if card.body:
            meta.append(f" +{len(card.body)}", style="dim")
        text.append("\n")
        text.append_text(meta)
        self.plain = text.plain
        super().__init__(text, classes="card")


class ColumnHeader(Static):
    def __init__(self, column: ColumnData):
        self.plain = column.header
        super().__init__(Text(column.header), classes="column-header")


class ModalBase(ModalScreen):
    BINDINGS = [Binding("escape", "dismiss_screen", "Close")]

    def action_dismiss_screen(self) -> None:
        self.dismiss(None)


class DetailScreen(ModalBase):
    def __init__(self, card: CardView, notes: list[dict] | None):
        super().__init__()
        lines = [card.title, f"{card.board} / {card.column}", f"ref: {card.ref}"]
        if card.due:
            lines.append(f"due: {card.due}")
        if card.tags:
            lines.append("tags: " + ", ".join(card.tags))
        lines.extend(card.body)
        for note in notes or []:
            lines.append(f"--- {note['name']} ---")
            content = note["content"]
            lines.append("(note not found)" if content is None else content.rstrip("\n"))
        self.lines = lines

    def compose(self) -> ComposeResult:
        with VerticalScroll(classes="dialog"):
            detail = Static(Text("\n".join(self.lines)), id="detail")
            detail.plain = "\n".join(self.lines)
            yield detail
            yield Label("escape: close", classes="dim")


class HelpScreen(ModalBase):
    def compose(self) -> ComposeResult:
        plain = "\n".join(f"{key:<16}{what}" for key, what in HELP_KEYS)
        with VerticalScroll(classes="dialog"):
            help_text = Static(Text(plain), id="help")
            help_text.plain = plain
            yield help_text
            yield Label("escape: close", classes="dim")


class AddScreen(ModalBase):
    """Add dialog. The destination is fixed when the dialog opens."""

    def __init__(self, path: Path, board: str, column: str):
        super().__init__()
        self.path = path
        self.board = board
        self.column = column

    def compose(self) -> ComposeResult:
        target = Static(Text(f"Add to {self.board} / {self.column}"), id="add-target")
        target.plain = f"Add to {self.board} / {self.column}"
        error = Static("", id="add-error", classes="error")
        error.plain = ""
        with Vertical(classes="dialog"):
            yield target
            yield Input(placeholder="Card text", id="add-input")
            yield error

    def show_error(self, message: str) -> None:
        error = self.query_one("#add-error", Static)
        error.plain = message
        error.update(Text(message))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        event.stop()
        if not event.value.strip():
            self.dismiss(None)
            return
        self.app.submit_add(self, event.value)


class HerdboardApp(App):
    TITLE = "herdboard"
    CSS = """
    #title-bar { height: 1; padding: 0 1; background: $primary; color: $text; }
    #columns { height: 1fr; overflow-x: auto; overflow-y: hidden; }
    .column { width: 28; height: 100%; border: round $surface-lighten-2; }
    .column.completed { border: round $success; }
    .column-header { text-style: bold; padding: 0 1; background: $surface; }
    .column.completed .column-header { color: $success; }
    .cards { height: 1fr; }
    .card { padding: 0 1; margin: 0 0 1 0; background: $panel; height: auto; }
    .card.selected { background: $accent; color: $text; }
    .column.current { border: round $accent; }
    .dialog { width: 70; max-width: 90%; height: auto; max-height: 80%; padding: 1 2;
              border: round $accent; background: $surface; }
    .dim { color: $text-muted; }
    .error { color: $error; }
    ModalBase { align: center middle; }
    """
    BINDINGS = [
        Binding("left,h", "column(-1)", "Column", show=False),
        Binding("right,l", "column(1)", "Column", show=False),
        Binding("up,k", "card(-1)", "Card", show=False),
        Binding("down,j", "card(1)", "Card", show=False),
        Binding("shift+left,H", "move(-1)", "Move left"),
        Binding("shift+right,L", "move(1)", "Move right"),
        Binding("enter", "detail", "Detail"),
        Binding("a", "add", "Add"),
        Binding("d", "done", "Done"),
        Binding("v", "cycle_view", "View"),
        Binding("r", "reload", "Reload"),
        Binding("question_mark", "help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(
        self,
        store: Store,
        view: str,
        paths_for: Callable[[str], list[Path]],
        repo_board: Path | None = None,
        reload_interval: float = RELOAD_SECONDS,
    ):
        super().__init__()
        self.store = store
        self.view = view
        self.paths_for = paths_for
        self.repo_board = repo_board
        self.reload_interval = reload_interval
        self.columns: list[ColumnData] = []
        self.paths: list[Path] = []
        self.col = 0
        self.row = 0
        self.signature: tuple = ()
        self.writing = False
        self.add_screen: AddScreen | None = None

    # Layout

    def compose(self) -> ComposeResult:
        title = Static("", id="title-bar")
        title.plain = ""
        yield title
        yield HorizontalScroll(id="columns", can_focus=False)
        yield Footer()

    async def on_mount(self) -> None:
        await self.reload()
        self.set_interval(self.reload_interval, self.check_files)

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
        for path in paths:
            for column in self.store.snapshot(path).columns:
                data = by_key.setdefault(column.title.casefold(), ColumnData(column.title))
                if data.limit is None:
                    data.limit = column.limit
                data.completed = data.completed or column.completed
                data.cards.extend(column.cards)
        if len(paths) <= 1:
            return list(by_key.values())
        wanted = [name.casefold() for name in self.store.config.columns]
        ordered = [by_key[key] for key in wanted if key in by_key]
        ordered += [data for key, data in by_key.items() if key not in wanted]
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
            columns = self._load(paths)
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        self.paths, self.signature, self.columns = paths, signature, columns
        self._select(identity, column)
        await self._rebuild()

    async def _rebuild(self) -> None:
        names = ", ".join(self.store.board_name(path) for path in self.paths) or "no boards"
        title = self.base.query_one("#title-bar", Static)
        title.plain = f"herdboard · view: {self.view} · {names}"
        title.update(Text(title.plain))
        show_board = len(self.paths) > 1
        container = self.base.query_one("#columns", HorizontalScroll)
        await container.remove_children()
        widgets = []
        for column in self.columns:
            classes = "column completed" if column.completed else "column"
            cards = VerticalScroll(
                *[CardWidget(card, show_board) for card in column.cards], classes="cards", can_focus=False
            )
            widgets.append(Vertical(ColumnHeader(column), cards, classes=classes))
        await container.mount_all(widgets)
        self._show_selection()

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
        if self._signature(self.paths_for_safe()) != self.signature:
            await self.reload()

    def paths_for_safe(self) -> list[Path]:
        try:
            return self.paths_for(self.view)
        except HerdboardError:
            return self.paths

    # Actions

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        # Modal screens handle their own keys.
        if isinstance(self.screen, ModalScreen) and action != "quit":
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

    def _start_write(self, change: Callable[[], CardView | None], add: AddScreen | None = None) -> bool:
        """Run a store change in a thread worker, one at a time. False when busy."""
        if self.writing:
            self.notify("Busy: a change is still being written", severity="warning")
            return False

        def guarded() -> tuple[CardView | None, HerdboardError | None]:
            try:
                return change(), None
            except HerdboardError as error:
                return None, error

        self.writing = True
        self.add_screen = add
        self.run_worker(guarded, thread=True, group=WRITE_GROUP, exclusive=True, exit_on_error=False)
        return True

    async def on_worker_state_changed(self, event: Worker.StateChanged) -> None:
        """Apply the result of a store change on the event loop."""
        worker = event.worker
        if worker.group != WRITE_GROUP or event.state not in (
            WorkerState.SUCCESS,
            WorkerState.ERROR,
            WorkerState.CANCELLED,
        ):
            return
        self.writing = False
        screen, self.add_screen = self.add_screen, None
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

    def action_move(self, step: int) -> None:
        card = self.current_card()
        target = self.col + step
        if card is None or not 0 <= target < len(self.columns):
            return
        name = self.columns[target].name
        self._start_write(lambda: self.store.move(card.ref, name))

    def action_done(self) -> None:
        card = self.current_card()
        if card is not None:
            self._start_write(lambda: self.store.done(card.ref))

    def action_detail(self) -> None:
        card = self.current_card()
        if card is None:
            return
        try:
            notes = self.store.notes_for(card)
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            notes = None
        self.push_screen(DetailScreen(card, notes))

    def _add_path(self) -> Path:
        if self.view == "repo" and self.repo_board is not None:
            return self.repo_board
        if self.view == "all":
            card = self.current_card()
            if card is not None:
                return card.path
        return self.store.inbox_path

    def action_add(self) -> None:
        if not self.columns:
            self.notify("The view has no columns", severity="error")
            return
        path = self._add_path()
        column = self.columns[self.col].name
        self.push_screen(AddScreen(path, self.store.board_name(path), column))

    def submit_add(self, screen: AddScreen, text: str) -> None:
        """Add `text` to the destination the dialog captured when it opened."""
        self._start_write(lambda: self.store.add(screen.path, text, screen.column), add=screen)

    async def action_cycle_view(self) -> None:
        views = [v for v in VIEWS if v != "repo" or self.repo_board is not None]
        if self.view in views:
            self.view = views[(views.index(self.view) + 1) % len(views)]
        else:
            self.view = views[0]
        self.col = self.row = 0
        await self.reload(select="")

    async def action_reload(self) -> None:
        await self.reload()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
