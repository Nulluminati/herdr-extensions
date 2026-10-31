"""Convert a todos folder from the old Obsidian Markdown boards to board folders, once.

Old layout:

    inbox.md, projects/<name>.md     one Markdown file per board
    notes/<board>.md                 the board notes
    .herdboard/log/<ref>.md          activity logs of todos, automations and Linear issues
    .herdboard/attachments/<ref>/    their attachments

New layout (see `store` and `paths`):

    boards/<name>/board.toml, cards/<id>/, archive/<id>/, notes.md
    .herdboard/automations/<id>/, .herdboard/linear/<IDENTIFIER>/

The run holds the store lock. It builds the new boards in `boards.tmp/` and renames that
folder to `boards/` only when the build is complete. Only then do the old files move to a
backup folder in the state folder. A failure before the rename leaves the old store as it was.
"""

from __future__ import annotations

import os
import re
import secrets
import shutil
import tomllib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from . import card_file, kanban_md
from .card_file import BoardFile, CardFile, ColumnFile
from .config import MERGE_MODES, Config
from .linear_links import parse_links
from .linear_pins import parse_pins
from .model import FormatError, HerdboardError
from .paths import AUTOMATION_REF, LOG_REF, TODO_REF
from .store import ID_ALPHABET, Store, is_card_id

LOG_ENTRY_TIME = re.compile(r"^- (?P<time>\d{4}-\d\d-\d\d \d\d:\d\d) · ")
OLD_DATA = (".herdboard/log", ".herdboard/attachments")


class ConvertError(HerdboardError):
    code = "convert"


@dataclass
class ConvertedBoard:
    name: str
    source: Path
    file: BoardFile
    cards: dict[str, CardFile] = field(default_factory=dict)
    # Archived cards, oldest first, as the old archive listed them.
    archive: dict[str, CardFile] = field(default_factory=dict)
    # Cards that got a new ID: (old block ID or None, new ID, title).
    new_ids: list[tuple[str | None, str, str]] = field(default_factory=list)


def old_board_files(todos: Path) -> list[tuple[str, Path]]:
    """The old board files as (board name, path): the inbox first, then the projects."""
    found = [("inbox", todos / "inbox.md")] if (todos / "inbox.md").is_file() else []
    projects = todos / "projects"
    if projects.is_dir():
        found.extend((path.stem, path) for path in sorted(projects.glob("*.md")))
    return found


def old_settings(config_file: Path | None) -> tuple[list[str], tuple[str, ...]]:
    """The old `complete_markers` and `archive_titles` settings, from the raw config file."""
    markers, titles = ["Complete"], ("Archive",)
    if config_file is None or not config_file.is_file():
        return markers, titles
    try:
        data = tomllib.loads(config_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
        return markers, titles
    extra = data.get("complete_markers")
    if isinstance(extra, list) and all(isinstance(m, str) for m in extra):
        markers = ["Complete", *extra]
    archive = data.get("archive_titles")
    if isinstance(archive, list) and all(isinstance(t, str) for t in archive):
        titles = ("Archive", *(t for t in archive if t != "Archive"))
    return markers, titles


def _frontmatter(lines: list[str], path: Path) -> dict:
    """The YAML mapping between the `---` fences, or an empty one."""
    if len(lines) < 2:
        return {}
    body = "\n".join(line.rstrip("\r") for line in lines[1:-1])
    try:
        import yaml
    except ImportError:
        if re.search(r"^linear-(projects|issues):", body, re.MULTILINE):
            raise ConvertError(
                f"{path}: PyYAML is needed to read the Linear links: install it with the plugin"
            ) from None
        return kanban_md.frontmatter_scalars(lines)
    try:
        data = yaml.safe_load(body)
    except (yaml.YAMLError, ValueError) as error:
        raise ConvertError(f"{path}: invalid YAML in frontmatter: {error}") from error
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConvertError(f"{path}: frontmatter must be a YAML mapping")
    return data


def split_title(title: str) -> dict:
    """The fields in an old card title: text, type, tags, due date, parent and promotion."""
    fields: dict = {"type": None, "tags": [], "due": None, "parent": None, "promoted": None}
    if found := kanban_md.PROMOTED.search(title):
        fields["promoted"] = found.group("identifier")
        title = kanban_md.PROMOTED.sub("", title)
    if found := kanban_md.PARENT_LINK.search(title):
        fields["parent"] = found.group("id")
        title = kanban_md.PARENT_LINK.sub("", title)
    if found := kanban_md.DUE_DATE.search(title):
        fields["due"] = found.group("date")
        title = kanban_md.DUE_DATE.sub("", title)
    for found in kanban_md.TAG.finditer(title):
        tag = found.group("tag")
        if tag.startswith(kanban_md.TYPE_TAG_PREFIX):
            if fields["type"] is None and len(tag) > len(kanban_md.TYPE_TAG_PREFIX):
                fields["type"] = tag[len(kanban_md.TYPE_TAG_PREFIX) :]
        elif tag not in fields["tags"]:
            fields["tags"].append(tag)
    fields["title"] = " ".join(kanban_md.TAG.sub("", title).split()) or "Untitled"
    return fields


def _dedent(line: str) -> str:
    """A body line with the one indent level that tied it to the card removed."""
    line = line.rstrip("\r")
    if line.startswith("\t"):
        return line[1:]
    if line.startswith("    "):
        return line[4:]
    return line.lstrip(" ")


def _card(card: kanban_md.Card, created: datetime) -> CardFile:
    fields = split_title(card.title)
    if fields["due"]:
        try:
            card_file.parse_due(fields["due"])
        except FormatError:
            # A date that is not real stays in the title as text.
            fields["title"] = f"{fields['title']} 📅 {fields['due']}"
            fields["due"] = None
    file = card_file.new_card(
        fields["title"],
        created=created,
        type=fields["type"],
        tags=fields["tags"],
        due=fields["due"],
        parent=fields["parent"],
        body=[_dedent(line) for line in card.body],
    )
    file.promoted = fields["promoted"]
    return file


def first_log_time(path: Path) -> datetime | None:
    """The time of the first entry of an old log file, in local time, or None."""
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                if found := LOG_ENTRY_TIME.match(line):
                    return datetime.strptime(found.group("time"), "%Y-%m-%d %H:%M").astimezone()
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return None


def _new_id(taken: set[str]) -> str:
    while True:
        candidate = "t" + "".join(secrets.choice(ID_ALPHABET) for _ in range(3))
        if candidate not in taken:
            taken.add(candidate)
            return candidate


def convert_board(
    name: str,
    path: Path,
    text: str,
    *,
    taken: set[str],
    now: datetime,
    markers: list[str],
    archive_titles: tuple[str, ...],
    created_for=lambda ref: None,
) -> ConvertedBoard:
    """Convert one old board file. `taken` holds the IDs in use and gets the new ones."""
    board = kanban_md.parse(text.replace("\r\n", "\n"), archive_titles)
    data = _frontmatter(board.frontmatter, path)
    file = card_file.new_board([])
    for key in ("repo", "merge"):
        value = data.get(key)
        if value is not None:
            if not isinstance(value, str):
                raise ConvertError(f"{path}: {key} must be text")
            setattr(file, key, value)
    if file.merge not in (None, *MERGE_MODES):
        raise ConvertError(f"{path}: merge must be local or pr")
    try:
        links = parse_links(data.get("linear-projects"))
        pins = parse_pins(data.get("linear-issues"))
    except FormatError as error:
        raise ConvertError(f"{path}: {error}") from error
    file.set_tables("linear-projects", [link.to_dict() for link in links])
    file.set_tables("linear-issues", [pin.to_dict() for pin in pins])
    converted = ConvertedBoard(name, path, file)

    def card_id(card: kanban_md.Card) -> str:
        old = card.block_id
        if old is not None and is_card_id(old) and old not in taken:
            taken.add(old)
            return old
        new = _new_id(taken)
        converted.new_ids.append((old, new, card.title))
        return new

    done_found = False
    for column in board.columns:
        done = column.has_marker(markers) and not done_found
        done_found = done_found or done
        ids = []
        for card in column.cards:
            new = card_id(card)
            converted.cards[new] = _card(card, created_for(new) or now)
            ids.append(new)
        file.columns.append(ColumnFile(column.title, ids, column.limit, done))
    if not done_found:
        named = file.column("done")
        if named is not None:
            named.done = True
    archived = board.archive.cards if board.archive is not None else []
    for index, card in enumerate(archived):
        new = card_id(card)
        result = _card(card, created_for(new) or now)
        # The old archive lists the newest card last; the new one sorts newest first.
        result.archived = now - timedelta(seconds=len(archived) - 1 - index)
        converted.archive[new] = result
    return converted


def running_cards(refs: list[str]) -> list[str]:
    """The cards among `refs` whose agent, planner or agent reviewer still runs."""
    from .plan import running_planner
    from .start import agent_live, find_record

    busy = []
    for ref in refs:
        found = find_record(ref)
        record = found[1] if found else {}
        if running_planner(ref) or record.get("agent_review") or (found and agent_live(record)):
            busy.append(ref)
    return busy


def convert_store(
    store: Store,
    *,
    config_file: Path | None = None,
    dry_run: bool = False,
    state: Path | None = None,
    busy=None,
) -> dict:
    """Convert the whole todos folder. Returns what was (or, with `dry_run`, would be) done."""
    config: Config = store.config
    todos = config.todos_dir
    boards_dir = todos / "boards"
    if boards_dir.exists():
        raise ConvertError(f"{boards_dir} already exists: the store is converted")
    sources = old_board_files(todos)
    if not sources:
        raise ConvertError(f"{todos} has no boards in the old format to convert")
    markers, titles = old_settings(config_file)
    now = datetime.now().astimezone().replace(microsecond=0)
    old_logs = todos / ".herdboard" / "log"
    old_files = todos / ".herdboard" / "attachments"
    with store._lock():
        temp = todos / "boards.tmp"
        if temp.exists():
            shutil.rmtree(temp)
        taken: set[str] = set()
        boards = []
        for name, path in sources:
            try:
                text = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError) as error:
                raise ConvertError(f"{path}: cannot read: {error}") from error
            boards.append(
                convert_board(
                    name,
                    path,
                    text,
                    taken=taken,
                    now=now,
                    markers=markers,
                    archive_titles=titles,
                    created_for=lambda ref: first_log_time(old_logs / f"{ref}.md"),
                )
            )
        refs = [ref for board in boards for ref in [*board.cards, *board.archive]]
        running = (busy or running_cards)(refs)
        # A dry run writes nothing, so it names the running cards instead of refusing.
        if running and not dry_run:
            raise ConvertError(
                f"Stop the agents, planners and reviewers of {', '.join(running)} first: their card folders move"
            )
        homes = {ref: (board.name, "cards") for board in boards for ref in board.cards}
        homes.update({ref: (board.name, "archive") for board in boards for ref in board.archive})
        data_refs = sorted(
            {p.stem for p in old_logs.glob("*.md")} | {p.name for p in old_files.glob("*") if p.is_dir()}
        )
        moves, dropped = [], []
        for ref in data_refs:
            if not LOG_REF.match(ref):
                continue
            if TODO_REF.match(ref):
                if ref not in homes:
                    dropped.append(ref)
                    continue
                name, folder = homes[ref]
                moves.append((ref, Path("boards") / name / folder / ref))
            elif AUTOMATION_REF.match(ref):
                moves.append((ref, Path(".herdboard") / "automations" / ref))
            else:
                moves.append((ref, Path(".herdboard") / "linear" / ref))
        notes = [(board.name, todos / "notes" / f"{board.name}.md") for board in boards]
        notes = [(name, path) for name, path in notes if path.is_file()]
        summary = {
            "running": running,
            "boards": [
                {
                    "name": board.name,
                    "source": str(board.source),
                    "columns": {column.name: len(column.cards) for column in board.file.columns},
                    "archived": len(board.archive),
                    "new_ids": [{"old": old, "new": new, "title": title} for old, new, title in board.new_ids],
                }
                for board in boards
            ],
            "data": [{"ref": ref, "to": str(todos / target)} for ref, target in moves],
            "data_without_card": dropped,
            "notes": [str(path) for _, path in notes],
            "dry_run": dry_run,
        }
        if dry_run:
            return summary
        created: list[Path] = []
        try:
            for board in boards:
                folder = temp / board.name
                for sub, cards in (("cards", board.cards), ("archive", board.archive)):
                    for ref, card in cards.items():
                        store._write_file(folder / sub / ref / "card.md", card_file.dump_card(card).encode("utf-8"))
                store._write_file(folder / "board.toml", card_file.dump_board(board.file).encode("utf-8"))
            for name, path in notes:
                shutil.copy2(path, temp / name / "notes.md")
            for ref, target in moves:
                where = temp / target.relative_to("boards") if target.parts[0] == "boards" else todos / target
                if target.parts[0] != "boards":
                    # The outermost folder that this run makes, so a failure removes all of it.
                    missing = [
                        p for p in (*reversed(where.parents), where) if p.is_relative_to(todos) and not p.exists()
                    ]
                    if missing:
                        created.append(missing[0])
                where.mkdir(parents=True, exist_ok=True)
                if (old_logs / f"{ref}.md").is_file():
                    shutil.copy2(old_logs / f"{ref}.md", where / "log.md")
                if (old_files / ref).is_dir():
                    shutil.copytree(old_files / ref, where / "attachments", dirs_exist_ok=True)
            os.rename(temp, boards_dir)
        except BaseException:
            shutil.rmtree(temp, ignore_errors=True)
            for path in created:
                shutil.rmtree(path, ignore_errors=True)
            raise
        backup = (state or _state_dir()) / f"backup-{now.strftime('%Y%m%d-%H%M%S')}"
        backup.mkdir(parents=True, exist_ok=True)
        old = [path for _, path in sources if path.parent == todos]
        old += [todos / "projects", todos / "notes", old_logs, old_files]
        for path in old:
            if path.exists():
                target = backup / path.relative_to(todos)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(target))
        summary["backup"] = str(backup)
        return summary


def _state_dir() -> Path:
    from .linear_source import state_dir

    return state_dir()
