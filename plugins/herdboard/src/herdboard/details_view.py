"""The Textual app of `herdboard details`: the card details lines.

Herdr draws the pane border and its `details - <ref>` label, so the app draws no border.

The app has no keys of its own. It checks the watched files every two seconds and draws
again when one changes or the pane changes size.
"""

from __future__ import annotations

import time
from typing import Callable

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from . import details
from .model import HerdboardError

CHECK_SECONDS = 2.0
# A Linear card costs a request, so the pane asks Linear again at most once a minute.
LINEAR_SECONDS = 60.0


class CachedLookup:
    """`lookup(ref)`, kept for `seconds`, so the redraws after file changes do not ask Linear each time.

    A failed lookup is kept too: its error is raised again until `seconds` pass.
    """

    def __init__(self, lookup: Callable, seconds: float = LINEAR_SECONDS, clock: Callable[[], float] = time.monotonic):
        self.lookup = lookup
        self.seconds = seconds
        self.clock = clock
        self.found = None
        self.error: HerdboardError | None = None
        self.at: float | None = None

    def __call__(self, ref: str):
        if self.at is None or self.clock() - self.at >= self.seconds:
            try:
                self.found, self.error = self.lookup(ref), None
            except HerdboardError as error:
                self.found, self.error = None, error
            self.at = self.clock()
        if self.error is not None:
            raise self.error
        return self.found


class DetailsApp(App):
    CSS = """
    Screen { background: $background; }
    #details { padding: 0 1; height: 1fr; }
    """

    def __init__(self, store, config, ref: str, linear_card: Callable | None = None,
                 check_seconds: float = CHECK_SECONDS):
        super().__init__()
        if config.theme in self.available_themes:
            self.theme = config.theme
        self.store = store
        self.config = config
        self.ref = ref
        self.linear_card = CachedLookup(linear_card) if linear_card is not None else None
        self.check_seconds = check_seconds
        self.data: dict | None = None
        self.seen: tuple | None = None

    def compose(self) -> ComposeResult:
        yield Static(id="details")

    def on_mount(self) -> None:
        self.reload()
        self.set_interval(self.check_seconds, self.check)

    def on_resize(self) -> None:
        self.draw()

    def paths(self) -> list:
        card = (self.data or {}).get("card")
        return details.watched_paths(self.config, (self.data or {}).get("ref", self.ref),
                                     card.path if card is not None else None)

    def check(self) -> None:
        linear_due = self.linear_card is not None and (
            self.linear_card.at is None or self.linear_card.clock() - self.linear_card.at >= self.linear_card.seconds)
        if linear_due or details.signature(self.paths()) != self.seen:
            self.reload()

    def reload(self) -> None:
        try:
            self.data = details.details_data(self.store, self.config, self.ref, self.linear_card)
        except HerdboardError as error:
            self.data = None
            self.query_one("#details", Static).update(Text(str(error), style="dim"))
            return
        self.seen = details.signature(self.paths())
        self.draw()

    def draw(self) -> None:
        if self.data is None:
            return
        box = self.query_one("#details", Static)
        # The padding takes two columns.
        width = max(self.size.width - 2, 1)
        height = max(self.size.height, 1)
        box.update(Text("\n").join(details.details_lines(self.data, width, height)))
