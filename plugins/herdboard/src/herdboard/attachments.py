"""Card attachments: documents too large for the card text, such as the plan, next to the activity log.

Like the log, attachments live in a hidden folder of the todos, so the card text stays the user's,
and they work for Linear cards without a connection.
"""

from __future__ import annotations

import codecs
import os
from pathlib import Path
import re
import tempfile

from . import activity
from .activity import LogUsageError, log_path
from .config import Config
from .model import IoError, NotFoundError

PLAN_NAME = "plan.md"
CHUNK = 1024 * 1024
# A plain file name: no folders, no hidden files, no control characters.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._ -]{0,99}")


def folder(config: Config, ref: str) -> Path:
    # The log checks that the reference is a card ID, which is then a safe folder name.
    log_path(config, ref)
    return config.todos_dir / ".herdboard" / "attachments" / ref


def _name(name: str) -> str:
    if not NAME.fullmatch(name) or name != name.strip():
        raise LogUsageError(f"{name!r} is not a plain file name")
    return name


def _entry(path: Path) -> dict:
    return {"name": path.name, "path": str(path), "size": path.stat().st_size}


def listing(config: Config, ref: str) -> list[dict]:
    """The card's attachments, sorted by name."""
    base = folder(config, ref)
    try:
        return [_entry(path) for path in sorted(base.iterdir()) if path.is_file() and NAME.fullmatch(path.name)]
    except FileNotFoundError:
        return []
    except OSError as error:
        raise IoError(f"{base}: cannot read the attachments: {error}") from error


def add(store, ref: str, source: Path, name: str | None = None) -> dict:
    """Copy a file into the card's attachments. An attachment with the same name is replaced."""
    target = folder(store.config, ref) / _name(source.name if name is None else name)
    try:
        reader = source.open("rb")
    except FileNotFoundError as error:
        raise NotFoundError(f"{source}: no such file") from error
    except OSError as error:
        raise IoError(f"{source}: cannot read: {error}") from error
    # The agents read the plan as text, so it must be UTF-8.
    decoder = codecs.getincrementaldecoder("utf-8")() if target.name == PLAN_NAME else None
    temp = None
    with reader, store._lock():
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
    target = folder(store.config, ref) / _name(name)
    with store._lock():
        if not target.is_file():
            raise NotFoundError(f"{ref} has no attachment {name}")
        entry = _entry(target)
        try:
            target.unlink()
        except OSError as error:
            raise IoError(f"{target}: cannot remove the attachment: {error}") from error
    return entry


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
