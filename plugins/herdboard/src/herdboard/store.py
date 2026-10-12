"""Read and write boards: card references, IDs, and locked atomic writes."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import re
import secrets
import shutil
import tempfile
import time
from typing import Callable, Iterator

from . import kanban_md
from .migrate_convert import Item
from .linear_links import ProjectLink, parse_links, replace_links
from .config import Config
from .identity import RepoIdentity, find_board, new_board_path, same_repo
from .model import (
    BLOCK_ID,
    Board,
    Card,
    Column,
    ConflictError,
    DuplicateIdError,
    FormatError,
    HerdboardError,
    IoError,
    NotFoundError,
)

ID_ALPHABET = "0123456789abcdefghijklmnopqrstuvwxyz"
TEMP_REF = re.compile(r"^(?P<board>.+):(?P<number>\d+)$")
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


@dataclass
class BoardMeta:
    """What Linear views need from a board: its repository and its project links."""

    repo: str | None
    links: list[ProjectLink]
    error: str | None = None


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

    def init_board(self, identity: RepoIdentity) -> Path:
        with self._lock():
            existing = find_board(self.projects_dir, identity)
            if existing is not None:
                raise HerdboardError(f"{identity.identity} already has a board: {existing}")
            path = new_board_path(self.projects_dir, identity)
            self._create(path, self._new_board_text([("repo", identity.identity)]))
        return path

    def board_meta(self, path: Path) -> BoardMeta:
        board = self._read(path)
        try:
            return BoardMeta(board.frontmatter_value("repo"), parse_links(board.frontmatter))
        except FormatError as error:
            return BoardMeta(board.frontmatter_value("repo"), [], f"{path}: {error}")

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
                            parsed.columns[0].title, done_title, error)

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
            for item in items:
                column = done if item.checked else board.columns[0]
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
            tags=card.tags,
            body=list(card.body),
            column_limit=column.limit,
        )

    def list_cards(self, paths: list[Path] | None = None) -> list[CardView]:
        views = []
        for path in self.board_paths() if paths is None else paths:
            name = self.board_name(path)
            for number, (column, card) in enumerate(self._read(path).all_cards(), start=1):
                views.append(self._view(path, column, card, card.block_id or f"{name}:{number}"))
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
        return BoardSnapshot(name, path, columns)

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
            raise NotFoundError(f"No card {ref}")
        self._check_unique(ref)
        path, column, card = found[0]
        return _Located(path, ref, None, column.title, list(card.lines))

    def notes_for(self, card: CardView) -> list[dict] | None:
        """Notes that `[[name]]` links in a card point to; None without `vault_root`."""
        vault = self.config.vault_root
        if vault is None:
            return None
        text = "\n".join([card.title, *card.body])
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

    def show(self, ref: str) -> CardView:
        located = self._resolve(ref)
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
            if candidate not in taken:
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
            return self._move_card(source, card, board.column(column), position)

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
        it, else it goes to the target's first column. The target file is written
        before the source file, so a failure between the two leaves the card on
        both boards (`doctor` reports the duplicate ID) instead of on neither.
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
                new_column = destination.column(column)
            else:
                new_column = next(
                    (c for c in destination.columns if c.title.casefold() == old_column.title.casefold()),
                    destination.columns[0] if destination.columns else None,
                )
                if new_column is None:
                    raise HerdboardError(f"{target}: board has no columns")
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
                card.lines[0] = f"- [{card.check_char}] {title}{ending}"
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

    def ensure_id(self, ref: str) -> CardView:
        """The card, with a block ID: a card without one gets IDs by the usual write rule."""
        view = self.show(ref)
        if TEMP_REF.match(view.ref):
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
    ) -> CardView:
        if not text.strip() or "\n" in text or "\r" in text:
            raise HerdboardError("Card text must be one non-empty line")
        if not path.is_file():
            raise NotFoundError(f"No board file: {path}")
        with self._lock():
            board = self._read(path)
            self._require_writable(path, board)
            if column is None:
                if not board.columns:
                    raise HerdboardError(f"{path}: board has no columns")
                target = board.columns[0]
            else:
                target = board.column(column)
            self._assign_ids(path, board)
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
