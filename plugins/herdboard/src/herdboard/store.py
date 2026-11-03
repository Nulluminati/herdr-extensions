"""Read and write boards: card references, IDs, and locked atomic writes.

A board is a folder under `<todos>/boards/`. Its `board.toml` lists the columns and,
in order, the IDs of the cards in each column. Each card is a folder `cards/<id>/`
with `card.md` (see `card_file`), its activity log and its attachments. An archived
card moves to `archive/<id>/`.

Every write holds the store lock for its whole read, check and write. A command that
changes more than one file writes them in an order where a crash leaves at most a
card folder that no column lists (an orphan, which shows at the bottom of the first
column) or a listed ID with no folder (which is skipped). It never loses a card.
"""

from __future__ import annotations

import fcntl
import os
import re
import secrets
import shutil
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import card_file
from .card_file import BoardFile, CardFile, ColumnFile
from .config import MERGE_MODES, Config
from .identity import RepoIdentity, find_board, new_board_path, same_repo
from .linear_links import ProjectLink, parse_links
from .linear_pins import IssuePin, parse_pins
from .migrate_convert import Item
from .model import (
    TRIAGE_COLUMN,
    ArchivedError,
    CardActiveError,
    ConflictError,
    DuplicateIdError,
    FormatError,
    HerdboardError,
    IoError,
    NotFoundError,
    OldFormatError,
    ParentError,
    is_triage,
)

ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
# A todo ID: `t` and three base-36 characters. A card folder's name is its ID.
CARD_ID = re.compile(r"^t[0-9a-z]{3}$")
BOARD_FILE = "board.toml"
CARD_FILE = "card.md"
INBOX = "inbox"


def is_card_id(text: str) -> bool:
    return bool(CARD_ID.match(text))


@dataclass
class CardView:
    ref: str
    board: str
    path: Path | None
    column: str
    title: str
    checked: bool
    due: str | None
    tags: list[str]
    body: list[str]
    column_limit: int | None = None
    # The card's type: a todo's `type` field, or a Linear label that names a configured type.
    type: str | None = None
    # Linear cards fill the fields below; todos keep the defaults.
    source: str = "todo"
    url: str | None = None
    state: str | None = None
    team: str | None = None
    pr: bool = False
    # A Linear issue's attachments, each with its title, subtitle and URL.
    linear_attachments: list[dict] = field(default_factory=list)
    # Linear's manual order inside a column (lowest first); None for todos.
    sort_order: float | None = None
    read_only: bool = False
    fetched_at: str | None = None
    stale: bool = False
    worktree_removed: bool = False
    # Board-only: True while the card's worktree exists, so the card menu offers the started-card actions.
    worktree: bool = False
    # Board-only: True when the card runs without a repository, in its own folder.
    repoless: bool = False
    # Board-only start status: a worktree still there after Done, and the pull request state.
    worktree_left: bool = False
    pr_state: str | None = None
    # Board-only: the URL of the card's pull request, from its start record.
    pr_url: str | None = None
    # Board-only: the kind of agent that `start` started for the card, while its worktree exists.
    agent: str | None = None
    # Board-only: the kind of agent that reviews the card now, while an agent review runs.
    agent_review: str | None = None
    # Board-only: True while an agent writes the card's pull request description.
    pr_writing: bool = False
    # Board-only: the kind of agent that plans the card while its planner pane exists.
    planning: str | None = None
    # Board-only: True when the card has at least one attachment.
    has_attachments: bool = False
    # Board-only: True when the card has a plan attachment.
    planned: bool = False
    # Board-only: "reviewed" while the branch HEAD is the commit of the last agent review, "new commits" after it.
    reviewed: str | None = None
    # Board-only Herdr status (working, blocked, done, idle, unknown) of each live agent pane on the card:
    # the work agent, the planner, the agent reviewer and the pull request writer.
    agent_status: str | None = None
    planning_status: str | None = None
    review_status: str | None = None
    pr_write_status: str | None = None
    # The card ID of a sub-card's parent, from its `parent` field.
    parent: str | None = None
    # Board-only: a Linear card's issue ID, which stays when the identifier changes.
    issue_id: str | None = None
    # Board-only family status: the parent's title, and a parent's (done, total) sub-cards.
    parent_title: str | None = None
    sub_cards: tuple[int, int] | None = None
    # True for a card in the board's archive folder.
    archived: bool = False
    # The Linear identifier that `promote` made from the todo, or None.
    promoted: str | None = None


# `Store.update_config` and `Store.edit` leave a value as it is.
KEEP = object()


@dataclass
class BoardMeta:
    """What Linear views need from a board: its repository, project links and pinned issues."""

    repo: str | None
    links: list[ProjectLink]
    error: str | None = None
    # The board's `merge` setting: "local", "pr" or None for the config default.
    merge: str | None = None
    pins: list[IssuePin] = field(default_factory=list)


@dataclass
class ColumnSnapshot:
    title: str
    limit: int | None
    completed: bool
    cards: list[CardView]


@dataclass
class BoardSnapshot:
    """One read of a board: its name, folder and columns in order."""

    name: str
    path: Path
    columns: list[ColumnSnapshot]
    # The archived cards, newest first, when the read asked for them; else None.
    archive: ColumnSnapshot | None = None


@dataclass
class ImportTarget:
    """Where `migrate` writes. `done_column` is None (with `done_error`) when the board has none."""

    path: Path
    identity: RepoIdentity | None
    exists: bool
    open_column: str
    done_column: str | None
    done_error: str | None = None


@dataclass
class _Card:
    """A card as read: its ID, its folder, the parsed file and the text it was parsed from."""

    id: str
    folder: Path
    file: CardFile
    text: str

    @property
    def path(self) -> Path:
        return self.folder / CARD_FILE


@dataclass
class _Board:
    """A board as read. `file.columns` lists only cards that have a folder, orphans included."""

    path: Path
    file: BoardFile
    # The canonical text of `board.toml` as read, before orphans join the first column.
    text: str
    cards: dict[str, _Card]

    def column_of(self, card_id: str) -> ColumnFile:
        column = self.file.column_of(card_id)
        if column is None:
            raise NotFoundError(f"No card {card_id} on {self.path}")
        return column

    def children(self, card_id: str) -> list[str]:
        return [other for other in self.file.card_ids() if self.cards[other].file.parent == card_id]


@dataclass
class _Located:
    """A card found without the lock: where it was, and its text and column then."""

    path: Path
    block_id: str
    column: str
    text: str


@dataclass
class TodoSnapshot:
    """A todo as `promote` read it. The final update checks that it is still the same."""

    located: _Located
    view: CardView
    # True when the todo is in its board's done column.
    done: bool


def _now() -> datetime:
    return datetime.now().astimezone().replace(microsecond=0)


def _insert(cards: list[str], card_id: str, position: str) -> None:
    if position not in ("top", "bottom"):
        raise ValueError(f"position must be 'top' or 'bottom', not {position!r}")
    if position == "top":
        cards.insert(0, card_id)
    else:
        cards.append(card_id)


class Store:
    def __init__(self, config: Config, lock_timeout: float = 5.0):
        self.config = config
        self.lock_timeout = lock_timeout
        self.lock_path = config.todos_dir / ".herdboard.lock"
        self.boards_dir = config.todos_dir / "boards"
        self.inbox_path = self.boards_dir / INBOX

    # Files

    def _read_text(self, path: Path, what: str) -> str:
        try:
            return path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise IoError(f"{path}: cannot read {what}: {error}") from error

    def _write_file(self, path: Path, data: bytes, mode: int | None = None) -> None:
        """Write to a temporary file beside `path`, then rename it over `path`.

        A new file gets the usual mode for the umask; an existing one keeps its mode.
        """
        try:
            if mode is None:
                try:
                    mode = path.stat().st_mode & 0o7777
                except FileNotFoundError:
                    umask = os.umask(0)
                    os.umask(umask)
                    mode = 0o666 & ~umask
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.chmod(temp, mode)
                os.replace(temp, path)
            except BaseException:
                if os.path.exists(temp):
                    os.unlink(temp)
                raise
        except OSError as error:
            raise IoError(f"{path}: cannot write: {error}") from error

    def _write_board(self, board: _Board) -> None:
        self._write_file(board.path / BOARD_FILE, card_file.dump_board(board.file).encode("utf-8"))

    def _write_card(self, card: _Card) -> None:
        self._write_file(card.path, card_file.dump_card(card.file).encode("utf-8"))

    def _rename(self, source: Path, target: Path) -> None:
        if target.exists():
            raise DuplicateIdError(f"{target} already exists")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.rename(source, target)
        except OSError as error:
            raise IoError(f"{source}: cannot move to {target}: {error}") from error

    def _read_card(self, folder: Path) -> _Card:
        path = folder / CARD_FILE
        text = self._read_text(path, "card")
        return _Card(folder.name, folder, card_file.parse_card(text, path), text)

    def _card_folders(self, folder: Path) -> list[Path]:
        """The card folders under `folder` whose names are card IDs and that hold a `card.md`."""
        try:
            return sorted(p for p in folder.iterdir() if is_card_id(p.name) and (p / CARD_FILE).is_file())
        except FileNotFoundError:
            return []
        except OSError as error:
            raise IoError(f"{folder}: cannot read: {error}") from error

    def _read(self, path: Path) -> _Board:
        """Read a board and every card in its `cards/` folder.

        A listed ID with no folder leaves its column. A folder that no column lists joins
        the bottom of the first column. Archived cards are not read.
        """
        board_path = path / BOARD_FILE
        file = card_file.parse_board(self._read_text(board_path, "board"), board_path)
        text = card_file.dump_board(file)
        cards = {folder.name: self._read_card(folder) for folder in self._card_folders(path / "cards")}
        seen: set[str] = set()
        for column in file.columns:
            kept = []
            for card_id in column.cards:
                if card_id in cards and card_id not in seen:
                    kept.append(card_id)
                    seen.add(card_id)
            column.cards = kept
        orphans = [card_id for card_id in cards if card_id not in seen]
        if orphans and file.columns:
            file.columns[0].cards.extend(orphans)
        return _Board(path, file, text, cards)

    def _read_archive(self, path: Path) -> list[_Card]:
        """The archived cards of a board, newest first."""
        cards = [self._read_card(folder) for folder in self._card_folders(path / "archive")]
        oldest = datetime.min.replace(tzinfo=_now().tzinfo)
        return sorted(cards, key=lambda card: card.file.archived or oldest, reverse=True)

    # Boards

    def check_layout(self) -> None:
        """Raise OldFormatError when the todos folder still has the Markdown board files."""
        todos = self.config.todos_dir
        old = [todos / "inbox.md"] if (todos / "inbox.md").is_file() else []
        if (todos / "projects").is_dir():
            old.extend(sorted((todos / "projects").glob("*.md")))
        if not old:
            return
        if self.boards_dir.is_dir():
            raise OldFormatError(
                f"{old[0]} is a board file of the old format, but {self.boards_dir} exists. "
                "An old herdboard copy wrote it after the conversion, or convert-store could not remove it. "
                "Move the cards that are not on the boards by hand, then remove it"
            )
        raise OldFormatError(f"{todos} has boards in the old format. Run `herdboard convert-store` to convert them")

    def board_paths(self) -> list[Path]:
        self.check_layout()
        if not self.boards_dir.is_dir():
            return []
        paths = sorted(path.parent for path in self.boards_dir.glob(f"*/{BOARD_FILE}"))
        return sorted(paths, key=lambda path: path.name != INBOX)

    def board_name(self, path: Path) -> str:
        return path.name

    def resolve_board(self, name: str) -> Path:
        if "/" in name or name in ("", ".", ".."):
            raise NotFoundError(f"No board named {name!r}")
        self.check_layout()
        path = self.boards_dir / name
        if not (path / BOARD_FILE).is_file():
            raise NotFoundError(f"No board named {name!r}")
        return path

    def _create_board(self, path: Path, repo: str | None = None, merge: str | None = None) -> BoardFile:
        """Write the `board.toml` of a new board. The caller holds the lock and checked that it is absent."""
        file = card_file.new_board(self.config.columns, repo=repo, merge=merge)
        self._write_file(path / BOARD_FILE, card_file.dump_board(file).encode("utf-8"))
        return file

    def ensure_inbox(self) -> Path:
        self.check_layout()
        with self._lock():
            if not (self.inbox_path / BOARD_FILE).exists():
                self._create_board(self.inbox_path)
        return self.inbox_path

    def init_board(
        self,
        identity: RepoIdentity,
        merge: str | None = None,
        links: list[ProjectLink] = (),
        *,
        exist_ok: bool = False,
        on_existing: Callable[[Path], None] | None = None,
    ) -> Path:
        """Create a configured board in one write. With `exist_ok`, keep an existing board unchanged.

        `on_existing` reports that the existing board kept its settings instead of the supplied ones.
        """
        if merge is not None and merge not in MERGE_MODES:
            raise HerdboardError("merge must be local or pr")
        self.check_layout()
        with self._lock():
            existing = find_board(self.boards_dir, identity)
            if existing is not None:
                if exist_ok:
                    if on_existing is not None:
                        on_existing(existing)
                    return existing
                raise HerdboardError(f"{identity.identity} already has a board: {existing}")
            path = new_board_path(self.boards_dir, identity)
            file = card_file.new_board(self.config.columns, repo=identity.identity, merge=merge)
            file.set_tables("linear-projects", [link.to_dict() for link in links])
            self._write_file(path / BOARD_FILE, card_file.dump_board(file).encode("utf-8"))
        return path

    def _board_file(self, path: Path) -> BoardFile:
        board_path = path / BOARD_FILE
        return card_file.parse_board(self._read_text(board_path, "board"), board_path)

    def board_meta(self, path: Path) -> BoardMeta:
        try:
            file = self._board_file(path)
        except FormatError as error:
            return BoardMeta(None, [], str(error))
        merge = file.merge
        if merge not in (None, *MERGE_MODES):
            return BoardMeta(file.repo, [], f"{path}: merge must be local or pr")
        try:
            return BoardMeta(
                file.repo,
                parse_links(file.tables("linear-projects")),
                merge=merge,
                pins=parse_pins(file.tables("linear-issues")),
            )
        except FormatError as error:
            return BoardMeta(file.repo, [], f"{path}: {error}", merge=merge)

    def update_links(self, path: Path, change: Callable[[list[ProjectLink]], list[ProjectLink]]) -> list[ProjectLink]:
        """Rewrite the `linear-projects` tables of a board with `change(links)`."""
        result: list[ProjectLink] = []

        def keep(links):
            result[:] = change(links)
            return result

        self.update_config(path, change_links=keep)
        return result

    def update_pins(self, path: Path, change: Callable[[list[IssuePin]], list[IssuePin]]) -> list[IssuePin]:
        """Rewrite the `linear-issues` tables of a board with `change(pins)`."""
        result: list[IssuePin] = []

        def keep(pins):
            result[:] = change(pins)
            return result

        self.update_config(path, change_pins=keep)
        return result

    def set_merge(self, path: Path, merge: str | None) -> None:
        """Set the `merge` setting of a board, or remove it for None (the config default)."""
        self.update_config(path, merge=merge)

    def update_config(
        self,
        path: Path,
        merge: str | None | object = KEEP,
        change_links: Callable[[list[ProjectLink]], list[ProjectLink]] | None = None,
        change_pins: Callable[[list[IssuePin]], list[IssuePin]] | None = None,
    ) -> None:
        """Set the `merge` setting (None removes it) and rewrite the links with `change_links`
        and the pinned issues with `change_pins`.

        `KEEP` leaves the merge mode, and None for `change_links` or `change_pins` leaves the links
        or the pins. All changes apply in memory, then go to `board.toml` in one write, so a
        failed one writes nothing.
        """
        if merge is not KEEP and merge not in (None, *MERGE_MODES):
            raise HerdboardError(f"merge must be local or pr, not {merge!r}")
        with self._lock():
            board = self._read(path)
            file = board.file
            try:
                links = parse_links(file.tables("linear-projects"))
                pins = parse_pins(file.tables("linear-issues"))
            except FormatError as error:
                raise FormatError(f"{path}: {error}. The board is read only") from error
            if change_links is not None:
                file.set_tables("linear-projects", [link.to_dict() for link in change_links(links)])
            if change_pins is not None:
                file.set_tables("linear-issues", [pin.to_dict() for pin in change_pins(pins)])
            if merge is not KEEP:
                file.merge = merge
            self._write_board(board)

    def import_target(self, identity: RepoIdentity | None = None, board: str | None = None) -> ImportTarget:
        """The board that `migrate` writes to: `board`, else the one of `identity`, else the inbox."""
        self.check_layout()
        if board is not None and board != INBOX:
            path, found = self.resolve_board(board), True
        elif board is None and identity is not None:
            path = find_board(self.boards_dir, identity)
            found = path is not None
            path = path or new_board_path(self.boards_dir, identity)
        else:
            path = self.inbox_path
            found = (path / BOARD_FILE).is_file()
        if not found:
            columns = self.config.columns
            done = next((t for t in columns if t.casefold() == "done"), None)
            return ImportTarget(path, identity if board is None else None, False, columns[0], done)
        file = self._board_file(path)
        if not file.columns:
            raise HerdboardError(f"{path}: board has no columns")
        try:
            done_title, error = self._done_column(file, path).name, None
        except HerdboardError as problem:
            done_title, error = None, str(problem)
        return ImportTarget(
            path, identity if board is None else None, True, self._default_column(file, path).name, done_title, error
        )

    def import_cards(self, items: list[Item], path: Path | None = None, identity: RepoIdentity | None = None) -> Path:
        """Add converted cards at the bottom of their columns, under one lock.

        With `path`, the board must exist (the inbox is created). With `identity`, the board
        of that repository is created like `init` when it is absent. With both, the board at
        `path` must still belong to that repository, else the write is a conflict.
        """
        self.check_layout()
        with self._lock():
            if path is None:
                path = find_board(self.boards_dir, identity)
                if path is None:
                    path = new_board_path(self.boards_dir, identity)
                    self._create_board(path, repo=identity.identity)
            elif not (path / BOARD_FILE).exists() and path == self.inbox_path:
                self._create_board(path)
            elif not (path / BOARD_FILE).is_file():
                raise NotFoundError(f"No board: {path}")
            board = self._read(path)
            if identity is not None and not same_repo(board.file.repo, identity):
                raise ConflictError(f"{path}: board no longer belongs to {identity.identity}; retry")
            if not board.file.columns:
                raise HerdboardError(f"{path}: board has no columns")
            done = self._done_column(board.file, path) if any(i.checked for i in items) else None
            open_column = self._default_column(board.file, path)
            taken = self._taken_ids()
            for item in items:
                body = [line[1:] if line.startswith("\t") else line.lstrip(" ") for line in item.body]
                card = self._new_card(path, self._new_id(taken), item.title, [item.tag] if item.tag else [], body=body)
                self._write_card(card)
                board.cards[card.id] = card
                (done if item.checked else open_column).cards.append(card.id)
            self._write_board(board)
        return path

    def board_for_repo(self, identity: RepoIdentity) -> Path | None:
        self.check_layout()
        return find_board(self.boards_dir, identity)

    # Reading

    def _view(self, board_path: Path, card: _Card, column: ColumnFile | None, done: bool = False) -> CardView:
        file = card.file
        return CardView(
            ref=card.id,
            board=self.board_name(board_path),
            path=board_path,
            column=column.name if column is not None else "Archive",
            title=file.title,
            checked=done,
            due=file.due,
            tags=file.tags,
            body=list(file.body),
            column_limit=column.limit if column is not None else None,
            type=file.type,
            parent=file.parent,
            archived=column is None,
            promoted=file.promoted,
        )

    def _board_view(self, board: _Board, card_id: str) -> CardView:
        column = board.column_of(card_id)
        done = self._done_or_none(board.file)
        return self._view(board.path, board.cards[card_id], column, column is done)

    def _done_or_none(self, file: BoardFile) -> ColumnFile | None:
        try:
            return self._done_column(file, None)
        except HerdboardError:
            return None

    def _views(self, board: _Board) -> list[CardView]:
        return [self._board_view(board, card_id) for card_id in board.file.card_ids()]

    def list_cards(self, paths: list[Path] | None = None) -> list[CardView]:
        views = []
        for path in self.board_paths() if paths is None else paths:
            views.extend(self._views(self._read(path)))
        return views

    def archived_cards(self, paths: list[Path] | None = None) -> list[CardView]:
        """The archived cards of the boards, newest first on each board."""
        views = []
        for path in self.board_paths() if paths is None else paths:
            views.extend(self._view(path, card, None) for card in self._read_archive(path))
        return views

    def snapshot(self, path: Path, archive: bool = False) -> BoardSnapshot:
        """One read of a board. With `archive`, the archived cards too."""
        board = self._read(path)
        done = self._done_or_none(board.file)
        columns = [
            ColumnSnapshot(
                column.name,
                column.limit,
                column is done,
                [self._view(path, board.cards[card_id], column, column is done) for card_id in column.cards],
            )
            for column in board.file.columns
        ]
        archived = None
        if archive:
            archived = ColumnSnapshot(
                "Archive", None, False, [self._view(path, c, None) for c in self._read_archive(path)]
            )
        return BoardSnapshot(self.board_name(path), path, columns, archived)

    def _places(self, ref: str, folder: str) -> list[Path]:
        """The card folders named `ref` under `folder` (`cards` or `archive`) of every board."""
        if not is_card_id(ref):
            return []
        return [path / folder / ref for path in self.board_paths() if (path / folder / ref / CARD_FILE).is_file()]

    def _check_unique(self, ref: str) -> None:
        """Raise when a card ID names more than one card folder, archived cards included."""
        places = self._places(ref, "cards") + self._places(ref, "archive")
        if len(places) > 1:
            raise DuplicateIdError(f"ID {ref} is on two cards: {' and '.join(str(p) for p in places)}")

    def card_dir(self, ref: str) -> Path | None:
        """The folder of the todo `ref`, on the board or in an archive, or None.

        A card folder moves on archive, unarchive and move to another board: hold the lock
        from this call until the folder is used.
        """
        places = self._places(ref, "cards") + self._places(ref, "archive")
        return places[0] if places else None

    def _resolve(self, ref: str) -> _Located:
        places = self._places(ref, "cards")
        if not places:
            if self._places(ref, "archive"):
                raise ArchivedError(f"{ref} is archived. Run `herdboard unarchive {ref}` first")
            raise NotFoundError(f"No card {ref}")
        self._check_unique(ref)
        path = places[0].parent.parent
        board = self._read(path)
        card = board.cards[ref]
        return _Located(path, ref, board.column_of(ref).name, card.text)

    def _find_archived(self, ref: str) -> Path | None:
        """The board whose archive has the card `ref`, or None."""
        places = self._places(ref, "archive")
        return places[0].parent.parent if places else None

    def notes_for(self, card: CardView) -> list[dict] | None:
        """Notes that a card links to. Cards no longer link to notes, so there are none."""
        return None

    def show(self, ref: str, archived: bool = False) -> CardView:
        """A card. With `archived`, also a card from an archive; else an archived card raises."""
        try:
            located = self._resolve(ref)
        except ArchivedError:
            if not archived:
                raise
            self._check_unique(ref)
            path = self._find_archived(ref)
            if path is None:
                raise NotFoundError(f"No card {ref}") from None
            return self._view(path, self._read_card(path / "archive" / ref), None)
        return self._board_view(self._read(located.path), ref)

    # Writing

    @contextmanager
    def _lock(self) -> Iterator[None]:
        """Hold the store-wide lock. Every write takes it for its whole read-check-write."""
        try:
            self.lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self.lock_path, "a")
        except OSError as error:
            raise IoError(f"{self.lock_path}: cannot open lock: {error}") from error
        deadline = time.monotonic() + self.lock_timeout
        try:
            while True:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise ConflictError(f"{self.config.todos_dir}: boards are locked by another writer") from None
                    time.sleep(0.01)
            yield
        finally:
            handle.close()

    def _find(self, board: _Board, located: _Located) -> _Card:
        """Find the card again in a fresh read; its text and its column must be unchanged."""
        card = board.cards.get(located.block_id)
        column = board.file.column_of(located.block_id)
        if card is None or card.text != located.text or column is None or column.name != located.column:
            raise ConflictError(f"{located.path}: card changed on disk; retry")
        return card

    def _taken_ids(self) -> set[str]:
        """Every card folder name, on the boards and in the archives."""
        taken: set[str] = set()
        for path in self.board_paths():
            for folder in ("cards", "archive"):
                try:
                    taken.update(p.name for p in (path / folder).iterdir())
                except FileNotFoundError:
                    pass
                except OSError as error:
                    raise IoError(f"{path / folder}: cannot read: {error}") from error
        return taken

    def _new_id(self, taken: set[str]) -> str:
        while True:
            candidate = "t" + "".join(secrets.choice(ID_ALPHABET) for _ in range(3))
            if candidate not in taken:
                taken.add(candidate)
                return candidate

    def _new_card(
        self,
        path: Path,
        card_id: str,
        title: str,
        tags: list[str] = (),
        *,
        type: str | None = None,
        due: str | None = None,
        parent: str | None = None,
        body: list[str] = (),
    ) -> _Card:
        file = card_file.new_card(
            title, created=_now(), type=type, tags=list(tags), due=due, parent=parent, body=list(body)
        )
        return _Card(card_id, path / "cards" / card_id, file, "")

    def _modify(
        self,
        ref: str,
        change: Callable[[_Board, _Card, ColumnFile], None],
        located: _Located | None = None,
    ) -> CardView:
        """Apply `change` to a card and its board under the lock.

        Only the files whose text changed are written: the card first, then the board.
        """
        located = located or self._resolve(ref)
        path = located.path
        with self._lock():
            board = self._read(path)
            card = self._find(board, located)
            self._check_unique(card.id)
            card_before = card_file.dump_card(card.file)
            change(board, card, board.column_of(card.id))
            if card_file.dump_card(card.file) != card_before:
                self._write_card(card)
            if card_file.dump_board(board.file) != board.text:
                self._write_board(board)
            return self._board_view(board, card.id)

    def _default_column(self, file: BoardFile, path: Path) -> ColumnFile:
        """The column for a card that names none: the first one that is not the triage column."""
        column = next((c for c in file.columns if not is_triage(c.name)), None)
        if column is None:
            if file.columns:
                raise HerdboardError(f"{path}: board has no column apart from {TRIAGE_COLUMN}")
            raise HerdboardError(f"{path}: board has no columns")
        return column

    def _target_column(self, file: BoardFile, title: str) -> ColumnFile:
        """The column named `title`. The triage column is added first on a board that has none.

        Only the triage column is made on demand; any other unknown name raises.
        """
        column = file.column(title)
        if column is not None:
            return column
        if not is_triage(title):
            raise NotFoundError(f"No column named {title!r}")
        column = ColumnFile(TRIAGE_COLUMN)
        file.columns.insert(0, column)
        return column

    def _done_column(self, file: BoardFile, path: Path | None) -> ColumnFile:
        """The column marked `done = true`, else the column named Done."""
        column = next((c for c in file.columns if c.done), None) or file.column("done")
        if column is None:
            name = self.board_name(path) if path is not None else "the board"
            raise HerdboardError(f"Board {name} has no done column: add `done = true` to a column or name one Done")
        return column

    def _move_card(self, card_id: str, source: ColumnFile, target: ColumnFile, position: str) -> None:
        source.cards.remove(card_id)
        _insert(target.cards, card_id, position)

    def move(self, ref: str, column: str, position: str = "bottom") -> CardView:
        def change(board, card, source):
            self._move_card(card.id, source, self._target_column(board.file, column), position)

        return self._modify(ref, change)

    def done(self, ref: str) -> CardView:
        def change(board, card, source):
            self._move_card(card.id, source, self._done_column(board.file, board.path), "bottom")

        return self._modify(ref, change)

    def toggle_item(self, ref: str, index: int, expected: CardView | None = None) -> CardView:
        """Check or clear the checklist item on body line `index` (0 is the first body line)."""

        def change(board, card, column):
            body = card.file.body
            if expected is not None and (card.file.title != expected.title or body != expected.body):
                raise ConflictError(f"{card.path}: card changed on disk since you opened it; open it again")
            line = body[index] if 0 <= index < len(body) else ""
            match = CHECK_LINE.match(line)
            if match is None:
                raise HerdboardError(f"Detail line {index + 1} of {card.id} is not a checklist item")
            mark = " " if match.group("mark") != " " else "x"
            body[index] = f"{match.group('start')}{mark}{line[match.end('mark') :]}"

        return self._modify(ref, change)

    def shift(self, ref: str, step: int) -> CardView:
        """Swap a card with the card above (step -1) or below (step 1) in its column.

        At the top or bottom of the column the card stays where it is.
        """

        def change(board, card, column):
            here = column.cards.index(card.id)
            there = here + step
            if 0 <= there < len(column.cards):
                column.cards[here], column.cards[there] = column.cards[there], column.cards[here]

        return self._modify(ref, change)

    def move_to_board(
        self,
        ref: str,
        target: Path,
        column: str | None = None,
        blocked: Callable[[str], str | None] | None = None,
    ) -> CardView:
        """Move a todo, with its folder (log and attachments too), to another board.

        Without `column`, the card keeps its column name when the target board has
        it, else it goes to the target's default column. A card that leaves the
        triage column lands in the target's triage column, which that board gains
        when it has none. A sub-card loses its parent, which stays behind.

        `blocked` names why the card must stay, such as a running agent that holds the
        folder path, or returns None. The folder moves first, then the target board is
        written, then the source board: a crash between them leaves an orphan on the
        target board, which shows there.
        """
        located = self._resolve(ref)
        source = located.path
        if Path(target).resolve() == Path(source).resolve():
            raise HerdboardError(f"The card is already on {self.board_name(source)}")
        if not (Path(target) / BOARD_FILE).is_file():
            raise NotFoundError(f"No board: {target}")
        reason = blocked(ref) if blocked is not None else None
        if reason:
            raise CardActiveError(f"Cannot move {ref} to another board: {reason}")
        with self._lock():
            board = self._read(source)
            card = self._find(board, located)
            self._check_unique(card.id)
            if board.children(card.id):
                raise ParentError(
                    f"Cannot move {card.id}: it has sub-cards on the board. Move them first", "has_sub_cards"
                )
            old_column = board.column_of(card.id)
            destination = self._read(target)
            if column is not None:
                new_column = self._target_column(destination.file, column)
            elif is_triage(old_column.name):
                new_column = self._target_column(destination.file, TRIAGE_COLUMN)
            else:
                new_column = destination.file.column(old_column.name) or self._default_column(destination.file, target)
            folder = target / "cards" / card.id
            self._rename(card.folder, folder)
            moved = _Card(card.id, folder, card.file, card.text)
            if moved.file.parent is not None:
                moved.file.parent = None
                self._write_card(moved)
            destination.cards[card.id] = moved
            new_column.cards.append(card.id)
            self._write_board(destination)
            old_column.cards.remove(card.id)
            del board.cards[card.id]
            self._write_board(board)
            return self._board_view(destination, card.id)

    def edit(
        self,
        ref: str,
        title: str | None = None,
        body: list[str] | None = None,
        expected: CardView | None = None,
        *,
        tags: list[str] | None = None,
        type: str | None | object = KEEP,
        due: str | None | object = KEEP,
    ) -> CardView:
        """Change a card's title, body, tags, type or due date.

        None for `title`, `body` or `tags`, and `KEEP` for `type` or `due`, leave the
        value. With `expected`, the card must still match it.
        """
        if title is not None:
            title = _check_title(title)
        if due is not KEEP and due is not None:
            card_file.parse_due(due)

        def change(board, card, column):
            file = card.file
            if expected is not None and (
                file.title != expected.title
                or file.body != expected.body
                or file.tags != expected.tags
                or file.type != expected.type
                or file.due != expected.due
                or (column is self._done_or_none(board.file)) != expected.checked
                or column.name != expected.column
            ):
                raise ConflictError(f"{card.path}: card changed on disk since you opened it; open it again")
            if title is not None:
                file.title = title
            if body is not None:
                file.body = card_file.body_lines(body)
            if tags is not None:
                file.tags = list(dict.fromkeys(tags))
            if type is not KEEP:
                file.type = type
            if due is not KEEP:
                file.due = due

        return self._modify(ref, change)

    def delete(self, ref: str, blocked: Callable[[str], str | None] | None = None) -> CardView:
        """Remove a card with its folder: its log and attachments too. Returns the card as it was.

        `blocked` names why the card must stay, such as a running agent that writes to the
        folder, or returns None. The ID leaves `board.toml` first, then the folder goes: a
        crash between them leaves an orphan, which shows again.
        """
        located = self._resolve(ref)
        reason = blocked(ref) if blocked is not None else None
        if reason:
            raise CardActiveError(f"Cannot delete {ref}: {reason}")
        with self._lock():
            board = self._read(located.path)
            card = self._find(board, located)
            self._check_unique(card.id)
            view = self._board_view(board, card.id)
            board.column_of(card.id).cards.remove(card.id)
            self._write_board(board)
            try:
                shutil.rmtree(card.folder)
            except OSError as error:
                raise IoError(f"{card.folder}: cannot remove: {error}") from error
        return view

    def _archive_one(self, board: _Board, card: _Card, when: datetime) -> _Card:
        """Move a card folder to the archive and stamp it. The caller writes `board.toml` after."""
        folder = board.path / "archive" / card.id
        self._rename(card.folder, folder)
        archived = _Card(card.id, folder, card.file, card.text)
        archived.file.archived = when
        self._write_card(archived)
        board.column_of(card.id).cards.remove(card.id)
        del board.cards[card.id]
        return archived

    def archive(self, ref: str, blocked: Callable[[str], str | None] | None = None) -> CardView:
        """Move a todo's folder to its board's archive.

        `blocked` names why a card must stay on the board, such as a worktree that still
        exists, or returns None. It runs before the lock, because it can ask Herdr.
        """
        located = self._resolve(ref)
        reason = blocked(ref) if blocked is not None else None
        if reason:
            raise CardActiveError(f"Cannot archive {ref}: {reason}")
        with self._lock():
            board = self._read(located.path)
            card = self._find(board, located)
            self._check_unique(card.id)
            if board.children(card.id):
                raise ParentError(
                    f"Cannot archive {card.id}: it has sub-cards on the board. Archive them first",
                    "has_sub_cards",
                )
            archived = self._archive_one(board, card, _now())
            self._write_board(board)
        return self._view(located.path, archived, None)

    def done_cards(self, path: Path) -> list[CardView]:
        """The cards in the board's done column."""
        board = self._read(path)
        done = self._done_column(board.file, path)
        return [self._view(path, board.cards[card_id], done, True) for card_id in done.cards]

    def archive_done(
        self,
        path: Path,
        blocked: Callable[[str], str | None] | None = None,
    ) -> tuple[list[CardView], list[tuple[CardView, str]]]:
        """Archive every card in the board's done column.

        A card stays when `blocked` gives a reason, or when it is a parent whose sub-cards
        stay on the board. Returns the archived cards, and the cards that stay with the reason.
        """
        first = self._read(path)
        done = self._done_column(first.file, path)
        skipped: list[tuple[CardView, str]] = []
        chosen: dict[str, CardView] = {}
        for card_id in done.cards:
            view = self._view(path, first.cards[card_id], done, True)
            reason = blocked(card_id) if blocked is not None else None
            if reason:
                skipped.append((view, reason))
            else:
                chosen[card_id] = view
        if not chosen:
            return [], skipped
        with self._lock():
            board = self._read(path)
            done = self._done_column(board.file, path)
            for card_id in chosen:
                card = board.cards.get(card_id)
                if card_id not in done.cards or card is None or card.text != first.cards[card_id].text:
                    raise ConflictError(f"{path}: {card_id} changed on disk; retry")
            moving = set(chosen)
            staying = {board.cards[c].file.parent for c in board.file.card_ids() if c not in moving}
            matches = []
            for card_id, view in chosen.items():
                if card_id in staying:
                    skipped.append((view, "it has sub-cards that stay on the board"))
                else:
                    matches.append(card_id)
            if not matches:
                return [], skipped
            when = _now()
            # One second apart, so the archive lists the cards newest first in their column order.
            archived = [
                self._archive_one(board, board.cards[card_id], when - timedelta(seconds=index))
                for index, card_id in enumerate(matches)
            ]
            self._write_board(board)
        return [self._view(path, card, None) for card in archived], skipped

    def archived_parent(self, ref: str) -> CardView | None:
        """The parent of an archived sub-card, when the parent is archived too."""
        path = self._find_archived(ref)
        if path is None:
            raise NotFoundError(f"No archived card {ref}")
        parent = self._read_card(path / "archive" / ref).file.parent
        if parent is None or not (path / "archive" / parent / CARD_FILE).is_file():
            return None
        return self._view(path, self._read_card(path / "archive" / parent), None)

    def unarchive(self, ref: str, with_parent: bool = False) -> CardView:
        """Move an archived todo's folder back to its board, into the done column.

        A sub-card whose parent is archived too needs `with_parent`, which moves the parent
        back first. Each folder moves before `board.toml` is written: a crash between them
        leaves an orphan, which shows in the first column.
        """
        path = self._find_archived(ref)
        if path is None:
            raise NotFoundError(f"No archived card {ref}")
        with self._lock():
            if not (path / "archive" / ref / CARD_FILE).is_file():
                raise ConflictError(f"{path}: card changed on disk; retry")
            self._check_unique(ref)
            card = self._read_card(path / "archive" / ref)
            parent_id = card.file.parent
            parent_folder = path / "archive" / parent_id if parent_id else None
            parent_archived = parent_folder is not None and (parent_folder / CARD_FILE).is_file()
            if parent_archived and not with_parent:
                raise ParentError(
                    f"The parent {parent_id} of {ref} is archived too. Unarchive the parent with it", "parent_archived"
                )
            board = self._read(path)
            done = self._done_column(board.file, path)
            order = ([self._read_card(parent_folder)] if parent_archived else []) + [card]
            for one in order:
                folder = path / "cards" / one.id
                self._rename(one.folder, folder)
                back = _Card(one.id, folder, one.file, one.text)
                back.file.archived = None
                self._write_card(back)
                board.cards[one.id] = back
                for column in board.file.columns:
                    if one.id in column.cards:
                        column.cards.remove(one.id)
                done.cards.append(one.id)
            self._write_board(board)
            return self._board_view(board, ref)

    def _check_parent(self, board: _Board, parent: str, child: str | None) -> None:
        """Refuse a parent that is not on this board, that has a parent, or whose child has children."""
        if parent == child:
            raise ParentError(f"{parent} cannot be its own parent", "usage")
        found = board.cards.get(parent)
        if found is None:
            raise ParentError(
                f"No card {parent} on board {self.board_name(board.path)}; a parent must be on the same board",
                "parent_missing",
            )
        if found.file.parent:
            raise ParentError(f"{parent} is a sub-card; sub-cards go one level deep", "parent_nested")
        if child is not None and board.children(child):
            raise ParentError(f"{child} has sub-cards, so it cannot be a sub-card", "parent_nested")

    def set_parent(self, ref: str, parent: str | None) -> CardView:
        """Set or remove a todo's parent."""

        def change(board, card, column):
            if parent is not None:
                self._check_parent(board, parent, card.id)
            card.file.parent = parent

        return self._modify(ref, change)

    def children(self, ref: str) -> list[CardView]:
        """The sub-cards of a todo, in board order."""
        card = self.show(ref)
        return [view for view in self.list_cards([card.path]) if view.parent == card.ref]

    def ensure_id(self, ref: str, archived: bool = False) -> CardView:
        """The card. Every card has its ID from the start, so this only reads it."""
        return self.show(ref, archived)

    def todo_snapshot(self, ref: str) -> TodoSnapshot:
        """Read a todo for `promote`. Fails when its board has no done column."""
        located = self._resolve(ref)
        board = self._read(located.path)
        card = self._find(board, located)
        done = self._done_column(board.file, located.path)
        view = self._board_view(board, card.id)
        return TodoSnapshot(located, view, board.column_of(card.id) is done)

    def finish_promotion(self, snapshot: TodoSnapshot, identifier: str) -> CardView:
        """Set the todo's `promoted` field and move it to the done column.

        Fails with a conflict when the todo changed since `snapshot`.
        """

        def change(board, card, source):
            card.file.promoted = identifier
            target = self._done_column(board.file, board.path)
            if target is not source:
                self._move_card(card.id, source, target, "bottom")

        return self._modify(snapshot.located.block_id, change, snapshot.located)

    def add(
        self,
        path: Path,
        text: str,
        column: str | None = None,
        position: str = "bottom",
        body: list[str] | None = None,
        parent: str | None = None,
        *,
        tags: list[str] = (),
        type: str | None = None,
        due: str | None = None,
    ) -> CardView:
        """Add a card. Its `card.md` is written first, then `board.toml`: a crash between
        them leaves an orphan, which shows in the first column."""
        text = _check_title(text)
        if due is not None:
            card_file.parse_due(due)
        if not (path / BOARD_FILE).is_file():
            raise NotFoundError(f"No board: {path}")
        with self._lock():
            board = self._read(path)
            if column is None:
                target = self._default_column(board.file, path)
            else:
                target = self._target_column(board.file, column)
            if parent is not None:
                self._check_parent(board, parent, None)
            card = self._new_card(
                path,
                self._new_id(self._taken_ids()),
                text,
                list(dict.fromkeys(tags)),
                type=type,
                due=due,
                parent=parent,
                body=body or [],
            )
            self._write_card(card)
            board.cards[card.id] = card
            _insert(target.cards, card.id, position)
            self._write_board(board)
            return self._board_view(board, card.id)


def _check_title(title: str) -> str:
    try:
        return card_file.check_title(title)
    except FormatError as error:
        raise HerdboardError(str(error)) from None


# A checklist item in a card's body: `- [ ] text`, at any indent.
CHECK_LINE = re.compile(r"^(?P<start>\s*[-*+] \[)(?P<mark>.)\] ")
