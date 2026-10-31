"""Card attachments: documents too large for the card text, such as the plan, next to the activity log.

Attachments live in the card's folder (see `paths`), in `attachments/`, so the card text stays
the user's. Linear cards have a folder of their own, so they work without a connection.
"""

from __future__ import annotations

import codecs
import os
import re
import tempfile
from pathlib import Path

from . import activity
from .config import Config
from .model import IoError, NotFoundError
from .paths import LogUsageError, card_dir, data_dir

PLAN_NAME = "plan.md"
CHUNK = 1024 * 1024
# The largest attachment that the board shows itself; a larger one opens in another application.
VIEW_LIMIT = 1024 * 1024
# A plain file name: no folders, no hidden files, no control characters.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}")


def folder(config: Config, ref: str) -> Path:
    """The card's attachment folder. Raises NotFoundError for a todo that has no folder."""
    return card_dir(config, ref) / "attachments"


def _name(name: str) -> str:
    if not NAME.fullmatch(name) or name != name.strip():
        raise LogUsageError(f"{name!r} is not a plain file name")
    return name


def _entry(path: Path) -> dict:
    return {"name": path.name, "path": str(path), "size": path.stat().st_size}


def listing(config: Config, ref: str) -> list[dict]:
    """The card's attachments, sorted by name."""
    try:
        base = folder(config, ref)
    except NotFoundError:
        return []
    try:
        return [_entry(path) for path in sorted(base.iterdir()) if path.is_file() and NAME.fullmatch(path.name)]
    except FileNotFoundError:
        return []
    except OSError as error:
        raise IoError(f"{base}: cannot read the attachments: {error}") from error


def cards_with_files(config: Config) -> set[str]:
    """The IDs of the cards that have at least one attachment, for the board's marker."""
    return set(card_files(config))


def card_files(config: Config) -> dict[str, set[str]]:
    """The attachment names of each card that has at least one attachment."""
    boards = config.todos_dir / "boards"
    try:
        folders = [
            *boards.glob("*/cards/*/attachments"),
            *boards.glob("*/archive/*/attachments"),
            *data_dir(config).glob("automations/*/attachments"),
            *data_dir(config).glob("linear/*/attachments"),
        ]
        found = {path.parent.name: _file_names(path) for path in folders if path.is_dir()}
        return {ref: names for ref, names in found.items() if names}
    except OSError as error:
        raise IoError(f"{boards}: cannot read the attachments: {error}") from error


def _file_names(card_folder: Path) -> set[str]:
    try:
        return {path.name for path in card_folder.iterdir() if path.is_file() and NAME.fullmatch(path.name)}
    except FileNotFoundError:
        # A folder removed while the board reads it has no attachments.
        return set()


def mark(config: Config, cards) -> None:
    """Set the board-only attachment and plan status. A folder that cannot be read hides only the markers."""
    try:
        files = card_files(config)
    except IoError:
        files = {}
    for card in cards:
        card.has_attachments = card.ref in files
        card.planned = PLAN_NAME in files.get(card.ref, ())


def add(store, ref: str, source: Path, name: str | None = None) -> dict:
    """Copy a file into the card's attachments. An attachment with the same name is replaced."""
    name = _name(source.name if name is None else name)
    # Checks the reference before the lock; the folder itself is found under the lock.
    card_dir(store.config, ref)
    try:
        reader = source.open("rb")
    except FileNotFoundError as error:
        raise NotFoundError(f"{source}: no such file") from error
    except OSError as error:
        raise IoError(f"{source}: cannot read: {error}") from error
    # The agents read the plan as text, so it must be UTF-8.
    decoder = codecs.getincrementaldecoder("utf-8")() if name == PLAN_NAME else None
    temp = None
    with reader, store._lock():
        # Under the lock: an archive or a move to another board moves a todo's folder.
        target = folder(store.config, ref) / name
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, temp = tempfile.mkstemp(dir=target.parent, prefix=f".{target.name}.")
            with os.fdopen(fd, "wb") as handle:
                # In parts, because an attachment can be large.
                while chunk := _read(reader, source):
                    if decoder:
                        decoder.decode(chunk)
                    handle.write(chunk)
                if decoder:
                    decoder.decode(b"", final=True)
            os.replace(temp, target)
            temp = None
        except UnicodeDecodeError as error:
            raise LogUsageError(f"{source}: {PLAN_NAME} must be UTF-8 text") from error
        except OSError as error:
            raise IoError(f"{target}: cannot write the attachment: {error}") from error
        finally:
            if temp is not None:
                Path(temp).unlink(missing_ok=True)
    return _entry(target)


def _read(reader, source: Path) -> bytes:
    try:
        return reader.read(CHUNK)
    except OSError as error:
        raise IoError(f"{source}: cannot read: {error}") from error


def remove(store, ref: str, name: str) -> dict:
    name = _name(name)
    with store._lock():
        target = folder(store.config, ref) / name
        if not target.is_file():
            raise NotFoundError(f"{ref} has no attachment {name}")
        entry = _entry(target)
        try:
            target.unlink()
        except OSError as error:
            raise IoError(f"{target}: cannot remove the attachment: {error}") from error
    return entry


def view_text(path: Path) -> str | None:
    """The attachment as text for the board's viewer, or None when it is not UTF-8 text
    or is too large to show there."""
    try:
        with path.open("rb") as handle:
            data = handle.read(VIEW_LIMIT + 1)
    except OSError as error:
        raise IoError(f"{path}: cannot read the attachment: {error}") from error
    if len(data) > VIEW_LIMIT or b"\x00" in data:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return None


def size_label(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def plan_path(config: Config, ref: str) -> Path:
    return folder(config, ref) / PLAN_NAME


def plan_text(config: Config, ref: str) -> str | None:
    """The card's plan, or None when it has none."""
    path = plan_path(config, ref)
    try:
        return path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except (OSError, UnicodeDecodeError) as error:
        raise IoError(f"{path}: cannot read the plan: {error}") from error


def plan_for_agent(store, ref: str) -> str | None:
    """The plan for an agent's brief. A plan that cannot be read does not stop the agent: the log says so."""
    try:
        return plan_text(store.config, ref)
    except IoError as error:
        activity.append(store, ref, f"The agent does not get the plan: {error}", "herdboard")
        return None
