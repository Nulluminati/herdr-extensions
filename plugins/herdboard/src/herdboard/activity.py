"""The activity log of a card: `log.md` in the card's folder (see `paths`).

Agents, herdboard and the user append entries. Nobody rewrites them, so the log is
the card's history, and the card's own text stays the user's.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path

from .config import Config
from .model import IoError, NotFoundError
from .paths import LOG_REF, LogUsageError, card_dir

__all__ = ["LOG_REF", "LogUsageError", "append", "log_path", "read"]

ENTRY = re.compile(r"^- (?P<time>\d{4}-\d\d-\d\d \d\d:\d\d) · (?P<author>[a-z]+) · (?P<message>.*)$")
# Panes whose agent herdboard started, newest last. Herdr pane IDs are never reused.
PANE_AGENTS_FILE = "panes.json"
PANE_AGENT_LIMIT = 256


def log_path(config: Config, ref: str) -> Path:
    """The card's log file. Raises NotFoundError for a todo that has no folder."""
    return card_dir(config, ref) / "log.md"


def append(store, ref: str, message: str, author: str, now: datetime | None = None) -> dict:
    """Add one entry to the end of a card's log. A message with several lines keeps them, indented."""
    if not LOG_REF.match(ref):
        raise LogUsageError(f"{ref!r} is not a card ID: use a todo ID or a Linear identifier")
    lines = [line.rstrip() for line in message.replace("\r", "").strip().split("\n")]
    if not lines[0]:
        raise LogUsageError("The log message is empty")
    stamp = (now or datetime.now()).strftime("%Y-%m-%d %H:%M")
    text = f"- {stamp} · {author} · {lines[0]}\n" + "".join(f"\t{line}\n" for line in lines[1:])
    with store._lock():
        # Under the lock: an archive or a move to another board moves a todo's folder.
        path = log_path(store.config, ref)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "a", encoding="utf-8") as handle:
                handle.write(text)
        except OSError as error:
            raise IoError(f"{path}: cannot write the log: {error}") from error
    return {"time": stamp, "author": author, "message": "\n".join(lines)}


def read(config: Config, ref: str, limit: int | None = None) -> list[dict]:
    """A card's log entries, oldest first; with `limit`, only the newest ones."""
    try:
        path = log_path(config, ref)
    except NotFoundError:
        return []
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
    """Who writes a log entry from this process: the agent of this Herdr pane, else the user.

    Some agents (Codex) run tool commands in a sandbox that cannot reach the Herdr socket,
    so the pane lookup can fail even in an agent pane; the pane record from the last
    `herdboard` start in that pane names the agent then.
    """
    pane = os.environ.get("HERDR_PANE_ID")
    if not pane:
        return "user"
    return agent_kind(pane) or pane_agent(pane) or "user"


def record_pane_agent(pane: str, kind: str) -> None:
    """Remember that the agent `kind` runs in `pane`, for the log author when Herdr is unreachable."""
    # Imported here: linear_source imports the store, which imports this module.
    from .linear_source import state_dir

    if not pane or not re.fullmatch(r"[a-z]+", kind or ""):
        return
    path = state_dir() / PANE_AGENTS_FILE
    panes: dict = {}
    try:
        found = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(found, dict):
            panes = found
    except (OSError, ValueError):
        panes = {}
    panes.pop(pane, None)
    panes[pane] = kind
    while len(panes) > PANE_AGENT_LIMIT:
        panes.pop(next(iter(panes)))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(panes), encoding="utf-8")
    except OSError:
        pass


def pane_agent(pane: str) -> str | None:
    """The agent kind that herdboard last started in `pane`, or None."""
    from .linear_source import state_dir

    try:
        panes = json.loads((state_dir() / PANE_AGENTS_FILE).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    kind = panes.get(pane) if isinstance(panes, dict) else None
    return kind if isinstance(kind, str) and re.fullmatch(r"[a-z]+", kind) else None
