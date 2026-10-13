"""The activity log of a card: one Markdown file per card in a hidden folder of the todos.

Agents, herdboard and the user append entries. Nobody rewrites them, so the log is
the card's history, and the card's own text stays the user's.
"""

from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import re
import subprocess

from .config import Config
from .model import HerdboardError, IoError

# A todo ID (`t3m9`) or a Linear identifier (`ENG-123`): both are safe file names.
LOG_REF = re.compile(r"^(t[0-9a-z]{3}|[A-Z][A-Z0-9]*-\d+)$")
ENTRY = re.compile(r"^- (?P<time>\d{4}-\d\d-\d\d \d\d:\d\d) · (?P<author>[a-z]+) · (?P<message>.*)$")


class LogUsageError(HerdboardError):
    code = "usage"


def log_path(config: Config, ref: str) -> Path:
    if not LOG_REF.match(ref):
        raise LogUsageError(f"{ref!r} is not a card ID: use a todo ID or a Linear identifier")
    return config.todos_dir / ".herdboard" / "log" / f"{ref}.md"


def append(store, ref: str, message: str, author: str, now: datetime | None = None) -> dict:
    """Add one entry to the end of a card's log. A message with several lines keeps them, indented."""
    path = log_path(store.config, ref)
    lines = [line.rstrip() for line in message.replace("\r", "").strip().split("\n")]
    if not lines[0]:
        raise LogUsageError("The log message is empty")
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    text = f"- {stamp} · {author} · {lines[0]}\n" + "".join(f"\t{line}\n" for line in lines[1:])
    with store._lock():
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as error:
            raise IoError(f"{path}: cannot write the log: {error}") from error
    return {"time": stamp, "author": author, "message": "\n".join(lines)}


def read(config: Config, ref: str, limit: int | None = None) -> list[dict]:
    """A card's log entries, oldest first; with `limit`, only the newest ones."""
    path = log_path(config, ref)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return []
    except (OSError, UnicodeDecodeError) as error:
        raise IoError(f"{path}: cannot read the log: {error}") from error
    entries: list[dict] = []
    for line in text.splitlines():
        match = ENTRY.match(line)
        if match:
            entries.append(match.groupdict())
        elif line.startswith("\t") and entries:
            entries[-1]["message"] += "\n" + line[1:]
    return entries[-limit:] if limit else entries


def _run(argv: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=10)


def agent_kind(pane_id: str) -> str | None:
    """The kind of agent (`claude`, `pi`, ...) that runs in a Herdr pane, or None."""
    try:
        result = _run([os.environ.get("HERDR_BIN_PATH") or "herdr", "agent", "get", pane_id])
        agent = json.loads(result.stdout)["result"]["agent"]["agent"]
    except (OSError, subprocess.TimeoutExpired, ValueError, KeyError, TypeError):
        return None
    if result.returncode or not isinstance(agent, str) or not re.fullmatch(r"[a-z]+", agent):
        return None
    return agent


def author() -> str:
    """Who writes a log entry from this process: the agent of this Herdr pane, else the user."""
    pane = os.environ.get("HERDR_PANE_ID")
    return (agent_kind(pane) if pane else None) or "user"
