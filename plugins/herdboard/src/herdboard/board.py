"""Textual kanban board. Every file change goes through `Store`."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import HorizontalScroll, Vertical, VerticalScroll
from textual.screen import ModalScreen
from textual.widgets import Footer, Input, Label, OptionList, Static
from textual.worker import Worker, WorkerState

from .linear import LinearAuthError
from .linear_writes import LinearWriter
from .model import HerdboardError
from .store import TEMP_REF, CardView, Store
from .start import Starter, mark_removed
from .views import LinearResult

VIEWS = ["repo", "global", "all", "mine"]
RELOAD_SECONDS = 2.0
LINEAR_SECONDS = 300.0
WRITE_GROUP = "store-write"
LINEAR_GROUP = "linear-fetch"
START_GROUP = "card-start"
LINEAR_OFFLINE_MESSAGE = "Linear cards are read only offline"
LINEAR_WRITER_MESSAGE = "Linear writes are not available"
LINEAR_MARK = "◆"

HELP_KEYS = [
    ("left, h", "previous column"),
    ("right, l", "next column"),
    ("up, k", "previous card"),
    ("down, j", "next card"),
    ("shift+left, H", "move card to previous column (a Linear card needs a connection)"),
    ("shift+right, L", "move card to next column (a Linear card needs a connection)"),
    ("enter", "card detail"),
    ("a", "add a card to the selected column"),
    ("d", "mark the card done (a Linear card moves to its completed state)"),
    ("s", "start work with the default agent"),
    ("S", "choose claude or pi and start work"),
    ("v", "cycle view: repo, global, all, mine"),
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

    @property
    def header(self) -> str:
        count = len(self.cards)
        return f"{self.name} {count}" if self.limit is None else f"{self.name} {count}/{self.limit}"


class CardWidget(Static):
    def __init__(self, card: CardView, show_board: bool):
        self.card = card
        text = Text(card.title)
        meta = Text(card.ref, style="dim")
        if card.source == "linear":
            meta = Text(f"{LINEAR_MARK} {card.ref}", style="dim")
            if card.pr:
                meta.append(" · PR", style="bold")
        if show_board:
            meta.append(f" · {card.board}", style="dim")
        if card.body:
            meta.append(f" +{len(card.body)}", style="dim")
        if card.worktree_removed:
            meta.append(" · worktree removed", style="bold yellow")
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
        if card.source == "linear":
            lines.append(f"state: {card.state}")
            if card.team:
                lines.append(f"team: {card.team}")
            if card.url:
                lines.append(f"url: {card.url}")
            if card.pr:
                lines.append("pull request: yes")
        if card.due:
            lines.append(f"due: {card.due}")
        if card.tags:
            lines.append(("labels: " if card.source == "linear" else "tags: ") + ", ".join(card.tags))
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
    """Select the agent before starting a card."""

    def compose(self) -> ComposeResult:
        with Vertical(classes="dialog"):
            yield Label("Choose an agent")
            yield OptionList("claude", "pi")
            yield Label("enter: choose, escape: cancel", classes="dim")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        self.dismiss(("claude", "pi")[event.option_index])


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
        Binding("s", "start", "Start"),
        Binding("S", "choose_agent", "Choose agent", show=False),
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
        linear_for: Callable[[str], LinearResult] | None = None,
        linear_active: bool = False,
        available: Callable[[str], bool] | None = None,
        linear_interval: float = LINEAR_SECONDS,
        linear_writer: LinearWriter | None = None,
        start_card: Callable[[str, str | None], dict] | None = None,
        directory: Path | None = None,
    ):
        super().__init__()
        self.store = store
        self.view = view
        self.paths_for = paths_for
        self.repo_board = repo_board
        self.reload_interval = reload_interval
        self.linear_for = linear_for
        self.linear_active = linear_active and linear_for is not None
        self.available = available or (
            lambda view: view != "mine" and (view != "repo" or repo_board is not None)
        )
        self.linear_interval = linear_interval
        self.linear_writer = linear_writer
        self.start_card = start_card
        self.directory = directory or Path.cwd()
        self.starting = False
        self.linear_results: dict[str, LinearResult] = {}
        self.linear_state = "loading" if self.linear_active else "off"
        # One Linear fetch runs at a time. A refresh asked meanwhile waits as one queued fetch.
        self.linear_fetching = False
        self.linear_queued = False
        self.linear_sequence = 0
        self.linear_applied: dict[str, int] = {}
        self.columns: list[ColumnData] = []
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
        for path in paths:
            for column in self.store.snapshot(path).columns:
                data = by_key.setdefault(column.title.casefold(), ColumnData(column.title))
                if data.limit is None:
                    data.limit = column.limit
                data.completed = data.completed or column.completed
                data.cards.extend(column.cards)
        linear = self.linear_results.get(self.view)
        for card in linear.cards if linear else []:
            data = by_key.setdefault(card.column.casefold(), ColumnData(card.column))
            data.cards.append(card)
        mark_removed([card for data in by_key.values() for card in data.cards])
        if len(paths) == 1 and not linear:
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
            columns = await asyncio.to_thread(self._load, paths)
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        self.paths, self.signature, self.columns = paths, signature, columns
        self._select(identity, column)
        await self._rebuild()

    def linear_label(self) -> str:
        """The Linear part of the title bar."""
        if not self.linear_active:
            return "Linear: off"
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
        cards = [card for column in self.columns for card in column.cards]
        old = [card.worktree_removed for card in cards]
        try:
            await asyncio.to_thread(mark_removed, cards)
        except HerdboardError as error:
            self.notify(str(error), severity="error")
            return
        if self._signature(self.paths_for_safe()) != self.signature or old != [card.worktree_removed for card in cards]:
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

    def _start_write(
        self,
        change: Callable[[], CardView | None],
        add: AddScreen | None = None,
        refetch: bool = False,
    ) -> bool:
        """Run a store or Linear change in a thread worker, one at a time. False when busy."""
        if self.writing or self.starting:
            self.notify("Busy: a change is still being written", severity="warning")
            return False

        def guarded() -> tuple[CardView | None, HerdboardError | None]:
            try:
                return change(), None
            except HerdboardError as error:
                return None, error

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
                self.notify(f"Started {result['ref']} in {result['workspace_id']} ({result['worktree_path']})")
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
        name = self.columns[target].name
        if card.source == "linear":
            self._linear_write(card, lambda writer: writer.move(card.ref, name, None, self.ask_state))
            return
        self._start_write(lambda: self.store.move(card.ref, name))

    def action_done(self) -> None:
        card = self.current_card()
        if card is None:
            return
        if card.source == "linear":
            self._linear_write(card, lambda writer: writer.done(card.ref, self.ask_state))
            return
        self._start_write(lambda: self.store.done(card.ref))

    def _start_card(self, ref: str, agent: str | None) -> None:
        if self.starting or self.writing:
            self.notify("Busy: a card change is in progress", severity="warning")
            return

        def guarded():
            try:
                if self.start_card:
                    return self.start_card(ref, agent), None
                starter = Starter(self.store, self.directory, writer=self.linear_writer,
                                  ask_state=self.ask_state, repository_view=self.view == "repo")
                repo = self.repo_board if self.view == "repo" else None
                return starter.start(ref, agent=agent, repo=repo), None
            except HerdboardError as error:
                return None, error

        self.starting = True
        self.run_worker(guarded, thread=True, group=START_GROUP, exclusive=True, exit_on_error=False)

    def action_start(self) -> None:
        card = self.current_card()
        if card:
            self._start_card(card.ref, None)

    def action_choose_agent(self) -> None:
        card = self.current_card()
        if card:
            ref = card.ref
            self.push_screen(AgentScreen(), lambda agent: self._start_card(ref, agent) if agent else None)

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
        self.push_screen(DetailScreen(card, notes))

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
        path = self._add_path()
        column = self.columns[self.col].name
        self.push_screen(AddScreen(path, self.store.board_name(path), column))

    def submit_add(self, screen: AddScreen, text: str) -> None:
        """Add `text` to the destination the dialog captured when it opened."""
        self._start_write(lambda: self.store.add(screen.path, text, screen.column), add=screen)

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

    async def action_reload(self) -> None:
        await self.reload()
        self.fetch_linear()

    def action_help(self) -> None:
        self.push_screen(HelpScreen())
