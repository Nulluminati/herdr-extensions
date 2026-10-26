"""Read and write boards: card references, IDs, and locked atomic writes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
import fcntl
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time
from typing import Callable, Iterator

from . import attachments, kanban_md
from .activity import log_path
from .migrate_convert import Item
from .linear_links import ProjectLink, parse_links, replace_links
from .linear_pins import IssuePin, parse_pins, replace_pins
from .config import MERGE_MODES, Config
from .identity import RepoIdentity, find_board, new_board_path, same_repo
from .model import (
    BLOCK_ID,
    PARENT_LINK,
    TRIAGE_COLUMN,
    ArchivedError,
    Board,
    Card,
    CardActiveError,
    Column,
    ConflictError,
    DuplicateIdError,
    FormatError,
    HerdboardError,
    IoError,
    NotFoundError,
    ParentError,
    card_type,
    is_triage,
    parent_of,
    plain_tags,
    with_parent,
)

ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
TEMP_REF = re.compile(r"^(?P<board>.+):(?P<number>\d+)$")
# An archive card without an ID: its board and its place in the archive.
ARCHIVE_REF = re.compile(r"^(?P<board>.+):archive-(?P<number>\d+)$")
NOTE_LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")


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
    # The card's type: a todo's `#type/<name>` tag, or a Linear label that names a configured type.
    type: str | None = None
    # Linear cards fill the fields below; todos keep the defaults.
    source: str = "todo"
    url: str | None = None
    state: str | None = None
    team: str | None = None
    pr: bool = False
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
    # The card ID of a sub-card's parent, from its `[[#^id]]` link.
    parent: str | None = None
    # Board-only: a Linear card's issue ID, which stays when the identifier changes.
    issue_id: str | None = None
    # Board-only family status: the parent's title, and a parent's (done, total) sub-cards.
    parent_title: str | None = None
    sub_cards: tuple[int, int] | None = None
    # True for a card in the board's archive section.
    archived: bool = False


# `Store.update_config` leaves the merge mode as it is.
KEEP = object()


@dataclass
class BoardMeta:
    """What Linear views need from a board: its repository, project links and pinned issues."""

    repo: str | None
    links: list[ProjectLink]
    error: str | None = None
    # The board's `merge:` frontmatter: "local", "pr" or None for the config default.
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
    """One read of a board: its name, path and columns in file order."""

    name: str
    path: Path
    columns: list[ColumnSnapshot]
    # The archive section, or None when the board has none.
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
class _Located:
    """A card found without the lock. `number` is set for a temporary ref."""

    path: Path
    block_id: str | None
    number: int | None
    column: str
    lines: list[str]


@dataclass
class TodoSnapshot:
    """A todo as `promote` read it. The final update checks that it is still the same."""

    located: _Located
    view: CardView
    # True when the todo is in its board's done column.
    done: bool


def _all_ids(board: Board) -> set[str]:
    cards = [card for _, card in board.all_cards()]
    if board.archive is not None:
        cards.extend(board.archive.cards)
    return {card.block_id for card in cards if card.block_id}


class Store:
    def __init__(self, config: Config, lock_timeout: float = 5.0):
        self.config = config
        self.lock_timeout = lock_timeout
        self.lock_path = config.todos_dir / ".herdboard.lock"
        self.inbox_path = config.todos_dir / "inbox.md"
        self.projects_dir = config.todos_dir / "projects"

    # Files

    def _read(self, path: Path) -> Board:
        try:
            text = path.read_bytes().decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise IoError(f"{path}: cannot read board: {error}") from error
        return kanban_md.parse(text, tuple(self.config.archive_titles))

    def _write_file(self, path: Path, data: bytes, mode: int) -> None:
        """Write to a temporary file beside `path`, then rename it over `path`."""
        try:
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
            raise IoError(f"{path}: cannot write board: {error}") from error

    def _write(self, path: Path, board: Board) -> None:
        data = kanban_md.serialize(board).encode("utf-8")
        try:
            mode = path.stat().st_mode & 0o7777
        except OSError as error:
            raise IoError(f"{path}: cannot write board: {error}") from error
        self._write_file(path, data, mode)

    def _create(self, path: Path, text: str) -> None:
        """Create a new board. The caller holds the lock and checked that it is absent."""
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as error:
            raise IoError(f"{path}: cannot create board: {error}") from error
        umask = os.umask(0)
        os.umask(umask)
        self._write_file(path, text.encode("utf-8"), 0o666 & ~umask)

    # Boards

    def board_paths(self) -> list[Path]:
        paths = [self.inbox_path] if self.inbox_path.is_file() else []
        if self.projects_dir.is_dir():
            paths.extend(sorted(self.projects_dir.glob("*.md")))
        return paths

    def board_name(self, path: Path) -> str:
        if path.parent == self.inbox_path.parent and path.name == self.inbox_path.name:
            return "inbox"
        return path.stem

    def resolve_board(self, name: str) -> Path:
        if name == "inbox":
            path = self.inbox_path
        elif "/" in name or name in ("", ".", ".."):
            raise NotFoundError(f"No board named {name!r}")
        else:
            path = self.projects_dir / f"{name}.md"
        if not path.is_file():
            raise NotFoundError(f"No board named {name!r}")
        return path

    def _new_board_text(self, frontmatter: list[tuple[str, str]] = ()) -> str:
        columns = self.config.columns
        done = next((title for title in columns if title.casefold() == "done"), None)
        return kanban_md.new_board(columns, done, list(frontmatter))

    def ensure_inbox(self) -> Path:
        with self._lock():
            if not self.inbox_path.exists():
                self._create(self.inbox_path, self._new_board_text())
        return self.inbox_path

    def init_board(self, identity: RepoIdentity, merge: str | None = None,
                   links: list[ProjectLink] = (), *, exist_ok: bool = False) -> Path:
        """Create a configured board in one write. With `exist_ok`, keep an existing board unchanged."""
        if merge is not None and merge not in MERGE_MODES:
            raise HerdboardError("merge must be local or pr")
        with self._lock():
            existing = find_board(self.projects_dir, identity)
            if existing is not None:
                if exist_ok:
                    return existing
                raise HerdboardError(f"{identity.identity} already has a board: {existing}")
            path = new_board_path(self.projects_dir, identity)
            frontmatter = [("repo", identity.identity)]
            if merge is not None:
                frontmatter.append(("merge", merge))
            text = self._new_board_text(frontmatter)
            if links:
                text = replace_links(text, links)
            self._create(path, text)
        return path

    def board_meta(self, path: Path) -> BoardMeta:
        board = self._read(path)
        merge = board.frontmatter_value("merge")
        if merge not in (None, *MERGE_MODES):
            return BoardMeta(board.frontmatter_value("repo"), [], f"{path}: merge must be local or pr")
        try:
            return BoardMeta(board.frontmatter_value("repo"), parse_links(board.frontmatter), merge=merge,
                             pins=parse_pins(board.frontmatter))
        except FormatError as error:
            return BoardMeta(board.frontmatter_value("repo"), [], f"{path}: {error}", merge=merge)

    def update_links(
        self, path: Path, change: Callable[[list[ProjectLink]], list[ProjectLink]]
    ) -> list[ProjectLink]:
        """Rewrite the `linear-projects` frontmatter of a board with `change(links)`."""
        with self._lock():
            try:
                text = path.read_bytes().decode("utf-8")
                mode = path.stat().st_mode & 0o7777
            except (OSError, UnicodeDecodeError) as error:
                raise IoError(f"{path}: cannot read board: {error}") from error
            board = kanban_md.parse(text, tuple(self.config.archive_titles))
            self._require_writable(path, board)
            links = change(parse_links(board.frontmatter))
            self._write_file(path, replace_links(text, links).encode("utf-8"), mode)
        return links

    def update_pins(
        self, path: Path, change: Callable[[list[IssuePin]], list[IssuePin]]
    ) -> list[IssuePin]:
        """Rewrite the `linear-issues` frontmatter of a board with `change(pins)`."""
        with self._lock():
            try:
                text = path.read_bytes().decode("utf-8")
                mode = path.stat().st_mode & 0o7777
            except (OSError, UnicodeDecodeError) as error:
                raise IoError(f"{path}: cannot read board: {error}") from error
            board = kanban_md.parse(text, tuple(self.config.archive_titles))
            self._require_writable(path, board)
            pins = change(parse_pins(board.frontmatter))
            self._write_file(path, replace_pins(text, pins).encode("utf-8"), mode)
        return pins

    def set_merge(self, path: Path, merge: str | None) -> None:
        """Set the `merge:` frontmatter of a board, or remove it for None (the config default)."""
        self.update_config(path, merge=merge)

    def update_config(
        self,
        path: Path,
        merge: str | None | object = KEEP,
        change_links: Callable[[list[ProjectLink]], list[ProjectLink]] | None = None,
        change_pins: Callable[[list[IssuePin]], list[IssuePin]] | None = None,
    ) -> None:
        """Set the `merge:` frontmatter (None removes it) and rewrite the links with `change_links`
        and the pinned issues with `change_pins`.

        `KEEP` leaves the merge mode, and None for `change_links` or `change_pins` leaves the links
        or the pins. All changes apply to the text in memory, then go to the file in one write, so
        a failed one writes nothing.
        """
        if merge is not KEEP and merge not in (None, *MERGE_MODES):
            raise HerdboardError(f"merge must be local or pr, not {merge!r}")
        with self._lock():
            try:
                text = path.read_bytes().decode("utf-8")
                mode = path.stat().st_mode & 0o7777
            except (OSError, UnicodeDecodeError) as error:
                raise IoError(f"{path}: cannot read board: {error}") from error
            board = kanban_md.parse(text, tuple(self.config.archive_titles))
            self._require_writable(path, board)
            if change_links is not None:
                text = replace_links(text, change_links(parse_links(board.frontmatter)))
            if change_pins is not None:
                text = replace_pins(text, change_pins(parse_pins(board.frontmatter)))
            if merge is not KEEP:
                text = kanban_md.set_frontmatter_value(text, "merge", merge)
            written = kanban_md.parse(text, tuple(self.config.archive_titles)).frontmatter
            parse_links(written)
            parse_pins(written)
            self._write_file(path, text.encode("utf-8"), mode)

    def import_target(
        self, identity: RepoIdentity | None = None, board: str | None = None
    ) -> ImportTarget:
        """The board that `migrate` writes to: `board`, else the one of `identity`, else the inbox."""
        if board is not None and board != "inbox":
            path, found = self.resolve_board(board), True
        elif board is None and identity is not None:
            path = find_board(self.projects_dir, identity)
            found = path is not None
            path = path or new_board_path(self.projects_dir, identity)
        else:
            path = self.inbox_path
            found = path.is_file()
        if not found:
            columns = self.config.columns
            done = next((t for t in columns if t.casefold() == "done"), None)
            return ImportTarget(path, identity if board is None else None, False,
                                columns[0], done)
        parsed = self._read(path)
        if not parsed.columns:
            raise HerdboardError(f"{path}: board has no columns")
        try:
            done_title, error = self._done_column(parsed, path).title, None
        except HerdboardError as problem:
            done_title, error = None, str(problem)
        return ImportTarget(path, identity if board is None else None, True,
                            self._default_column(parsed, path).title, done_title, error)

    def import_cards(
        self, items: list[Item], path: Path | None = None, identity: RepoIdentity | None = None
    ) -> Path:
        """Append converted cards at the bottom of their columns, in one locked write.

        With `path`, the board must exist (the inbox is created). With `identity`, the board
        of that repository is created like `init` when it is absent. With both, the board at
        `path` must still belong to that repository, else the write is a conflict.
        """
        with self._lock():
            if path is None:
                path = find_board(self.projects_dir, identity)
                if path is None:
                    path = new_board_path(self.projects_dir, identity)
                    self._create(path, self._new_board_text([("repo", identity.identity)]))
            elif not path.exists() and path == self.inbox_path:
                self._create(path, self._new_board_text())
            elif not path.is_file():
                raise NotFoundError(f"No board file: {path}")
            board = self._read(path)
            self._require_writable(path, board)
            if identity is not None and not same_repo(board.frontmatter_value("repo"), identity):
                raise ConflictError(f"{path}: board no longer belongs to {identity.identity}; retry")
            if not board.columns:
                raise HerdboardError(f"{path}: board has no columns")
            done = self._done_column(board, path) if any(i.checked for i in items) else None
            open_column = self._default_column(board, path)
            for item in items:
                column = done if item.checked else open_column
                kanban_md.insert_card(column, Card(item.card_lines()), "bottom")
            self._assign_ids(path, board)
            self._write(path, board)
        return path

    def board_for_repo(self, identity: RepoIdentity) -> Path | None:
        return find_board(self.projects_dir, identity)

    # Reading

    def _view(self, path: Path, column: Column, card: Card, ref: str) -> CardView:
        return CardView(
            ref=ref,
            board=self.board_name(path),
            path=path,
            column=column.title,
            title=card.title,
            checked=card.checked,
            due=card.due,
            tags=plain_tags(card.tags),
            body=list(card.body),
            column_limit=column.limit,
            type=card_type(card.tags),
            parent=parent_of(card.title),
            archived=column.archive,
        )

    def _archive_views(self, path: Path, board: Board) -> list[CardView]:
        """The archive cards of a board. A card without an ID gets a reference that no command takes."""
        if board.archive is None:
            return []
        name = self.board_name(path)
        return [self._view(path, board.archive, card, card.block_id or f"{name}:archive-{number}")
                for number, card in enumerate(board.archive.cards, start=1)]

    def list_cards(self, paths: list[Path] | None = None) -> list[CardView]:
        views = []
        for path in self.board_paths() if paths is None else paths:
            name = self.board_name(path)
            for number, (column, card) in enumerate(self._read(path).all_cards(), start=1):
                views.append(self._view(path, column, card, card.block_id or f"{name}:{number}"))
        return views

    def archived_cards(self, paths: list[Path] | None = None) -> list[CardView]:
        """The cards in the archive sections of the boards, in file order."""
        views = []
        for path in self.board_paths() if paths is None else paths:
            views.extend(self._archive_views(path, self._read(path)))
        return views

    def snapshot(self, path: Path) -> BoardSnapshot:
        board = self._read(path)
        name = self.board_name(path)
        columns = []
        number = 0
        for column in board.columns:
            cards = []
            for card in column.cards:
                number += 1
                cards.append(self._view(path, column, card, card.block_id or f"{name}:{number}"))
            columns.append(
                ColumnSnapshot(column.title, column.limit, self._is_completed(column), cards)
            )
        archive = None
        if board.archive is not None:
            archive = ColumnSnapshot(board.archive.title, None, False, self._archive_views(path, board))
        return BoardSnapshot(name, path, columns, archive)

    def _check_unique(self, block_id: str) -> None:
        """Raise when a block ID is on more than one card, archive cards included."""
        places = []
        for path in self.board_paths():
            board = self._read(path)
            pairs = board.all_cards()
            if board.archive is not None:
                pairs += [(board.archive, card) for card in board.archive.cards]
            places.extend(
                f"{path} ({column.title})" for column, card in pairs if card.block_id == block_id
            )
        if len(places) > 1:
            raise DuplicateIdError(f"ID {block_id} is on two cards: {' and '.join(places)}")

    def _resolve(self, ref: str) -> _Located:
        temp = TEMP_REF.match(ref)
        if temp:
            path = self.resolve_board(temp.group("board"))
            number = int(temp.group("number"))
            cards = self._read(path).all_cards()
            if not 1 <= number <= len(cards):
                raise NotFoundError(f"No card {ref}")
            column, card = cards[number - 1]
            if card.block_id:
                self._check_unique(card.block_id)
            return _Located(path, None, number, column.title, list(card.lines))
        found = []
        for path in self.board_paths():
            for column, card in self._read(path).all_cards():
                if card.block_id == ref:
                    found.append((path, column, card))
        if not found:
            if self._find_archived(ref) is not None:
                raise ArchivedError(f"{ref} is archived. Run `herdboard unarchive {ref}` first")
            raise NotFoundError(f"No card {ref}")
        self._check_unique(ref)
        path, column, card = found[0]
        return _Located(path, ref, None, column.title, list(card.lines))

    def _find_archived(self, ref: str) -> Path | None:
        """The board whose archive has the card with block ID `ref`, or None."""
        for path in self.board_paths():
            board = self._read(path)
            if board.archive is not None and any(card.block_id == ref for card in board.archive.cards):
                return path
        return None

    def notes_for(self, card: CardView) -> list[dict] | None:
        """Notes that `[[name]]` links in a card point to; None without `vault_root`."""
        vault = self.config.vault_root
        if vault is None:
            return None
        text = PARENT_LINK.sub("", "\n".join([card.title, *card.body]))
        names = list(dict.fromkeys(m.group(1).strip() for m in NOTE_LINK.finditer(text)))
        root = vault.resolve()
        notes = []
        for name in names:
            path = vault / f"{name}.md"
            if not path.is_file():
                matches = sorted(vault.rglob(f"{Path(name).name}.md")) if name else []
                path = matches[0] if matches else path
            content = None
            if path.is_file() and path.resolve().is_relative_to(root):
                try:
                    content = path.read_bytes().decode("utf-8")
                except (OSError, UnicodeDecodeError) as error:
                    raise IoError(f"{path}: cannot read note: {error}") from error
            notes.append({"name": name, "path": str(path), "content": content})
        return notes

    def show(self, ref: str, archived: bool = False) -> CardView:
        """A card. With `archived`, also a card from an archive, found by its ID or its archive reference;
        else an archived card raises."""
        temp = ARCHIVE_REF.match(ref) if archived else None
        if temp:
            path = self.resolve_board(temp.group("board"))
            # One read: a second one can find a shorter archive.
            views = self._archive_views(path, self._read(path))
            number = int(temp.group("number"))
            if not 1 <= number <= len(views):
                raise NotFoundError(f"No archived card {ref}")
            return views[number - 1]
        try:
            located = self._resolve(ref)
        except ArchivedError:
            if not archived:
                raise
            self._check_unique(ref)
            path = self._find_archived(ref)
            views = self._archive_views(path, self._read(path)) if path is not None else []
            # The card can leave the archive between the reads.
            view = next((view for view in views if view.ref == ref), None)
            if view is None:
                raise NotFoundError(f"No card {ref}")
            return view
        column, card = self._find(self._read(located.path), located)
        return self._view(located.path, column, card, card.block_id or ref)

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
                        raise ConflictError(
                            f"{self.config.todos_dir}: boards are locked by another writer"
                        ) from None
                    time.sleep(0.01)
            yield
        finally:
            handle.close()

    def _require_writable(self, path: Path, board: Board) -> None:
        frontmatter = board.frontmatter
        closed = len(frontmatter) >= 2
        if not closed or board.frontmatter_value("kanban-plugin") != "board":
            raise FormatError(
                f"{path}: not a valid board: frontmatter must be closed and contain "
                "'kanban-plugin: board'. The board is read only"
            )
        try:
            parse_links(frontmatter)
            parse_pins(frontmatter)
        except FormatError as error:
            raise FormatError(f"{path}: {error}. The board is read only") from error

    def _find(self, board: Board, located: _Located) -> tuple[Column, Card]:
        """Find the card again in a fresh parse; its lines must be unchanged."""
        cards = board.all_cards()
        if located.number is not None:
            match = cards[located.number - 1] if located.number <= len(cards) else None
        else:
            matches = [pair for pair in cards if pair[1].block_id == located.block_id]
            if len(matches) > 1:
                raise DuplicateIdError(f"ID {located.block_id} is on two cards in {located.path}")
            match = matches[0] if matches else None
        if match is None or match[1].lines != located.lines or match[0].title != located.column:
            raise ConflictError(f"{located.path}: card changed on disk; retry")
        return match

    def _new_id(self, taken: set[str]) -> str:
        while True:
            candidate = "t" + "".join(secrets.choice(ID_ALPHABET) for _ in range(3))
            # A deleted card keeps its log and attachments, so its ID stays taken.
            if candidate not in taken and not (log_path(self.config, candidate).exists()
                                               or attachments.folder(self.config, candidate).exists()):
                taken.add(candidate)
                return candidate

    def _assign_ids(self, path: Path, board: Board, taken: set[str] | None = None) -> None:
        """Give every column card without an ID a new one. `taken` collects the IDs in use."""
        taken = set() if taken is None else taken
        taken |= _all_ids(board)
        for other in self.board_paths():
            if other != path:
                taken |= _all_ids(self._read(other))
        for _, card in board.all_cards():
            if card.block_id is None:
                card.set_block_id(self._new_id(taken))

    def _modify(
        self,
        ref: str,
        change: Callable[[Board, Path, Column, Card], Column],
        located: _Located | None = None,
    ) -> CardView:
        located = located or self._resolve(ref)
        path = located.path
        with self._lock():
            board = self._read(path)
            self._require_writable(path, board)
            column, card = self._find(board, located)
            if card.block_id:
                self._check_unique(card.block_id)
            self._assign_ids(path, board)
            column = change(board, path, column, card)
            self._write(path, board)
        return self._view(path, column, card, card.block_id)

    def _is_completed(self, column: Column) -> bool:
        """True when a `**<marker>**` line marks the column complete."""
        return column.has_marker(["Complete", *self.config.complete_markers])

    def _default_column(self, board: Board, path: Path) -> Column:
        """The column for a card that names none: the first one that is not the triage column."""
        column = next((c for c in board.columns if not is_triage(c.title)), None)
        if column is None:
            if board.columns:
                raise HerdboardError(f"{path}: board has no column apart from {TRIAGE_COLUMN}")
            raise HerdboardError(f"{path}: board has no columns")
        return column

    def _target_column(self, board: Board, title: str) -> Column:
        """The column named `title`. The triage column is added first on a board that has none.

        Only the triage column is made on demand; any other unknown name raises like `Board.column`.
        """
        try:
            return board.column(title)
        except NotFoundError:
            if not is_triage(title):
                raise
        column = Column(f"## {TRIAGE_COLUMN}", ["", "", ""])
        board.columns.insert(0, column)
        return column

    def _done_column(self, board: Board, path: Path) -> Column:
        column = next((c for c in board.columns if self._is_completed(c)), None)
        if column is None:
            column = next((c for c in board.columns if c.title.casefold() == "done"), None)
        if column is None:
            raise HerdboardError(
                f"Board {self.board_name(path)} has no done column: "
                "add a complete marker or a column named Done"
            )
        return column

    def _move_card(
        self,
        column: Column,
        card: Card,
        target: Column,
        position: str,
        check: bool = False,
    ) -> Column:
        kanban_md.remove_card(column, card)
        if check or self._is_completed(target):
            if not card.checked:
                card.set_checked(True)
        elif self._is_completed(column):
            card.set_checked(False)
        kanban_md.insert_card(target, card, position)
        return target

    def move(self, ref: str, column: str, position: str = "bottom") -> CardView:
        def change(board, path, source, card):
            return self._move_card(source, card, self._target_column(board, column), position)

        return self._modify(ref, change)

    def done(self, ref: str) -> CardView:
        def change(board, path, source, card):
            target = self._done_column(board, path)
            return self._move_card(source, card, target, "bottom", check=True)

        return self._modify(ref, change)

    def toggle_item(self, ref: str, index: int, expected: CardView | None = None) -> CardView:
        """Check or clear the checklist item on detail line `index` (0 is the first detail line)."""

        def change(board, path, column, card):
            if expected is not None and (card.title != expected.title or card.body != expected.body):
                raise ConflictError(f"{path}: card changed on disk since you opened it; open it again")
            line = card.lines[index + 1] if 0 <= index < len(card.lines) - 1 else ""
            match = CHECK_LINE.match(line)
            if match is None:
                raise HerdboardError(f"Detail line {index + 1} of {card.block_id or ref} is not a checklist item")
            mark = " " if match.group("mark") != " " else "x"
            card.lines[index + 1] = f"{match.group('start')}{mark}{line[match.end('mark'):]}"
            return column

        return self._modify(ref, change)

    def shift(self, ref: str, step: int) -> CardView:
        """Swap a card with the card above (step -1) or below (step 1) in its column.

        Other lines of the column, such as a complete marker, keep their place.
        At the top or bottom of the column the card stays where it is.
        """

        def change(board, path, column, card):
            places = [i for i, item in enumerate(column.items) if isinstance(item, Card)]
            here = places.index(next(i for i in places if column.items[i] is card))
            there = here + step
            if 0 <= there < len(places):
                a, b = places[here], places[there]
                column.items[a], column.items[b] = column.items[b], column.items[a]
            return column

        return self._modify(ref, change)

    def move_to_board(self, ref: str, target: Path, column: str | None = None) -> CardView:
        """Move a todo, with its indented lines and its ID, to another board.

        Without `column`, the card keeps its column name when the target board has
        it, else it goes to the target's default column. A card that leaves the
        triage column lands in the target's triage column, which that board gains
        when it has none. The target file is written before the source file, so a
        failure between the two leaves the card on both boards (`doctor` reports the
        duplicate ID) instead of on neither.
        """
        located = self._resolve(ref)
        source = located.path
        if Path(target).resolve() == Path(source).resolve():
            raise HerdboardError(f"The card is already on {self.board_name(source)}")
        if not Path(target).is_file():
            raise NotFoundError(f"No board file: {target}")
        with self._lock():
            board = self._read(source)
            self._require_writable(source, board)
            old_column, card = self._find(board, located)
            if card.block_id:
                self._check_unique(card.block_id)
            destination = self._read(target)
            self._require_writable(target, destination)
            if column is not None:
                new_column = self._target_column(destination, column)
            elif is_triage(old_column.title):
                new_column = self._target_column(destination, TRIAGE_COLUMN)
            else:
                new_column = next(
                    (c for c in destination.columns if c.title.casefold() == old_column.title.casefold()),
                    None,
                ) or self._default_column(destination, target)
            # One set of used IDs for both boards: neither file has the other's new IDs yet.
            taken = _all_ids(destination)
            self._assign_ids(source, board, taken)
            self._assign_ids(target, destination, taken)
            kanban_md.remove_card(old_column, card)
            if self._is_completed(new_column):
                if not card.checked:
                    card.set_checked(True)
            elif self._is_completed(old_column):
                card.set_checked(False)
            kanban_md.insert_card(new_column, card, "bottom")
            self._write(target, destination)
            self._write(source, board)
        return self._view(target, new_column, card, card.block_id)

    def edit(
        self,
        ref: str,
        title: str | None = None,
        body: list[str] | None = None,
        expected: CardView | None = None,
    ) -> CardView:
        """Replace a card's first-line text, its indented lines, or both.

        The checkbox and the block ID stay. `body` lines are written one level
        deeper than the card. With `expected`, the card must still match it.
        """
        if title is not None:
            title = title.strip()
            if not title or "\n" in title or "\r" in title:
                raise HerdboardError("A card title must be one line of text")
            if BLOCK_ID.search(title):
                raise HerdboardError("A card title must not end with a block ID such as ^t1a2")

        def change(board, path, column, card):
            if expected is not None and (
                card.title != expected.title or card.body != expected.body
                or card.checked != expected.checked or column.title != expected.column
            ):
                raise ConflictError(f"{path}: card changed on disk since you opened it; open it again")
            if title is not None:
                ending = f" ^{card.block_id}" if card.block_id else ""
                # The parent link is card structure, not text: only set_parent changes it.
                text = with_parent(title, parent_of(title) or parent_of(card.title))
                card.lines[0] = f"- [{card.check_char}] {text}{ending}"
            if body is not None:
                card.lines[1:] = _body_lines(body)
            return column

        return self._modify(ref, change)

    def delete(self, ref: str) -> CardView:
        """Remove a card and its indented lines. Returns the card as it was."""

        def change(board, path, column, card):
            kanban_md.remove_card(column, card)
            return column

        return self._modify(ref, change)

    def _archive_column(self, board: Board) -> Column:
        """The board's archive section. A missing one is added after the last column, as Obsidian writes it."""
        if board.archive is None:
            last = board.columns[-1].items[-1] if board.columns and board.columns[-1].items else ""
            blank = isinstance(last, str) and not last.strip()
            board.archive_lines = ([] if blank else [""]) + ["***", ""]
            board.archive = Column(f"## {self.config.archive_titles[0]}", ["", ""], archive=True)
        return board.archive

    def archive(self, ref: str, blocked: Callable[[str], str | None] | None = None) -> CardView:
        """Move a todo to its board's archive section.

        `blocked` names why a card must stay on the board, such as a worktree that still
        exists, or returns None. It runs before the lock, because it can ask Herdr.
        """
        located = self._resolve(ref)
        # A temporary reference has no block ID in `located`, but the card can have one.
        reason = blocked(Card(located.lines).block_id or ref) if blocked is not None else None
        if reason:
            raise CardActiveError(f"Cannot archive {ref}: {reason}")

        def change(board, path, column, card):
            if card.block_id and any(parent_of(other.title) == card.block_id for _, other in board.all_cards()):
                raise ParentError(f"Cannot archive {card.block_id}: it has sub-cards on the board. "
                                  "Archive them first", "has_sub_cards")
            kanban_md.remove_card(column, card)
            archive = self._archive_column(board)
            kanban_md.insert_card(archive, card, "bottom")
            return archive

        return self._modify(ref, change, located)

    def done_cards(self, path: Path) -> list[CardView]:
        """The cards in the board's done column."""
        board = self._read(path)
        done = self._done_column(board, path)
        return [view for view in self.list_cards([path]) if view.column == done.title]

    def archive_done(
        self, path: Path, blocked: Callable[[str], str | None] | None = None,
    ) -> tuple[list[CardView], list[tuple[CardView, str]]]:
        """Archive every card in the board's done column, in one write.

        A card stays when `blocked` gives a reason, or when it is a parent whose sub-cards
        stay on the board. Returns the archived cards, and the cards that stay with the reason.
        """
        board = self._read(path)
        done = self._done_column(board, path)
        name = self.board_name(path)
        numbers = {id(card): number for number, (_, card) in enumerate(board.all_cards(), start=1)}
        skipped: list[tuple[CardView, str]] = []
        chosen: list[tuple[CardView, Card]] = []
        for card in done.cards:
            view = self._view(path, done, card, card.block_id or f"{name}:{numbers[id(card)]}")
            # A card without an ID was never started, so nothing can block it.
            reason = blocked(card.block_id) if blocked is not None and card.block_id else None
            if reason:
                skipped.append((view, reason))
            else:
                chosen.append((view, card))
        moving = {id(card) for _, card in chosen}
        staying = {parent_of(card.title) for _, card in board.all_cards() if id(card) not in moving}
        for view, card in list(chosen):
            if card.block_id and card.block_id in staying:
                chosen.remove((view, card))
                skipped.append((view, "it has sub-cards that stay on the board"))
        if not chosen:
            return [], skipped
        with self._lock():
            board = self._read(path)
            self._require_writable(path, board)
            done = self._done_column(board, path)
            pairs: list[tuple[CardView, Card]] = []
            for view, card in chosen:
                match = next((c for c in done.cards if c.lines == card.lines
                              and not any(c is m for _, m in pairs)), None)
                if match is None:
                    raise ConflictError(f"{path}: {view.ref} changed on disk; retry")
                pairs.append((view, match))
            # A sub-card can have appeared since the first read: check the parents again.
            moving = {id(card) for _, card in pairs}
            staying = {parent_of(card.title) for _, card in board.all_cards() if id(card) not in moving}
            matches: list[Card] = []
            for view, card in pairs:
                if card.block_id and card.block_id in staying:
                    skipped.append((view, "it has sub-cards that stay on the board"))
                else:
                    matches.append(card)
            if not matches:
                return [], skipped
            for card in matches:
                if card.block_id:
                    self._check_unique(card.block_id)
            self._assign_ids(path, board)
            archive = self._archive_column(board)
            for card in matches:
                kanban_md.remove_card(done, card)
                kanban_md.insert_card(archive, card, "bottom")
            self._write(path, board)
        return [self._view(path, archive, card, card.block_id) for card in matches], skipped

    def _locate_archived(self, ref: str) -> tuple[Path, int | None, list[str] | None]:
        """Find an archived card by its ID, or by an archive reference such as `p:archive-2`.

        Returns its board, and for an archive reference its place and its lines.
        """
        temp = ARCHIVE_REF.match(ref)
        if temp:
            path = self.resolve_board(temp.group("board"))
            board = self._read(path)
            cards = board.archive.cards if board.archive is not None else []
            number = int(temp.group("number"))
            if not 1 <= number <= len(cards):
                raise NotFoundError(f"No archived card {ref}")
            return path, number, list(cards[number - 1].lines)
        path = self._find_archived(ref)
        if path is None:
            raise NotFoundError(f"No archived card {ref}")
        return path, None, None

    @staticmethod
    def _archived_card(board: Board, path: Path, ref: str, number: int | None, lines: list[str] | None) -> Card:
        """Find the card of `_locate_archived` again in a fresh parse; its lines must be unchanged."""
        cards = board.archive.cards if board.archive is not None else []
        if number is not None:
            card = cards[number - 1] if number <= len(cards) else None
            if card is not None and card.lines != lines:
                card = None
        else:
            card = next((c for c in cards if c.block_id == ref), None)
        if card is None:
            raise ConflictError(f"{path}: card changed on disk; retry")
        return card

    def _archive_id(self, ref: str) -> CardView:
        """Give the archive card of an archive reference a new ID."""
        path, number, lines = self._locate_archived(ref)
        with self._lock():
            board = self._read(path)
            self._require_writable(path, board)
            card = self._archived_card(board, path, ref, number, lines)
            taken: set[str] = set()
            self._assign_ids(path, board, taken)
            card.set_block_id(self._new_id(taken))
            self._write(path, board)
        return self._view(path, board.archive, card, card.block_id)

    def archived_parent(self, ref: str) -> CardView | None:
        """The parent of an archived sub-card, when the parent is archived too."""
        path, number, lines = self._locate_archived(ref)
        board = self._read(path)
        parent = parent_of(self._archived_card(board, path, ref, number, lines).title)
        if parent is None:
            return None
        return next((view for view in self._archive_views(path, board) if view.ref == parent), None)

    def unarchive(self, ref: str, with_parent: bool = False) -> CardView:
        """Move an archived todo to its board's done column.

        A card without an ID takes its archive reference, and gets an ID. A sub-card whose
        parent is archived too needs `with_parent`, which moves the parent back first.
        """
        path, number, lines = self._locate_archived(ref)
        with self._lock():
            board = self._read(path)
            self._require_writable(path, board)
            card = self._archived_card(board, path, ref, number, lines)
            if card.block_id:
                self._check_unique(card.block_id)
            parent_id = parent_of(card.title)
            parent = next((c for c in board.archive.cards if c.block_id == parent_id), None) if parent_id else None
            if parent is not None and not with_parent:
                raise ParentError(f"The parent {parent_id} of {ref} is archived too. "
                                  "Unarchive the parent with it", "parent_archived")
            done = self._done_column(board, path)
            if parent is not None:
                self._move_card(board.archive, parent, done, "bottom", check=True)
            target = self._move_card(board.archive, card, done, "bottom", check=True)
            self._assign_ids(path, board)
            self._write(path, board)
        return self._view(path, target, card, card.block_id)

    def _check_parent(self, board: Board, path: Path, parent: str, child: str | None) -> None:
        """Refuse a parent that is not on this board, that has a parent, or whose child has children."""
        if parent == child:
            raise ParentError(f"{parent} cannot be its own parent", "usage")
        cards = [card for _, card in board.all_cards()]
        found = next((card for card in cards if card.block_id == parent), None)
        if found is None:
            raise ParentError(f"No card {parent} on board {self.board_name(path)}; "
                              "a parent must be on the same board", "parent_missing")
        if parent_of(found.title):
            raise ParentError(f"{parent} is a sub-card; sub-cards go one level deep", "parent_nested")
        if child is not None and any(parent_of(card.title) == child for card in cards):
            raise ParentError(f"{child} has sub-cards, so it cannot be a sub-card", "parent_nested")

    def set_parent(self, ref: str, parent: str | None) -> CardView:
        """Set or remove a todo's parent link."""
        def change(board, path, column, card):
            if parent is not None:
                self._check_parent(board, path, parent, card.block_id)
            ending = f" ^{card.block_id}" if card.block_id else ""
            card.lines[0] = f"- [{card.check_char}] {with_parent(card.title, parent)}{ending}"
            return column

        return self._modify(ref, change)

    def children(self, ref: str) -> list[CardView]:
        """The sub-cards of a todo, in board order."""
        card = self.show(ref)
        return [view for view in self.list_cards([card.path]) if view.parent == card.ref]

    def ensure_id(self, ref: str, archived: bool = False) -> CardView:
        """The card, with a block ID: a card without one gets IDs by the usual write rule.

        With `archived`, an archived card is returned too. One without an ID gets one.
        """
        view = self.show(ref, archived)
        if view.archived and ARCHIVE_REF.match(view.ref):
            view = self._archive_id(view.ref)
        elif TEMP_REF.match(view.ref):
            view = self._modify(ref, lambda board, path, column, card: column)
        return view

    def todo_snapshot(self, ref: str) -> TodoSnapshot:
        """Read a todo for `promote`. Fails when its board has no done column."""
        located = self._resolve(ref)
        board = self._read(located.path)
        column, card = self._find(board, located)
        done = self._done_column(board, located.path)
        return TodoSnapshot(
            located, self._view(located.path, column, card, card.block_id or ref), column is done
        )

    def finish_promotion(self, snapshot: TodoSnapshot, identifier: str) -> CardView:
        """Add `→ identifier` before the block ID and move the todo to the done column.

        Fails with a conflict when the todo changed since `snapshot`.
        """

        def change(board, path, source, card):
            card.lines[0] = BLOCK_ID.sub(
                lambda match: f" → {identifier}{match.group(0)}", card.first_line
            )
            target = self._done_column(board, path)
            if target is source:
                card.set_checked(True)
                return source
            return self._move_card(source, card, target, "bottom", check=True)

        return self._modify(snapshot.located.block_id, change, snapshot.located)

    def add(
        self,
        path: Path,
        text: str,
        column: str | None = None,
        position: str = "bottom",
        body: list[str] | None = None,
        parent: str | None = None,
    ) -> CardView:
        if not text.strip() or "\n" in text or "\r" in text:
            raise HerdboardError("Card text must be one non-empty line")
        if not path.is_file():
            raise NotFoundError(f"No board file: {path}")
        with self._lock():
            board = self._read(path)
            self._require_writable(path, board)
            if column is None:
                target = self._default_column(board, path)
            else:
                target = self._target_column(board, column)
            self._assign_ids(path, board)
            if parent is not None:
                self._check_parent(board, path, parent, None)
                text = with_parent(text, parent)
            mark = "x" if self._is_completed(target) else " "
            card = Card([f"- [{mark}] {text}", *_body_lines(body or [])])
            card.set_block_id(self._new_id(self._taken_ids(path, board)))
            kanban_md.insert_card(target, card, position)
            self._write(path, board)
        return self._view(path, target, card, card.block_id)

    def _taken_ids(self, path: Path, board: Board) -> set[str]:
        taken = _all_ids(board)
        for other in self.board_paths():
            if other != path:
                taken |= _all_ids(self._read(other))
        return taken


# A checklist item in a card's detail lines: `<indent>- [ ] text`.
CHECK_LINE = re.compile(r"^(?P<start>\s+[-*+] \[)(?P<mark>.)\] ")


def _body_lines(body: list[str]) -> list[str]:
    """Card lines for typed detail lines: one level deeper than the card, trailing blanks dropped."""
    lines = list(body)
    while lines and not lines[-1].strip():
        lines.pop()
    return [f"\t{line}" if line.strip() else "\t" for line in lines]
